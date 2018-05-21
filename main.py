#!/usr/bin/env python

import sys
import time
import math

from tqdm import tqdm

import torch
import torch.nn as nn
import torch.optim as optim
import torch.autograd as autograd
import torch.distributed as dist
import torch.multiprocessing as mp

import data
from model import RNNModel
from nce import NCELoss
from utils import process_data, build_unigram_noise, setup_parser, setup_logger
from generic_model import GenModel
from index_gru import IndexGRU
from index_linear import IndexLinear


parser = setup_parser()
args = parser.parse_args()
logger = setup_logger('pt-nce-%s' % args.save)
logger.info(args)

# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
#################################################################
# Load data
#################################################################
corpus = data.Corpus(
    path=args.data,
    vocab_path=args.vocab,
    batch_size=args.batch_size,
    shuffle=True,
    pin_memory=args.cuda,
    min_freq=args.min_freq,
    concat=args.concat,
    bptt=args.bptt,
)

################################################################## Build the criterion and model, setup the NCE module
#################################################################

ntoken = len(corpus.train.dataset.vocab)
logger.info('Vocabulary size is {}'.format(ntoken))

# noise for soise sampling in NCE
noise = build_unigram_noise(
    torch.FloatTensor(corpus.train.dataset.vocab.idx2count)
)

# setting up NCELoss modules
if args.index_module == 'linear':
    criterion = IndexLinear(
        args.nhid,
        ntoken,
        noise=noise,
        noise_ratio=args.noise_ratio,
        norm_term=args.norm_term,
    )
    criterion.nce = args.nce
    model = RNNModel(
        ntoken, args.emsize, args.nhid, args.nlayers,
        criterion=criterion, dropout=args.dropout,
    )

elif args.index_module == 'gru':
    logger.warning('Falling into one layer GRU due to indx_GRU supporting')
    nce_criterion = IndexGRU(
        ntoken, args.nhid, args.nhid,
        args.dropout,
        noise=noise,
        noise_ratio=args.noise_ratio,
        norm_term=args.norm_term,
    )
    model = GenModel(
        criterion=nce_criterion,
    )

else:
    logger.error('The index module [%s] is not supported yet' % args.index_module)
    raise(NotImplementedError('index module not supported'))

logger.info('model definition:\n %s', model)
#################################################################
# Training code
#################################################################

dense_params = [model.criterion.bias] + list(model.rnn.parameters())
model.criterion.emb = model.encoder  # test tying weight
model.encoder.share_memory()

def train(model, data_source, epoch, lr=1.0, weight_decay=1e-5, momentum=0.9):
    optimizer = optim.SGD(
        params=model.rnn.parameters(),
        lr=lr,
        momentum=momentum,
        weight_decay=weight_decay,
    )
    optimizer.add_param_group({'params': model.criterion.bias})
    # Turn on training mode which enables dropout.
    # model.encoder.weight = torch.nn.Parameter(model.encoder.weight.pin_memory())
    model.train()
    model.criterion.nce = args.nce
    total_loss = 0
    pbar = tqdm(data_source, desc='Training PPL: ....')
    for num_batch, data_batch in enumerate(pbar):
        optimizer.zero_grad()
        if model.encoder.weight.grad is not None:
            model.encoder.weight.grad.zero_()
        data, target, length = process_data(data_batch, cuda=False, sep_target=False)
        loss = model(data, length.cuda(), lr)
        with torch.autograd.profiler.profile(enabled=args.prof, use_cuda=True) as p:
            loss.backward()
        if args.prof:
            print(p)

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm(dense_params, args.clip)
        optimizer.step()
        emb_grad = model.encoder.weight.grad.coalesce()
        indices = emb_grad._indices().view(-1)
        w_d = weight_decay * model.encoder.weight.data[indices]
        emb_grad._values().add_(w_d)
        # model.encoder.weight.data.add_(-lr * emb_grad)
        model.encoder.weight.data.index_add_(0, indices, -lr * emb_grad._values())


        total_loss += loss.data[0]

        if args.prof:
            break
        if num_batch % args.log_interval == 0 and num_batch > 0:
            for param in dense_params:
                dist.all_reduce(param.data, op=dist.reduce_op.SUM)
                param.data /= dist.get_world_size()
            cur_loss = total_loss / args.log_interval
            ppl = math.exp(cur_loss)
            logger.debug(
                '| epoch {:3d} | {:5d}/{:5d} batches '
                '| lr {:02.2f} | loss {:5.2f} | ppl {:8.2f}'.format(
                    epoch, num_batch, len(corpus.train),
                    lr, cur_loss, ppl
                  )
            )
            pbar.set_description('Training PPL %.1f' % ppl)
            total_loss = 0

def evaluate(model, data_source, cuda=args.cuda):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    model.criterion.nce = False

    eval_loss = 0
    total_length = 0

    with torch.no_grad():
        for data_batch in data_source:
            data, target, length = process_data(data_batch, cuda=False, sep_target=False)

            loss = model(data, length.cuda())
            cur_length = int(length.data.sum())
            eval_loss += loss.data[0] * cur_length
            total_length += cur_length

    model.criterion.nce = True

    return math.exp(eval_loss/total_length)


def run_epoch(model, epoch, lr, best_val_ppl):
    """A training epoch includes training, evaluation and logging"""
    epoch_start_time = time.time()
    train(model, corpus.train, epoch=epoch, lr=lr, weight_decay=args.weight_decay)
    if args.prof:
        return
    val_ppl = evaluate(model, corpus.valid)
    logger.warn(
        '| end of epoch {:3d} | time: {:5.2f}s |'
        'valid ppl {:8.2f}'.format(
            epoch,
            (time.time() - epoch_start_time),
            val_ppl)
    )
    with open(args.save+'.epoch_{}'.format(epoch), 'wb') as f:
        torch.save(model, f)
    # Save the model if the validation loss is the best we've seen so far.
    if not best_val_ppl or val_ppl < best_val_ppl:
        with open(args.save, 'wb') as f:
            torch.save(model, f)
        best_val_ppl = val_ppl
    else:
        # Anneal the learning rate if no improvement has been seen in the
        # validation dataset.
        lr /= args.lr_decay
    return model, lr, best_val_ppl

WORLD_SIZE = 2

def main(model):
    dist.init_process_group('nccl', world_size=WORLD_SIZE, init_method='file:///tmp/shared_tile')
    torch.cuda.set_device(dist.get_rank())
    lr = args.lr
    best_val_ppl = None
    if args.cuda:
        for p in dense_params:
            p.data = p.data.cuda()
        model.criterion.noise = model.criterion.noise.cuda()
        model.criterion.to_cuda()
    if args.train:
        # At any point you can hit Ctrl + C to break out of training early.
        try:
            for epoch in range(1, args.epochs + 1):
                model, lr, best_val_ppl = run_epoch(model, epoch, lr, best_val_ppl)
                if args.prof:
                    break
        except KeyboardInterrupt:
            logger.warning('Exiting from training early')

    else:
        # Load the best saved model.
        with open(args.save, 'rb') as f:
            model = torch.load(f)

    if not args.prof:
        # Run on test data.
        test_ppl = evaluate(model, corpus.test)
        logger.warning('| End of training | test ppl {:8.2f}'.format(test_ppl))
        sys.stdout.flush()

if __name__ == '__main__':
    processes = []
    for rank in range(WORLD_SIZE):
        p = mp.Process(target=main, args=(model,))
        p.start()
        processes.append(p)
    for p in processes:
        p.join()

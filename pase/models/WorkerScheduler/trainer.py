from ..Minions.minions import *
from ..Minions.cls_minions import *
from .encoder import encoder
from .lr_scheduler import LR_Scheduler
from ..pase import pase, pase_attention, pase_chunking
from .worker_scheduler import backprop_scheduler
import torch
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
import torch.nn as nn
import numpy as np
import random
import os
import pickle
from tqdm import tqdm, trange
try:
    from tensorboardX import SummaryWriter
    use_tb = True
except ImportError:
    print('cannot import Tensorboard, use pickle for logging')
    use_tb = False


class trainer(object):
    def __init__(self,
                 frontend=None,
                 frontend_cfg=None,
                 att_cfg=None,
                 minions_cfg=None,
                 cfg=None,
                 cls_lst=None,
                 regr_lst=None,
                 pretrained_ckpt=None,
                 tensorboard=None,
                 backprop_mode = None,
                 name='Pase_base',
                 device=None):

        # init the pase
        if not cls_lst:
            cls_lst = ["mi", "cmi", "spc"]
        if not regr_lst:
            regr_lst = ["chunk", "lps", "mfcc", "prosody"]

        if att_cfg:
            print("training pase with attention!")
            self.model = pase_attention(frontend=frontend,
                            frontend_cfg=frontend_cfg,
                            minions_cfg=minions_cfg,
                            att_cfg=att_cfg,
                            cls_lst=cls_lst, regr_lst=regr_lst,
                            pretrained_ckpt=pretrained_ckpt,
                            name=name)
        else:
            print("training pase...")
            self.model = pase(frontend=frontend,
                            frontend_cfg=frontend_cfg,
                            minions_cfg=minions_cfg,
                            cls_lst=cls_lst, regr_lst=regr_lst,
                            pretrained_ckpt=pretrained_ckpt,
                            name=name)

        # init param
        self.epoch = cfg['epoch']
        self.bsize = cfg['batch_size']
        self.save_path = cfg['save_path']
        self.log_freq = cfg['log_freq']
        self.bpe = cfg['bpe']
        self.va_bpe = cfg['va_bpe']
        self.savers = []



        # init front end optim
        self.frontend_optim = getattr(optim, cfg['fe_opt'])(self.model.frontend.parameters(),
                                              lr=cfg['fe_lr'])
        self.fe_scheduler = LR_Scheduler('poly', optim_name="frontend", base_lr=cfg['fe_lr'],
                                    num_epochs=self.epoch,
                                    iters_per_epoch=self.bpe)

        self.savers.append(Saver(self.model.frontend, self.save_path,
                        max_ckpts=cfg['max_ckpts'],
                        optimizer=self.frontend_optim, prefix='PASE-'))

        # init workers optim
        self.cls_optim = {}
        self.cls_scheduler = {}
        for worker in self.model.classification_workers:
            min_opt = cfg['min_opt']
            min_lr = cfg['min_lr']
            self.cls_optim[worker.name] = getattr(optim, min_opt)(worker.parameters(),
                                                             lr=min_lr)

            worker_scheduler = LR_Scheduler('poly', optim_name=worker.name, base_lr=min_lr,
                                            num_epochs=self.epoch,
                                            iters_per_epoch=self.bpe)
            self.cls_scheduler[worker.name] = worker_scheduler
            
            self.savers.append(Saver(worker, self.save_path, max_ckpts=cfg['max_ckpts'],
                                optimizer=self.cls_optim[worker.name],
                                prefix='M-{}-'.format(worker.name)))


        self.regr_optim = {}
        self.regr_scheduler = {}
        for worker in self.model.regression_workers:
            min_opt = cfg['min_opt']
            min_lr = cfg['min_lr']
            self.regr_optim[worker.name] = getattr(optim, min_opt)(worker.parameters(),
                                                              lr=min_lr)
            worker_scheduler = LR_Scheduler('poly', optim_name=worker.name, base_lr=min_lr,
                                            num_epochs=self.epoch,
                                            iters_per_epoch=self.bpe)
            self.regr_scheduler[worker.name] = worker_scheduler

            self.savers.append(Saver(worker, self.save_path, max_ckpts=cfg['max_ckpts'],
                                optimizer=self.regr_optim[worker.name],
                                prefix='M-{}-'.format(worker.name)))

        if cfg["ckpt_continue"]:
                self.load_checkpoints(self.save_path)
                self.model.to(device)

        # init tensorboard writer
        print("Use tenoserboard: {}".format(tensorboard))
        self.tensorboard = tensorboard and use_tb
        if tensorboard and use_tb:
            self.writer = SummaryWriter(self.save_path)
        else:
            self.writer = None
            self.train_losses = {}
            self.valid_losses = {}

        # init backprop scheduler
        assert backprop_mode is not None
        self.backprop = backprop_scheduler(self.model, mode=backprop_mode)

        if backprop_mode == "dropout":
            print(backprop_mode)
            print("droping workers with rate: {}".format(cfg['dropout_rate']))
            self.worker_drop_rate = cfg['dropout_rate']
        else:
            self.worker_drop_rate = None

        if backprop_mode == "hyper_volume":
            print("using hyper volume with delta: {}".format(cfg['delta']))
            self.delta = cfg['delta']
        else:
            self.delta = None

        if backprop_mode == "softmax":
            print("using softmax with temp: {}".format(cfg['temp']))
            self.temp = cfg['temp']
        else:
            self.temp = None

        if backprop_mode == "adaptive":
            print("using adaptive with temp: {}, alpha: {}".format(cfg['temp'], cfg['alpha']))
            self.temp = cfg['temp']
            self.alpha = cfg['alpha']
        else:
            self.temp = None
            self.alpha = None


    def train_(self, dataloader, valid_dataloader, device):

        print('=' * 50)
        print('Beginning training...')
        print('Batches per epoch: ', self.bpe)
        print('Loss schedule policy: {}'.format(self.backprop.mode))

        for e in range(self.epoch):

            self.model.train()

            iterator = iter(dataloader)

            with trange(1, self.bpe + 1) as pbar:
                for bidx in pbar:
                    pbar.set_description("Epoch {}/{}".format(e, self.epoch))
                    try:
                        batch = next(iterator)
                    except StopIteration:
                        iterator = iter(dataloader)
                        batch = next(iterator)

                    # inference
                    h, chunk, preds, labels = self.model.forward(batch, device)

                    # backprop using scheduler
                    losses = self.backprop(preds,
                                           labels,
                                            self.cls_optim,
                                            self.regr_optim,
                                            self.frontend_optim,
                                            device=device,
                                           dropout_rate=self.worker_drop_rate,
                                           delta=self.delta,
                                           temperture=self.temp,
                                           alpha=self.alpha,
                                           batch = batch)


                    if bidx % self.log_freq == 0 or bidx >= self.bpe:
                        # decrease learning rate
                        lrs = {}
                        lrs["frontend"] = self.fe_scheduler(self.frontend_optim, bidx, e, losses["total"].item())

                        for name, scheduler in self.cls_scheduler.items():
                            lrs[name] = scheduler(self.cls_optim[name], bidx, e, losses[name].item())

                        for name, scheduler in self.regr_scheduler.items():
                            lrs[name] = scheduler(self.regr_optim[name], bidx, e, losses[name].item())

                        # print out info
                        self.train_logger(preds, labels, losses, e, bidx, lrs, pbar)

            self._eval(valid_dataloader,
                       epoch=e,
                       device=device)

            # torch.save(self.model.frontend.state_dict(),
            #            os.path.join(self.save_path,
            #                         'FE_e{}.ckpt'.format(e)))
            # torch.save(self.model.state_dict(),
            #            os.path.join(self.save_path,
            #                         'fullmodel_e{}.ckpt'.format(e)))
            for saver in self.savers:
                saver.save(saver.prefix[:-1], e * self.bpe + bidx)



    def _eval(self, dataloader, epoch=0, device='cpu'):

        self.model.eval()
        with torch.no_grad():
            print('=' * 50)
            print('Beginning evaluation...')
            running_loss = {}

            iterator = iter(dataloader)
            with trange(1, self.va_bpe + 1) as pbar:
                for bidx in pbar:
                    pbar.set_description("Eval: {}/{}".format(bidx, self.va_bpe+1))
                    try:
                        batch = next(iterator)
                    except StopIteration:
                        iterator = iter(dataloader)
                        batch = next(iterator)

                    # inference
                    h, chunk, preds, labels = self.model.forward(batch, device)

                    # calculate losses
                    tot_loss = torch.tensor([0.]).to(device)
                    losses = {}
                    for worker in self.model.classification_workers:
                        loss = worker.loss(preds[worker.name], labels[worker.name])
                        losses[worker.name] = loss
                        tot_loss += loss
                        if worker.name not in running_loss:
                            running_loss[worker.name] = [loss.item()]
                        else:
                            running_loss[worker.name].append(loss.item())

                    for worker in self.model.regression_workers:
                        loss = worker.loss(preds[worker.name], labels[worker.name])
                        losses[worker.name] = loss
                        tot_loss += loss
                        if worker.name not in running_loss:
                            running_loss[worker.name] = [loss.item()]
                        else:
                            running_loss[worker.name].append(loss.item())

                    losses["total"] = tot_loss

                    if bidx % self.log_freq == 0 or bidx >= self.bpe:
                        pbar.write('-' * 50)
                        pbar.write('EVAL Batch {}/{} (Epoch {}):'.format(bidx,
                                                                    self.va_bpe,
                                                                    epoch))
                        for name, loss in losses.items():
                            pbar.write('{} loss: {:.3f}'
                                  ''.format(name, loss.item()))

            self.eval_logger(running_loss, epoch, pbar)

    def load_checkpoints(self, load_path):

        # now load each ckpt found
        giters = 0
        for saver in self.savers:
            # try loading all savers last state if not forbidden is active
            try:
                state = saver.read_latest_checkpoint()
                giter_ = saver.load_ckpt_step(state)
                print('giter_ found: ', giter_)
                # assert all ckpts happened at last same step
                if giters == 0:
                    giters = giter_
                else:
                    assert giters == giter_, giter_
                saver.load_pretrained_ckpt(os.path.join(load_path,
                                                        'weights_' + state), 
                                           load_last=True)
            except TypeError:
                break


    def train_logger(self, preds, labels, losses, epoch, bidx, lrs, pbar):
        step = epoch * self.bpe + bidx
        pbar.write("=" * 50)
        pbar.write('Batch {}/{} (Epoch {}) step: {}:'.format(bidx, self.bpe, epoch, step))

        for name, loss in losses.items():
            if name == "total":
                pbar.write('%s, learning rate = %.8f, loss = %.4f' % ("total", lrs['frontend'], loss))
            else:
                pbar.write('%s, learning rate = %.8f, loss = %.4f' % (name, lrs[name], loss))

            if name != "total" and self.writer:

                self.writer.add_scalar('train/{}_loss'.format(name),
                                  loss.item(),
                                  global_step=step)
                self.writer.add_histogram('train/{}'.format(name),
                                     preds[name].data,
                                     bins='sturges',
                                     global_step=step)

                self.writer.add_histogram('train/gtruth_{}'.format(name),
                                     labels[name].data,
                                     bins='sturges',
                                     global_step=step)
        if not self.tensorboard:

            for name, _ in preds.items():
                    preds[name] = preds[name].data
                    labels[name] = labels[name].data

            self.train_losses['itr'] = step
            self.train_losses['losses'] = losses
            self.train_losses['dist'] = preds
            self.train_losses['dist_gt'] = labels

            with open(os.path.join(self.save_path, 'train_losses.pkl'), "wb") as f:
                pbar.write("saved log to {}".format(os.path.join(self.save_path, 'train_losses.pkl')))
                pickle.dump(self.train_losses, f, protocol=pickle.HIGHEST_PROTOCOL)

    def eval_logger(self, running_loss, epoch, pbar):
        pbar.write("=" * 50)
        if self.writer:
            for name, loss in running_loss.items():
                loss = np.mean(loss)
                pbar.write("avg loss {}: {}".format(name, loss))

                self.writer.add_scalar('eval/{}_loss'.format(name),
                                        loss,
                                        global_step=epoch)
        else:
            self.valid_losses['epoch'] = epoch
            self.valid_losses['losses'] = running_loss

            with open(os.path.join(self.save_path, 'valid_losses.pkl'), "wb") as f:
                pbar.write("saved log to {}".format(os.path.join(self.save_path, 'valid_losses.pkl')))
                pickle.dump(self.valid_losses, f, protocol=pickle.HIGHEST_PROTOCOL)

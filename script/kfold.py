import os
import time
import pprint
import torch
import dataset.dataset_factory as dataset_factory
from colorama import Back, Fore
from config import cfg, update_config_from_file
from torch.utils.data import DataLoader, ConcatDataset
from torch.optim import SGD, Adam
from torch.optim.lr_scheduler import StepLR
from dataset.collate import collate_train, collate_test
from model.vgg16 import VGG16
from model.resnet import Resnet
from utils.net_utils import clip_gradient

from sklearn.model_selection import KFold

def reset_weights(m):
  '''
    Try resetting model weights to avoid
    weight leakage.
  '''
  for layer in m.children():
   if hasattr(layer, 'reset_parameters'):
    print(f'Reset trainable parameters of layer = {layer}')
    layer.reset_parameters()

def kfold(dataset, net, batch_size, learning_rate, param_optimizer, lr_decay_step,
          lr_decay_gamma, pretrain, resume, class_agnostic, total_epoch,
          display_interval, session, epoch, save_dir, vis_off, mGPU, add_params, k_folds):
    device = torch.device('cuda:0') if cfg.CUDA else torch.device('cpu')
    print(Back.CYAN + Fore.BLACK + 'Current device: %s' % (str(device).upper()))

    if batch_size is not None:
        cfg.TRAIN.BATCH_SIZE = batch_size
    if learning_rate is not None:
        cfg.TRAIN.LEARNING_RATE = learning_rate
    if lr_decay_step is not None:
        cfg.TRAIN.LR_DECAY_STEP = lr_decay_step
    if lr_decay_gamma is not None:
        cfg.TRAIN.LR_DECAY_GAMMA = lr_decay_gamma

    if 'cfg_file' in add_params:
        update_config_from_file(add_params['cfg_file'])

    print(Back.WHITE + Fore.BLACK + 'Using config:')
    print('GENERAL:')
    pprint.pprint(cfg.GENERAL)
    print('TRAIN:') # We're gonna use the train config for the kfold
    pprint.pprint(cfg.TRAIN)
    print('RPN:')
    pprint.pp(cfg.RPN)

    results = {}
    kfold = KFold(n_splits=k_folds, shuffle=True)

    dataset, ds_name = dataset_factory.get_dataset(dataset, add_params)
    if 'data_path' in add_params:
        cfg.DATA_DIR = add_params['data_path']
    output_dir = os.path.join(cfg.DATA_DIR, save_dir, net, ds_name)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    print(Back.CYAN + Fore.BLACK + 'Output directory: %s' % (output_dir))

    pretrained = True
    model_name = '{}.pth'.format(net)
    if 'use_pretrain' in add_params:
        pretrained = add_params['use_pretrain']
    if 'model_name' in add_params:
        model_name = '{}.pth'.format(add_params['model_name'])
    model_path = os.path.join(cfg.DATA_DIR, 'pretrained_model', model_name)
    if net == 'vgg16':
        faster_rcnn = VGG16(dataset.num_classes, class_agnostic=class_agnostic,
                            pretrained=pretrained, model_path=model_path)
    elif net.startswith('resnet'):
        num_layers = net[6:]
        faster_rcnn = Resnet(num_layers, dataset.num_classes, class_agnostic=class_agnostic,
                             pretrained=pretrained, model_path=model_path)
    else:
        raise ValueError(Back.RED + 'Network "{}" is not defined!'.format(net))

    for fold, (train_ids, test_ids) in enumerate(kfold.split(dataset)):
        train_subsampler = torch.utils.data.SubsetRandomSampler(train_ids)

        trainloader = torch.utils.data.DataLoader(
                      dataset, 
                      batch_size=cfg.TRAIN.BATCH_SIZE,
                      collate_fn=collate_train,
                      sampler=train_subsampler)
        
        faster_rcnn.init()
        faster_rcnn.apply(reset_weights)
        faster_rcnn.to(device)

        params = []
        for key, value in dict(faster_rcnn.named_parameters()).items():
            if value.requires_grad:
                if 'bias' in key:
                    params += [{'params': [value],
                                'lr': cfg.TRAIN.LEARNING_RATE * (cfg.TRAIN.DOUBLE_BIAS + 1),
                                'weight_decay': cfg.TRAIN.BIAS_DECAY and cfg.TRAIN.WEIGHT_DECAY or 0}]
                else:
                    params += [{'params': [value],
                                'lr':cfg.TRAIN.LEARNING_RATE,
                                'weight_decay': cfg.TRAIN.WEIGHT_DECAY}]
        
        if param_optimizer == 'sgd':
            optimizer = SGD(params, momentum=cfg.TRAIN.MOMENTUM)
        elif param_optimizer == 'adam':
            optimizer = Adam(params)
        else:
            raise ValueError(Back.RED + 'Optimizer "{}" is not defined!'.format(param_optimizer))

        start_epoch = 1

        if pretrain or resume:
            model_name = 'frcnn_{}_{}.pth'.format(session, epoch)
            if 'model_name' in add_params:
                model_name = '{}.pth'.format(add_params['model_name'])
            model_path = os.path.join(output_dir, model_name)
            print(Back.WHITE + Fore.BLACK + 'Loading checkpoint %s...' % (model_path))
            checkpoint = torch.load(model_path, map_location=device)
            faster_rcnn.load_state_dict(checkpoint['model'])
            if resume:
                start_epoch = checkpoint['epoch']
                optimizer.load_state_dict(checkpoint['optimizer'])
            print('Done.')

        # Decays the learning rate of each parameter group by gamma every step_size epochs.
        lr_scheduler = StepLR(optimizer, step_size=cfg.TRAIN.LR_DECAY_STEP,
                            gamma=cfg.TRAIN.LR_DECAY_GAMMA)

        if mGPU:
            faster_rcnn = torch.nn.DataParallel(faster_rcnn)

        faster_rcnn.train()

        if not vis_off:
            from visualize.plotter import Plotter
            plotter = Plotter()

            for current_epoch in range(start_epoch, total_epoch + 1):
                loss_temp = 0
                start = time.time()

                for step, data in enumerate(trainloader):
                    image_data = data[0].to(device)
                    image_info = data[1].to(device)
                    gt_boxes = data[2].to(device)

                    *_, rpn_loss_cls, rpn_loss_bbox, \
                        RCNN_loss_cls, RCNN_loss_bbox = faster_rcnn(image_data, image_info, gt_boxes)

                    loss = rpn_loss_cls.mean() + rpn_loss_bbox.mean() \
                        + RCNN_loss_cls.mean() + RCNN_loss_bbox.mean()
                    loss_temp += loss.item()

                    optimizer.zero_grad()
                    loss.backward()
                    if net == 'vgg16':
                        clip_gradient(faster_rcnn, 10.)
                    optimizer.step()

                    if step % display_interval == 0:
                        end = time.time()
                        if step > 0:
                            loss_temp /= (display_interval + 1)

                        loss_rpn_cls = rpn_loss_cls.mean().item()
                        loss_rpn_bbox = rpn_loss_bbox.mean().item()
                        loss_rcnn_cls = RCNN_loss_cls.mean().item()
                        loss_rcnn_bbox = RCNN_loss_bbox.mean().item()

                        print(Back.WHITE + Fore.BLACK + '[fold %d][epoch %2d/%2d][iter %4d/%4d]'
                            % (fold, current_epoch, total_epoch, step, len(trainloader)))
                        print('loss: %.4f, learning rate: %.2e, time cost: %f'
                            % (loss_temp, optimizer.param_groups[0]['lr'], end-start))
                        print('rpn_cls: %.4f, rpn_box: %.4f, rcnn_cls: %.4f, rcnn_box %.4f'
                            % (loss_rpn_cls, loss_rpn_bbox, loss_rcnn_cls, loss_rcnn_bbox))

                        if not vis_off:
                            plotter_data = {'fold': fold,
                                            'current_epoch': current_epoch,
                                            'total_epoch': total_epoch,
                                            'current_iter': step,
                                            'total_iter': len(trainloader),
                                            'lr': optimizer.param_groups[0]['lr'],
                                            'time_cost': end-start,
                                            'loss': [loss_temp,
                                                    loss_rpn_cls,
                                                    loss_rpn_bbox,
                                                    loss_rcnn_cls,
                                                    loss_rcnn_bbox]}
                            plotter.send('data', plotter_data)

                        loss_temp = 0
                        start = time.time()

                lr_scheduler.step()

                save_path = os.path.join(output_dir, 'frcnn_F{}_S{}_E{}.pth'.format(fold, session, current_epoch))
                checkpoint = {'epoch': current_epoch + 1,
                            'model': faster_rcnn.state_dict(),
                            'optimizer': optimizer.state_dict()}
                torch.save(checkpoint, save_path)
                print(Back.WHITE + Fore.BLACK + '[Fold %d, Epoch %2d/%2d] Model saved: %s' % (fold, current_epoch, total_epoch, save_path))

        save_path = os.path.join(output_dir, 'frcnn_F{}_S{}.pth'.format(fold, session))
        checkpoint = {'fold': fold,
                    'model': faster_rcnn.state_dict(),
                    'optimizer': optimizer.state_dict()}
        torch.save(checkpoint, save_path)
        print(Back.WHITE + Fore.BLACK + '[Fold %d] Model saved: %s' % (fold, save_path))
    
    save_path = os.path.join(output_dir, 'frcnn_S{}.pth'.format(session))
    checkpoint = { 'model': faster_rcnn.state_dict(),
                'optimizer': optimizer.state_dict() }
    torch.save(checkpoint, save_path)

    if not vis_off:
        plotter.send('save', save_path[:-4])
    print(Back.WHITE + Fore.BLACK + 'Model saved: %s' % (save_path))

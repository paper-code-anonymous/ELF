# adapted from
# https://github.com/GeorgeCazenavette/mtt-distillation

import os
import sys
import argparse
import numpy as np
from datetime import datetime

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.utils
from tqdm import tqdm
from utils import (
    get_dataset,
    get_network,
    get_eval_pool,
    evaluate_synset,
    get_time,
    DiffAugment,
    ParamDiffAug,
    seed_torch
)

import copy
import random
from reparam_module import ReparamModule

import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)


def main(args):
    os.environ["CUDA_VISIBLE_DEVICES"] = args.CUDA_VISIBLE_DEVICES
    seed_torch()

    if args.max_experts is not None and args.max_files is not None:
        args.total_experts = args.max_experts * args.max_files

    print("CUDNN STATUS: {}".format(torch.backends.cudnn.enabled))

    args.dsa = True if args.dsa == 'True' else False
    args.device = 'cuda' if torch.cuda.is_available() else 'cpu'
    
    now = datetime.now()
    current_time = now.strftime("%m-%d-%Y-%H:%M:%S")
    print(f'current time: \n{current_time}')
    print('Hyper-parameters: \n', args.__dict__)


    eval_it_pool = np.arange(0, args.Iteration + 1, args.eval_it).tolist()
    (
        channel,
        im_size,
        num_classes,
        class_names,
        mean,
        std,
        dst_train,
        dst_test,
        testloader,
        loader_train_dict,
        class_map,
        class_map_inv,
    ) = get_dataset(
        args.dataset, args.data_path, args.batch_real, args.subset, args=args
    )
    model_eval_pool = get_eval_pool(args.eval_mode,  args.model)

    im_res = im_size[0]
    args.im_size = im_size

    accs_all_exps = dict()  # record performances of all experiments
    for key in model_eval_pool:
        accs_all_exps[key] = []

    data_save = []

    if args.dsa:
        args.dc_aug_param = None

    args.dsa_param = ParamDiffAug()


    if args.batch_syn is None:
        args.batch_syn = num_classes * args.ipc

    args.distributed = torch.cuda.device_count() > 1

    print('Evaluation model pool: ', model_eval_pool)

    ''' organize the real dataset '''
    images_all = []
    labels_all = []
    indices_class = [[] for c in range(num_classes)]

    print("BUILDING DATASET")
    for i in tqdm(range(len(dst_train))):
        sample = dst_train[i]
        # [1,3,32,32]
        images_all.append(torch.unsqueeze(sample[0], dim=0))
        # class num
        labels_all.append(class_map[torch.tensor(sample[1]).item()])
    for i, lab in tqdm(enumerate(labels_all)):
        indices_class[lab].append(i)
    images_all = torch.cat(images_all, dim=0).to("cpu")
    labels_all = torch.tensor(labels_all, dtype=torch.long, device="cpu")

    def get_images(c, n):  # get random n images from class c
        idx_shuffle = np.random.permutation(indices_class[c])[:n]
        return images_all[idx_shuffle]

    ''' initialize the synthetic data '''
    label_syn = torch.tensor(
        [np.ones(args.ipc, dtype=np.int_) * i for i in range(num_classes)],
        dtype=torch.long,
        requires_grad=False,
        device=args.device,
    ).view(
        -1
    )  # [0,0,0, 1,1,1, ..., 9,9,9]


    image_syn = torch.randn(
        size=(num_classes * args.ipc, channel, im_size[0], im_size[1]),
        dtype=torch.float,
    )

    syn_lr = torch.tensor(args.lr_teacher).to(args.device)

    if args.pix_init == 'real':
        print('initialize synthetic data from random real images')
        for c in range(num_classes):
            image_syn.data[c * args.ipc : (c + 1) * args.ipc] = (
                get_images(c, args.ipc).detach().data
            )
    else:
        print('initialize synthetic data from random noise')

    ''' training '''
    image_syn = image_syn.detach().to(args.device).requires_grad_(True)
    syn_lr = syn_lr.detach().to(args.device).requires_grad_(True)
    optimizer_img = torch.optim.SGD([image_syn], lr=args.lr_img, momentum=0.5)
    optimizer_lr = torch.optim.SGD([syn_lr], lr=args.lr_lr, momentum=0.5)
    optimizer_img.zero_grad()

    criterion = nn.CrossEntropyLoss().to(args.device)
    print('%s training begins' % get_time())
    expert_dir = os.path.join(args.buffer_path, args.dataset)
    if args.dataset == "ImageNet":
        expert_dir = os.path.join(expert_dir, args.subset, str(args.res))
    if args.dataset in ["CIFAR10", "CIFAR100"] and not args.zca:
        expert_dir += "_NO_ZCA"
    expert_dir = os.path.join(expert_dir, args.model)
    print("Expert Dir: {}".format(expert_dir))

    if args.load_all:
        print('load all buffer')
        buffer = []
        n = 0
        while os.path.exists(os.path.join(expert_dir, "replay_buffer_{}.pt".format(n))):
            print(f"load buffer:replay_buffer_{n}.pt")
            buffer = buffer + torch.load(
                os.path.join(expert_dir, "replay_buffer_{}.pt".format(n))
            )
            n += 1
        if n == 0:
            raise AssertionError("No buffers detected at {}".format(expert_dir))
    else:
        expert_files = []
        n = 0
        while os.path.exists(os.path.join(expert_dir, "replay_buffer_{}.pt".format(n))):
            expert_files.append(
                os.path.join(expert_dir, "replalry_buffer_{}.pt".format(n))
            )
            n += 1
        if n == 0:
            raise AssertionError("No buffers detected at {}".format(expert_dir))
        file_idx = 0
        expert_idx = 0
        random.shuffle(expert_files)
        if args.max_files is not None:
            expert_files = expert_files[: args.max_files]

        print("loading file {}".format(expert_files[file_idx]))
        buffer = torch.load(expert_files[file_idx])
        if args.max_experts is not None:
            buffer = buffer[: args.max_experts]
        random.shuffle(buffer)

    best_acc = {m: 0 for m in model_eval_pool}

    best_std = {m: 0 for m in model_eval_pool}

    for it in range(0, args.Iteration + 1):
        save_best_it = False
        
        if it in eval_it_pool and it>0:  
            for model_eval in model_eval_pool:
                print(
                    '-------------------------\nEvaluation\nmodel_train = %s, model_eval = %s, iteration = %d'
                    % (args.model, model_eval, it)
                )
                if args.dsa:
                    print('DSA augmentation strategy: \n', args.dsa_strategy)
                    print('DSA augmentation parameters: \n', args.dsa_param.__dict__)
                else:
                    print('DC augmentation parameters: \n', args.dc_aug_param)

                accs_test = []
                accs_train = []
                for it_eval in range(args.num_eval):
                    net_eval = get_network(
                        model_eval, channel, num_classes, im_size, args=args
                    ).to(
                        args.device
                    )  # get a random model

                    eval_labs = label_syn
                    with torch.no_grad():
                        image_save = image_syn
                    image_syn_eval, label_syn_eval = copy.deepcopy(
                        image_save.detach()
                    ), copy.deepcopy(
                        eval_labs.detach()
                    )  # avoid any unaware modification

                    args.lr_net = syn_lr.item()
                    _, acc_train, acc_test = evaluate_synset(
                        it_eval,
                        net_eval,
                        image_syn_eval,
                        label_syn_eval,
                        testloader,
                        args,
                        texture=args.texture,
                    )
                    accs_test.append(acc_test)
                    accs_train.append(acc_train)
                accs_test = np.array(accs_test)
                accs_train = np.array(accs_train)
                acc_test_mean = np.mean(accs_test)
                acc_test_std = np.std(accs_test)
                if acc_test_mean > best_acc[model_eval]:
                    best_acc[model_eval] = acc_test_mean
                    best_std[model_eval] = acc_test_std
                    save_best_it = True
                print(
                    'Evaluate %d random %s, mean = %.4f std = %.4f\n-------------------------'
                    % (len(accs_test), model_eval, acc_test_mean, acc_test_std)
                )

        if it in eval_it_pool and (
            save_best_it or it % args.save_it == 0
        ) and it > 0:
            with torch.no_grad():
                image_save = image_syn.cuda()
                save_dir = os.path.join(
                    ".", "result_MTT", args.dataset
                )
                if args.dataset in ["CIFAR10", "CIFAR100"] and not args.zca:
                    save_dir += "_NO_ZCA"
                save_dir=os.path.join(save_dir,args.model, str(args.ipc)+'ipc')
                if not os.path.exists(save_dir):
                    os.makedirs(save_dir)

                if save_best_it:
                    print(f'Save the best results on iteration:{it}, lr={args.lr_net}\n-------------------------')
                    torch.save(
                        image_save.cpu(),
                        os.path.join(save_dir, "images_best.pt"),
                    )
                    torch.save(
                        label_syn.cpu(),
                        os.path.join(save_dir, "labels_best.pt"),
                    )
                else:
                    torch.save(
                        image_save.cpu(), os.path.join(save_dir, "images_{}.pt".format(it))
                    )
                    torch.save(
                        label_syn.cpu(), os.path.join(save_dir, "labels_{}.pt".format(it))
                    )
        student_net = get_network(
            args.model, channel, num_classes, im_size, dist=False,args=args
        ).to(
            args.device
        )  # get a random model

        student_net = ReparamModule(student_net)

        if args.distributed:
            student_net = torch.nn.DataParallel(student_net)

        student_net.train()

        num_params = sum([np.prod(p.size()) for p in (student_net.parameters())])

        if args.load_all:
            expert_trajectory = buffer[np.random.randint(0, len(buffer))]
        else:
            expert_trajectory = buffer[expert_idx]
            expert_idx += 1
            if expert_idx == len(buffer):
                expert_idx = 0
                file_idx += 1
                if file_idx == len(expert_files):
                    file_idx = 0
                    random.shuffle(expert_files)
                print("loading file {}".format(expert_files[file_idx]))
                if args.max_files != 1:
                    del buffer
                    buffer = torch.load(expert_files[file_idx])
                if args.max_experts is not None:
                    buffer = buffer[: args.max_experts]
                random.shuffle(buffer)
        start_epoch = np.random.randint(0, args.max_start_epoch)
        starting_params = expert_trajectory[start_epoch]
        target_params = expert_trajectory[start_epoch + args.expert_epochs]
        target_params = torch.cat(
            [p.data.to(args.device).reshape(-1) for p in target_params], 0
        )
        student_params = [
            torch.cat(
                [p.data.to(args.device).reshape(-1) for p in starting_params], 0
            ).requires_grad_(True)
        ]

        starting_params = torch.cat(
            [p.data.to(args.device).reshape(-1) for p in starting_params], 0
        )

        syn_images = image_syn
        y_hat = label_syn.to(args.device)

        param_loss_list = []
        param_dist_list = []
        indices_chunks = []
        for step in range(args.syn_steps):
            if not indices_chunks:
                indices = torch.randperm(len(syn_images))
                indices_chunks = list(torch.split(indices, args.batch_syn))
            these_indices = indices_chunks.pop()
            x = syn_images[these_indices]
            this_y = y_hat[these_indices]
            if args.dsa and (not args.no_aug):
                x = DiffAugment(x, args.dsa_strategy, param=args.dsa_param)

            if args.distributed:
                forward_params = (
                    student_params[-1].unsqueeze(0).expand(torch.cuda.device_count(), -1)
                )
            else:
                forward_params = student_params[-1]
            
            x = student_net(x, flat_param=forward_params)
        
            ce_loss = criterion(x, this_y)
            grad = torch.autograd.grad(ce_loss, student_params[-1], create_graph=True)[
                0
            ]
            student_params.append(student_params[-1] - syn_lr * grad)
        param_loss = torch.tensor(0.0).to(args.device)
        param_dist = torch.tensor(0.0).to(args.device)
        param_loss += torch.nn.functional.mse_loss(
            student_params[-1], target_params, reduction="sum"
        )
        param_dist += torch.nn.functional.mse_loss(
            starting_params, target_params, reduction="sum"
        )

        param_loss_list.append(param_loss)
        param_dist_list.append(param_dist)

        param_loss /= num_params
        param_dist /= num_params

        param_loss /= param_dist

        grand_loss = param_loss

        optimizer_img.zero_grad()
        optimizer_lr.zero_grad()
        grand_loss.backward()

        optimizer_img.step()
        optimizer_lr.step()


        for _ in student_params:
            del _

        if it % 10 == 0:
            print('%s iter = %04d, loss = %.4f' % (get_time(), it, grand_loss.item()))
                



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Parameter Processing')
    parser.add_argument('--dataset', type=str, default='CIFAR10', help='dataset')
    parser.add_argument(
        '--subset',
        type=str,
        default='imagenette',
        help='ImageNet subset. This only does anything when --dataset=ImageNet',
    )
    parser.add_argument('--model', type=str, default='ConvNet', help='model')
    parser.add_argument('--max_USConv2d_width', type=int, default=512)
    parser.add_argument('--width_mult_list', type=float, nargs='+', default=[1])
    parser.add_argument('--ipc', type=int, default=1, help='image(s) per class')
    parser.add_argument(
        '--eval_mode',
        type=str,
        default='S',
        help='eval_mode, check utils.py for more info',
    )
    parser.add_argument(
        '--num_eval', type=int, default=5, help='how many networks to evaluate on'
    )
    parser.add_argument(
        '--eval_it', type=int, default=100, help='how often to evaluate'
    )
    parser.add_argument(
        '--save_it',
        type=int,
        default=2500,
        help='how often to save',
    )
    parser.add_argument(
        '--Iteration',
        type=int,
        default=5000,
        help='how many distillation steps to perform',
    )
    parser.add_argument(
        '--lr_img',
        type=float,
        default=1000,
        help='learning rate for updating synthetic images',
    )
    parser.add_argument(
        '--lr_lr',
        type=float,
        default=1e-05,
        help='learning rate for updating... learning rate',
    )
    parser.add_argument(
        '--lr_teacher',
        type=float,
        default=0.01,
        help='initialization for synthetic learning rate',
    )
    parser.add_argument(
        '--lr_init', type=float, default=0.01, help='how to init lr (alpha)'
    )

    parser.add_argument(
        '--batch_real', type=int, default=256, help='batch size for real data'
    )
    parser.add_argument(
        '--batch_syn',
        type=int,
        default=None,
        help='should only use this if you run out of VRAM',
    )
    parser.add_argument(
        '--batch_train', type=int, default=256, help='batch size for training networks'
    )

    parser.add_argument(
        '--pix_init',
        type=str,
        default='real',
        choices=["noise", "real"],
        help='noise/real: initialize synthetic images from random noise or randomly sampled real images.',
    )

    parser.add_argument(
        '--dsa',
        type=str,
        default='True',
        choices=['True', 'False'],
        help='whether to use differentiable Siamese augmentation.',
    )
    parser.add_argument(
        '--dsa_strategy',
        type=str,
        default='color_crop_cutout_flip_scale_rotate',
        help='differentiable Siamese augmentation strategy',
    )

    parser.add_argument('--data_path', type=str, default='data', help='dataset path')
    parser.add_argument(
        '--buffer_path', type=str, default='./buffers', help='buffer path'
    )

    parser.add_argument(
        '--expert_epochs',
        type=int,
        default=3,
        help='how many  epochs the target params are',
    )
    parser.add_argument(
        '--syn_steps',
        type=int,
        default=20,
        help='how many steps to take on synthetic data',
    )
    parser.add_argument(
        '--max_start_epoch', type=int, default=25, help='max epoch we can start at'
    )
    parser.add_argument(
        '--epoch_eval_train',
        type=int,
        default=1000,
        help='epochs to train a model with synthetic data',
    )

    parser.add_argument('--zca', action='store_true', help="do ZCA whitening")

    parser.add_argument(
        '--load_all',
        action='store_true',
        help="only use if you can fit all expert trajectories into RAM",
    )

    parser.add_argument(
        '--no_aug',
        type=bool,
        default=False,
        help='this turns off diff aug during distillation',
    )

    parser.add_argument(
        '--canvas_size', type=int, default=2, help='size of synthetic canvas'
    )
    parser.add_argument(
        '--canvas_samples',
        type=int,
        default=1,
        help='number of canvas samples per iteration',
    )

    parser.add_argument(
        '--max_files',
        type=int,
        default=None,
        help='number of expert files to read (leave as None unless doing ablations)',
    )
    parser.add_argument(
        '--max_experts',
        type=int,
        default=None,
        help='number of experts to read per file (leave as None unless doing ablations)',
    )


    parser.add_argument(
        '--texture', action='store_true', help="will distill textures instead"
    )
    parser.add_argument(
        '--CUDA_VISIBLE_DEVICES',
        type=str,
        default="0",
        help='gpus use for training',
    )
    parser.add_argument('--num_workers', type=int, default=0, help='num workers')

    args = parser.parse_args()

    main(args)

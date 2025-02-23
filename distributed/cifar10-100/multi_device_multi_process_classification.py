# Copyright (c) 2017 Sony Corporation. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import os
import glob
import re
import time
from args import get_args

from cifar10_data import data_iterator_cifar10
from cifar100_data import data_iterator_cifar100
from nnabla.utils.data_iterator import data_iterator
import nnabla as nn
import nnabla.communicators as C
from nnabla.ext_utils import get_extension_context
import nnabla.functions as F
import nnabla.parametric_functions as PF
import nnabla.solvers as S
import numpy as np
import functools
from models import (resnet23_prediction, categorical_error, loss_function)
from checkpoint import save_checkpoint, load_checkpoint


def backward_and_all_reduce(loss, comm, with_all_reduce_callback=False):
    params = [x.grad for x in nn.get_parameters().values()]
    if with_all_reduce_callback:
        # All-reduce gradients every 2MiB parameters during backward computation
        loss.backward(clear_buffer=True,
                      communicator_callbacks=comm.all_reduce_callback(params, 1024 * 1024 * 2))
    else:
        loss.backward(clear_buffer=True)
        comm.all_reduce(params, division=False, inplace=False)


def train():
    """
    Naive Multi-Device Training

    NOTE: the communicator exposes low-level interfaces

    * Parse command line arguments.
    * Instantiate a communicator and set parameter variables.
    * Specify contexts for computation.
    * Initialize DataIterator.
    * Construct a computation graph for training and one for validation.
    * Initialize solver and set parameter variables to that.
    * Create monitor instances for saving and displaying training stats.
    * Training loop
      * Computate error rate for validation data (periodically)
      * Get a next minibatch.
      * Execute forwardprop
      * Set parameter gradients zero
      * Execute backprop.
      * AllReduce for gradients
      * Solver updates parameters by using gradients computed by backprop and all reduce.
      * Compute training error
    """
    # Parse args
    args = get_args()
    n_train_samples = 50000
    n_valid_samples = 10000
    bs_valid = args.batch_size

    # Create Communicator and Context
    extension_module = "cudnn"
    ctx = get_extension_context(extension_module, type_config=args.type_config)
    comm = C.MultiProcessDataParalellCommunicator(ctx)
    comm.init()
    n_devices = comm.size
    mpi_rank = comm.rank
    mpi_local_rank = comm.local_rank
    device_id = mpi_local_rank
    ctx.device_id = str(device_id)
    nn.set_default_context(ctx)

    # Model
    rng = np.random.RandomState(313)
    comm_syncbn = comm if args.sync_bn else None
    if args.net == "cifar10_resnet23":
        prediction = functools.partial(
            resnet23_prediction, rng=rng, ncls=10, nmaps=32, act=F.relu, comm=comm_syncbn)
        data_iterator = data_iterator_cifar10
    if args.net == "cifar100_resnet23":
        prediction = functools.partial(
            resnet23_prediction, rng=rng, ncls=100, nmaps=384, act=F.elu, comm=comm_syncbn)
        data_iterator = data_iterator_cifar100

    # Create training graphs
    image_train = nn.Variable((args.batch_size, 3, 32, 32))
    label_train = nn.Variable((args.batch_size, 1))
    pred_train = prediction(image_train, test=False)
    pred_train.persistent = True
    loss_train = (loss_function(pred_train, label_train) /
                  n_devices).apply(persistent=True)
    error_train = F.mean(F.top_n_error(
        pred_train, label_train, axis=1)).apply(persistent=True)
    loss_error_train = F.sink(loss_train, error_train)
    input_image_train = {"image": image_train, "label": label_train}

    # Create validation graph
    image_valid = nn.Variable((bs_valid, 3, 32, 32))
    label_valid = nn.Variable((args.batch_size, 1))
    pred_valid = prediction(image_valid, test=True)
    error_valid = F.mean(F.top_n_error(pred_valid, label_valid, axis=1))
    input_image_valid = {"image": image_valid, "label": label_valid}

    # Solvers
    solver = S.Adam()
    solver.set_parameters(nn.get_parameters())
    base_lr = args.learning_rate
    warmup_iter = int(1. * n_train_samples /
                      args.batch_size / n_devices) * args.warmup_epoch
    warmup_slope = base_lr * (n_devices - 1) / warmup_iter
    solver.set_learning_rate(base_lr)

    # load checkpoint if file exist.
    start_point = 0
    if args.use_latest_checkpoint:
        files = glob.glob(f'{args.model_save_path}/checkpoint_*.json')
        if len(files) != 0:
            index = max(
                [int(n) for n in [re.sub(r'.*checkpoint_(\d+).json', '\\1', f) for f in files]])
            # load weights and solver state info from specified checkpoint file.
            start_point = load_checkpoint(
                f'{args.model_save_path}/checkpoint_{index}.json', solver)

    # Create monitor
    from nnabla.monitor import Monitor, MonitorSeries, MonitorTimeElapsed
    monitor = Monitor(args.monitor_path)
    monitor_loss = MonitorSeries("Training loss", monitor, interval=10)
    monitor_err = MonitorSeries("Training error", monitor, interval=10)
    monitor_time = MonitorTimeElapsed("Training time", monitor, interval=10)
    monitor_verr = MonitorSeries("Validation error", monitor, interval=1)
    monitor_vtime = MonitorTimeElapsed("Validation time", monitor, interval=1)

    # Data Iterator

    # If the data does not exist, it will try to download it from the server
    # and prepare it. When executing multiple processes on the same host, it is
    # necessary to execute initial data preparation by the representative
    # process (local_rank is 0) on the host.

    # Prepare data only when local_rank is 0
    if mpi_rank == 0:
        rng = np.random.RandomState(device_id)
        _, tdata = data_iterator(args.batch_size, True, rng)
        vsource, vdata = data_iterator(args.batch_size, False)

    # Wait for data to be prepared without watchdog
    comm.barrier()

    # Prepare data when local_rank is not 0
    if mpi_rank != 0:
        rng = np.random.RandomState(device_id)
        _, tdata = data_iterator(args.batch_size, True, rng)
        vsource, vdata = data_iterator(args.batch_size, False)

    # loss_error_train.forward()

    # Training-loop
    ve = nn.Variable()
    model_save_interval = 0
    for i in range(start_point, int(args.max_iter / n_devices)):
        # Validation
        if i % int(n_train_samples / args.batch_size / n_devices) == 0:
            ve_local = 0.
            k = 0
            idx = np.random.permutation(n_valid_samples)
            val_images = vsource.images[idx]
            val_labels = vsource.labels[idx]
            for j in range(int(n_valid_samples / n_devices * mpi_rank),
                           int(n_valid_samples / n_devices * (mpi_rank + 1)),
                           bs_valid):
                image = val_images[j:j + bs_valid]
                label = val_labels[j:j + bs_valid]
                if len(image) != bs_valid:  # note that smaller batch is ignored
                    continue
                input_image_valid["image"].d = image
                input_image_valid["label"].d = label
                error_valid.forward(clear_buffer=True)
                ve_local += error_valid.d.copy()
                k += 1
            ve_local /= k
            ve.d = ve_local
            comm.all_reduce(ve.data, division=True, inplace=True)

            # Save model
            if mpi_rank == 0:
                monitor_verr.add(i * n_devices, ve.d.copy())
                monitor_vtime.add(i * n_devices)
                if model_save_interval <= 0:
                    nn.save_parameters(os.path.join(
                        args.model_save_path, 'params_%06d.h5' % i))
                    save_checkpoint(args.model_save_path, i, solver)
                    model_save_interval += int(
                        args.model_save_interval / n_devices)
        model_save_interval -= 1

        # Forward/Zerograd
        image, label = tdata.next()
        input_image_train["image"].d = image
        input_image_train["label"].d = label
        loss_error_train.forward(clear_no_need_grad=True)
        solver.zero_grad()

        # Backward/AllReduce
        backward_and_all_reduce(
            loss_error_train, comm, with_all_reduce_callback=args.with_all_reduce_callback)

        # Solvers update
        solver.update()

        # Linear Warmup
        if i <= warmup_iter:
            lr = base_lr + warmup_slope * i
            solver.set_learning_rate(lr)

        if mpi_rank == 0:  # loss and error locally, and elapsed time
            monitor_loss.add(i * n_devices, loss_train.d.copy())
            monitor_err.add(i * n_devices, error_train.d.copy())
            monitor_time.add(i * n_devices)

        # exit(0)

    if mpi_rank == 0:
        nn.save_parameters(os.path.join(
            args.model_save_path,
            'params_%06d.h5' % (args.max_iter / n_devices)))
    comm.barrier()


if __name__ == '__main__':
    """
    Call this script with `mpirun` or `mpiexec`

    $ mpirun -n 4 python multi_device_multi_process.py --context "cudnn" -bs 64

    """
    train()

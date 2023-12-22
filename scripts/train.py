import hashlib
import os
import sys
from typing import Any, Dict
import random

import numpy as np
import gin
import pytorch_lightning as pl
import torch
from absl import flags
from torch.utils.data import DataLoader

import rave
import rave.core
import rave.dataset

# fix torch device order to be same as nvidia-smi order
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"

FLAGS = flags.FLAGS

flags.DEFINE_string('name', None, help='Name of the run', required=True)
flags.DEFINE_multi_string('config',
                          default='v2.gin',
                          help='RAVE configuration to use')
flags.DEFINE_string('db_path',
                    None,
                    help='Preprocessed dataset path',
                    required=True)
flags.DEFINE_integer('max_steps',
                     6000000,
                     help='Maximum number of training steps')
flags.DEFINE_integer('val_every', 10000, help='Checkpoint model every n steps')
flags.DEFINE_integer('n_signal',
                     126976,
                     help='Number of audio samples to use during training')
flags.DEFINE_integer('batch', 8, help='Batch size')
flags.DEFINE_string('ckpt',
                    None,
                    help='Path to checkpoint to continue training from')
flags.DEFINE_string('transfer_ckpt',
                    None,
                    help='Path to checkpoint to initialize weights from')
flags.DEFINE_multi_string('override', default=[], help='Override gin binding')
flags.DEFINE_integer('workers',
                     default=8,
                     help='Number of workers to spawn for dataset loading')
flags.DEFINE_multi_integer('gpu', default=None, help='GPU to use')
flags.DEFINE_integer('seed', 0, help='random seed')
flags.DEFINE_bool('derivative',
                  default=False,
                  help='Train RAVE on the derivative of the signal')
flags.DEFINE_bool('normalize',
                  default=False,
                  help='Train RAVE on normalized signals')
flags.DEFINE_float('speed_semitones',
                  default=0,
                  help='speed change data augmentation')
flags.DEFINE_float('gain_db',
                  default=0,
                  help='gain change data augmentation')
flags.DEFINE_float('allpass_p',
                  default=0.8,
                  help='chance of allpass filter data augmentation')
flags.DEFINE_float('eq_p',
                  default=0,
                  help='chance of random EQ data augmentation')
flags.DEFINE_float('delay_p',
                  default=0,
                  help='chance of random comb delay data augmentation')
flags.DEFINE_float('distort_p',
                  default=0,
                  help='chance of random distortion data augmentation')
flags.DEFINE_float('ema',
                   default=None,
                   help='Exponential weight averaging factor (optional)')
flags.DEFINE_bool('progress',
                  default=True,
                  help='Display training progress bar')


class EMA(pl.Callback):

    def __init__(self, factor=.999) -> None:
        super().__init__()
        self.weights = {}
        self.factor = factor

    def on_train_batch_end(self, trainer, pl_module, outputs, batch,
                           batch_idx) -> None:
        for n, p in pl_module.named_parameters():
            if n not in self.weights:
                self.weights[n] = p.data.clone()
                continue

            self.weights[n] = self.weights[n] * self.factor + p.data * (
                1 - self.factor)

    def swap_weights(self, module):
        for n, p in module.named_parameters():
            current = p.data.clone()
            p.data.copy_(self.weights[n])
            self.weights[n] = current

    def on_validation_epoch_start(self, trainer, pl_module) -> None:
        if self.weights:
            self.swap_weights(pl_module)
        else:
            print("no ema weights available")

    def on_validation_epoch_end(self, trainer, pl_module) -> None:
        if self.weights:
            self.swap_weights(pl_module)
        else:
            print("no ema weights available")

    def state_dict(self) -> Dict[str, Any]:
        return self.weights.copy()

    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        self.weights.update(state_dict)


def add_gin_extension(config_name: str) -> str:
    if config_name[-4:] != '.gin':
        config_name += '.gin'
    return config_name


def main(argv):
    random.seed(FLAGS.seed)
    np.random.seed(FLAGS.seed)
    torch.manual_seed(FLAGS.seed)

    torch.set_float32_matmul_precision('high')
    torch.backends.cudnn.benchmark = True
    gin.parse_config_files_and_bindings(
        map(add_gin_extension, FLAGS.config),
        FLAGS.override,
    )

    model = rave.RAVE()

    print(model)

    if FLAGS.derivative:
        model.integrator = rave.dataset.get_derivator_integrator(model.sr)[1]

    dataset = rave.dataset.get_dataset(FLAGS.db_path,
                                       model.sr,
                                       FLAGS.n_signal,
                                       derivative=FLAGS.derivative,
                                       normalize=FLAGS.normalize,
                                       speed_semitones=FLAGS.speed_semitones,
                                       gain_db=FLAGS.gain_db,
                                       allpass_p=FLAGS.allpass_p,
                                       eq_p=FLAGS.eq_p,
                                       delay_p=FLAGS.delay_p,
                                       distort_p=FLAGS.distort_p,
                                       )
    train, val = rave.dataset.split_dataset(dataset, 98)
    num_workers = FLAGS.workers

    if os.name == "nt" or sys.platform == "darwin":
        num_workers = 0

    train = DataLoader(train,
                       FLAGS.batch,
                       True,
                       drop_last=True,
                       num_workers=num_workers)
    val = DataLoader(val, FLAGS.batch, False, num_workers=num_workers)

    # CHECKPOINT CALLBACKS
    validation_checkpoint = pl.callbacks.ModelCheckpoint(monitor="validation",
                                                         filename="best")
    last_checkpoint = pl.callbacks.ModelCheckpoint(filename="last")

    val_check = {}
    if len(train) >= FLAGS.val_every:
        val_check["val_check_interval"] = FLAGS.val_every
    else:
        nepoch = FLAGS.val_every // len(train)
        val_check["check_val_every_n_epoch"] = nepoch

    gin_hash = hashlib.md5(
        gin.operative_config_str().encode()).hexdigest()[:10]

    RUN_NAME = f'{FLAGS.name}_{gin_hash}'

    os.makedirs(os.path.join("runs", RUN_NAME), exist_ok=True)

    if FLAGS.gpu == [-1]:
        gpu = 0
    else:
        gpu = FLAGS.gpu or rave.core.setup_gpu()

    print('selected gpu:', gpu)

    accelerator = None
    devices = None
    if FLAGS.gpu == [-1]:
        pass
    elif torch.cuda.is_available():
        accelerator = "cuda"
        devices = FLAGS.gpu or rave.core.setup_gpu()
    elif torch.backends.mps.is_available():
        print(
            "Training on mac is not available yet. Use --gpu -1 to train on CPU (not recommended)."
        )
        exit()
        accelerator = "mps"
        devices = 1

    callbacks = [
        validation_checkpoint,
        last_checkpoint,
        rave.model.WarmupCallback(),
        rave.model.QuantizeCallback(),
        rave.core.LoggerCallback(rave.core.ProgressLogger(RUN_NAME)),
        rave.model.BetaWarmupCallback(),
    ]

    if FLAGS.ema is not None:
        callbacks.append(EMA(FLAGS.ema))

    trainer = pl.Trainer(
        logger=pl.loggers.TensorBoardLogger(
            "runs",
            name=RUN_NAME,
        ),
        accelerator=accelerator,
        devices=devices,
        callbacks=callbacks,
        max_epochs=100000,
        max_steps=FLAGS.max_steps,
        profiler="simple",
        enable_progress_bar=FLAGS.progress,
        **val_check,
    )

    run = rave.core.search_for_run(FLAGS.ckpt)
    if run is not None:
        step = torch.load(run, map_location='cpu')["global_step"]
        trainer.fit_loop.epoch_loop._batches_that_stepped = step

    transfer_run = rave.core.search_for_run(FLAGS.transfer_ckpt)
    if transfer_run is not None:
        print(f'transferring weights from {transfer_run}')
        sd = torch.load(transfer_run, map_location='cpu')["state_dict"]
        msd = model.state_dict()
        for k in list(sd):
            if k not in model.state_dict() or sd[k].shape != msd[k].shape:
                print(f'skipping {k}')
                sd.pop(k)


        model.load_state_dict(sd, strict=False)

    with open(os.path.join("runs", RUN_NAME, "config.gin"), "w") as config_out:
        config_out.write(gin.operative_config_str())

    trainer.fit(model, train, val, ckpt_path=run)

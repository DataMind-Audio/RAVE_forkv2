import logging
import math
import random
import os
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logging.info("library loading")
logging.info("DEBUG")
import torch

torch.set_grad_enabled(False)

# import cached_conv as cc
import gin
import nn_tilde
import numpy as np
import torch.nn as nn
# import torch.nn.functional as F
from absl import flags

import rave
import rave.dataset

FLAGS = flags.FLAGS

flags.DEFINE_string('from_model',
                    default=None,
                    help='Path to the exported model with the reference latent space',
                    required=True)
flags.DEFINE_string('to_model',
                    default=None,
                    help='Path to the exported model to adapt',
                    required=True)
flags.DEFINE_string('db_path',
                    None,
                    help='Preprocessed dataset path',
                    required=True)
flags.DEFINE_integer('n_signal',
                     126976,
                     help='Number of audio samples to use during training')
flags.DEFINE_string('name', None, help='Name to export under', required=True)
flags.DEFINE_integer('seed', 0, help='random seed')
flags.DEFINE_float('data_gain', 0, help='random gain when fitting')

flags.DEFINE_bool('bias', False, 
                  help='whether to fit a bias parameter')
flags.DEFINE_float('clip', None, 
                  help='what value to clip extreme values of latents to before fitting')

flags.DEFINE_bool('data_normalize', False, 
                  help='should match setting during RAVE training')
flags.DEFINE_bool('data_derivative', False,
                  help='should match setting during RAVE training')

class Adapter(nn_tilde.Module):

    def __init__(self,
                 data: torch.Tensor,
                 from_model: nn_tilde.Module,
                 to_model: nn_tilde.Module,
                 bias = False,
                 clip = None
                 ) -> None:
        super().__init__()

        ratio_encode = from_model.encode_params[3]
        assert ratio_encode == from_model.encode_params[3]

        ### compute mapping from models + dataset
        trim = 20
        def encode(model, data, temp=0):
            with torch.no_grad():
                batches = data.split(8)
                z = torch.cat([
                    model.encode(b, temp=temp)[:,:,trim:]
                    for b in batches])
                z = z.permute(0,2,1).reshape(-1,z.shape[1])
                return z
        z_from = encode(from_model, data)
        z_to = encode(to_model, data)

        if bias:
            b = z_to.mean(0)
        else:
            b = torch.zeros(z_to.shape[1])

        if clip is not None:
            z_from = z_from.clip(-clip, clip)
            z_to = z_to.clip(-clip, clip)

        w = torch.linalg.lstsq(z_from, z_to - b).solution
        
        self.register_buffer('weight', w)
        self.register_buffer('bias', b)

        self.register_method(
            "forward",
            in_channels=from_model.latent_size,
            in_ratio=ratio_encode,
            out_channels=to_model.latent_size,
            out_ratio=ratio_encode,
            input_labels=[
                f'(signal) Input latent {i}'
                for i in range(from_model.latent_size)
            ],
            output_labels=[
                f'(signal) Adapted latent {i}'
                for i in range(to_model.latent_size)
            ],
        )

    def forward(self, x):
        return (x.permute(0,2,1) @ self.weight + self.bias).permute(0,2,1)


def main(argv):
    random.seed(FLAGS.seed)
    np.random.seed(FLAGS.seed)
    torch.manual_seed(FLAGS.seed)

    logging.info("loading models")

    from_model = torch.jit.load(FLAGS.from_model)
    to_model = torch.jit.load(FLAGS.to_model)

    assert from_model.n_channels == to_model.n_channels

    logging.info("preparing data")

    dataset = rave.dataset.get_dataset(
        os.path.normpath(FLAGS.db_path),
        from_model.sr,
        FLAGS.n_signal,
        derivative=FLAGS.data_derivative,
        normalize=FLAGS.data_normalize,
        gain_db=FLAGS.data_gain,
        n_channels=from_model.n_channels
        )
    
    train, val = rave.dataset.split_dataset(dataset, 98)
    rng = np.random.default_rng(FLAGS.seed)
    idx = rng.choice(len(train), size=32)
    data = torch.stack([torch.from_numpy(train[i]) for i in idx])

    logging.info(f'{data.shape=}')

    logging.info("creating adapter model")

    scripted_adapter = Adapter(
        data,
        from_model,
        to_model,
        bias=FLAGS.bias,
        clip=FLAGS.clip
    )

    logging.info("save model")
    model_name = os.path.basename(FLAGS.name)
    if FLAGS.bias:
        model_name += "_bias"
    if FLAGS.clip is not None:
        model_name += f"_clip"
    model_name += ".ts"

    out_path = model_name
    scripted_adapter.export_to_ts(out_path)



    logging.info(
        f"all good ! model exported to {out_path}")

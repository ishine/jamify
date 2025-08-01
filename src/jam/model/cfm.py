# Copyright (c) 2025 ASLP-LAB
#               2025 Ziqian Ning   (ningziqian@mail.nwpu.edu.cn)
#               2025 Huakang Chen  (huakang@mail.nwpu.edu.cn)
#               2025 Guobin Ma     (guobin.ma@mail.nwpu.edu.cn)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" This implementation is adapted from github repo:
    https://github.com/SWivid/F5-TTS.
"""

from __future__ import annotations
from typing import Callable
from random import random

import torch
from torch import nn
import torch
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
from tqdm import tqdm

from torchdiffeq import odeint

from .utils import (
    exists,
    list_str_to_idx,
    list_str_to_tensor,
    lens_to_mask,
    mask_from_frac_lengths,
)

def custom_mask_from_start_end_indices(
    seq_len: int["b"],  # noqa: F821
    latent_pred_segments,
    device,
    max_seq_len
):
    max_seq_len = max_seq_len
    seq = torch.arange(max_seq_len, device=device).long()

    res_mask = torch.zeros(max_seq_len, device=device, dtype=torch.bool)
    
    for start, end in latent_pred_segments:
        start = start.unsqueeze(0)
        end = end.unsqueeze(0)
        start_mask = seq[None, :] >= start[:, None]
        end_mask = seq[None, :] < end[:, None]
        res_mask = res_mask | (start_mask & end_mask)
    
    return res_mask

class CFM(nn.Module):
    def __init__(
        self,
        transformer: nn.Module,
        sigma=0.0,
        odeint_kwargs: dict = dict(
            method="euler"
        ),
        odeint_options: dict = dict(
            min_step=0.05
        ),
        audio_drop_prob=0.3,
        cond_drop_prob=0.2,
        style_drop_prob=0.1,
        lrc_drop_prob=0.1,
        dual_drop_prob=None,
        no_cond_drop=False,
        num_channels=None,
        frac_lengths_mask: tuple[float, float] = (0.7, 1.0),
        vocab_char_map: dict[str:int] | None = None,
        max_frames=2048,
        no_edit=False
    ):
        super().__init__()

        self.frac_lengths_mask = frac_lengths_mask

        self.num_channels = num_channels

        # classifier-free guidance
        self.audio_drop_prob = audio_drop_prob
        self.cond_drop_prob = cond_drop_prob
        self.style_drop_prob = style_drop_prob
        self.lrc_drop_prob = lrc_drop_prob
        self.dual_drop_prob = dual_drop_prob
        if self.dual_drop_prob is not None:
            print(f"Dual drop prob: {self.dual_drop_prob}")
        self.no_cond_drop = no_cond_drop
        if self.no_cond_drop:
            print("No conditional dropout")

        # transformer
        self.transformer = transformer
        dim = transformer.dim
        self.dim = dim

        # conditional flow related
        self.sigma = sigma

        # sampling related
        self.odeint_kwargs = odeint_kwargs
        
        self.odeint_options = odeint_options

        # vocab map for tokenization
        self.vocab_char_map = vocab_char_map
        
        self.max_frames = max_frames
        self.no_edit = no_edit

    @property
    def device(self):
        return next(self.parameters()).device

    @torch.no_grad()
    def sample(
        self,
        cond: float["b n d"] | float["b nw"],  # noqa: F722
        text: int["b nt"] | list[str],  # noqa: F722
        *,
        style_prompt = None,
        duration_abs=None,
        duration_rel=None,
        negative_style_prompt = None,
        lens: int["b"] | None = None,  # noqa: F821
        steps=32,
        cfg_strength=4.0,
        dual_cfg: tuple | None =None,
        fix_dual_cfg: bool = False,
        sway_sampling_coef=None,
        seed: int | None = None,
        vocoder: Callable[[float["b d n"]], float["b nw"]] | None = None,  # noqa: F722
        t_inter=0.1,
        edit_mask=None,
        start_time=None,
        cfg_range: tuple|None =None,
        latent_pred_segments=None,
        batch_infer_num=1,
    ):
        self.eval()
        batch_size = cond.shape[0]

        if next(self.parameters()).dtype == torch.float16:
            cond = cond.half()

        # raw wave
        if cond.shape[1] > self.max_frames:
            cond = cond[:, :self.max_frames, :]

        if cond.ndim == 2:
            cond = self.mel_spec(cond)
            cond = cond.permute(0, 2, 1)
            assert cond.shape[-1] == self.num_channels

        batch, cond_seq_len, device = *cond.shape[:2], cond.device
        if not exists(lens):
            lens = torch.full((batch,), cond_seq_len, device=device, dtype=torch.long)

        # text
        if isinstance(text, list):
            if exists(self.vocab_char_map):
                text = list_str_to_idx(text, self.vocab_char_map).to(device)
            else:
                text = list_str_to_tensor(text).to(device)
            assert text.shape[0] == batch

        # duration
        cond_mask = lens_to_mask(lens)
        if edit_mask is not None:
            cond_mask = cond_mask & edit_mask

        latent_pred_segments = torch.tensor(latent_pred_segments).to(cond.device)
        fixed_span_mask = custom_mask_from_start_end_indices(cond_seq_len, latent_pred_segments, device=cond.device, max_seq_len=self.max_frames)
        fixed_span_mask = fixed_span_mask.unsqueeze(-1)
        step_cond = torch.where(fixed_span_mask, torch.zeros_like(cond), cond)

        cond = cond.repeat(batch_infer_num, 1, 1)
        step_cond = step_cond.repeat(batch_infer_num, 1, 1)
        text = text.repeat(batch_infer_num, 1)
        style_prompt = style_prompt.repeat(batch_infer_num, 1)
        negative_style_prompt = negative_style_prompt.repeat(batch_infer_num, 1)
        start_time = start_time.repeat(batch_infer_num)
        duration_abs = duration_abs.repeat(batch_infer_num)
        duration_rel = duration_rel.repeat(batch_infer_num)
        fixed_span_mask = fixed_span_mask.repeat(batch_infer_num, 1, 1)

        # Initialize progress bar for sampling steps
        pbar = tqdm(total=steps, desc="Sampling", unit="step")

        def fn(t, x):
            # Update progress bar with current time value
            pbar.set_postfix({"t": f"{t.item():.3f}"})
            pbar.update(1)
            
            # predict flow
            pred = self.transformer(
                x=x, cond=step_cond, text=text, time=t, drop_audio_cond=False, drop_text=False, drop_prompt=False,
                style_prompt=style_prompt, start_time=start_time, duration_abs=duration_abs, duration_rel=duration_rel
            )
            if cfg_range is not None and (t < cfg_range[0] or t > cfg_range[1]):
                return pred

            if dual_cfg is not None:
                if not fix_dual_cfg:
                    phoneme_pred = self.transformer(
                        x=x, cond=step_cond, text=text, time=t, drop_audio_cond=False, drop_text=False, drop_prompt=False,
                        style_prompt=negative_style_prompt, start_time=start_time, duration_abs=duration_abs, duration_rel=duration_rel
                    )
                    null_pred = self.transformer(
                        x=x, cond=step_cond, text=text, time=t, drop_audio_cond=True, drop_text=True, drop_prompt=False,
                        style_prompt=negative_style_prompt, start_time=start_time, duration_abs=duration_abs, duration_rel=duration_rel
                    )
                    return dual_cfg[0] * (pred-phoneme_pred) + dual_cfg[1] * (phoneme_pred-null_pred) + null_pred
                else:
                    style_pred = self.transformer(
                        x=x, cond=step_cond, text=text, time=t, drop_audio_cond=False, drop_text=True, drop_prompt=False,
                        style_prompt=style_prompt, start_time=start_time, duration_abs=duration_abs, duration_rel=duration_rel
                    )
                    null_pred = self.transformer(
                        x=x, cond=step_cond, text=text, time=t, drop_audio_cond=True, drop_text=True, drop_prompt=False,
                        style_prompt=negative_style_prompt, start_time=start_time, duration_abs=duration_abs, duration_rel=duration_rel
                    )
                    return dual_cfg[0] * (style_pred-null_pred) + dual_cfg[1] * (pred-style_pred) + null_pred
            else:
                if cfg_strength < 1e-5:
                    return pred

                null_pred = self.transformer(
                    x=x, cond=step_cond, text=text, time=t, drop_audio_cond=True, drop_text=True, drop_prompt=False,
                    style_prompt=negative_style_prompt, start_time=start_time, duration_abs=duration_abs, duration_rel=duration_rel
                )
                return pred + (pred - null_pred) * cfg_strength

        # noise input
        # to make sure batch inference result is same with different batch size, and for sure single inference
        # still some difference maybe due to convolutional layers
        # y0 = []
        # for dur in duration:
        #     if exists(seed):
        #         torch.manual_seed(seed)
        #     y0.append(torch.randn(dur, self.num_channels, device=self.device, dtype=step_cond.dtype))
        # y0 = pad_sequence(y0, padding_value=0, batch_first=True)

        # t_start = 0

        y0 = torch.randn(cond.shape[0], self.max_frames, self.num_channels, device=self.device, dtype=step_cond.dtype)
        t_start = 0

        t = torch.linspace(t_start, 1, steps, device=self.device, dtype=step_cond.dtype)
        if sway_sampling_coef is not None:
            t = t + sway_sampling_coef * (torch.cos(torch.pi / 2 * t) - 1 + t)

        trajectory = odeint(fn, y0, t, **self.odeint_kwargs)
        
        # Close progress bar
        pbar.close()

        sampled = trajectory[-1]
        out = sampled
        # out = torch.where(fixed_span_mask, out, cond)

        if exists(vocoder):
            out = out.permute(0, 2, 1)
            out = vocoder(out)

        out = torch.chunk(out, batch_infer_num, dim=0)
        return out, trajectory

    def forward(
        self,
        inp: float["b n d"] | float["b nw"],  # mel or raw wave  # noqa: F722
        text: int["b nt"] | list[str],  # noqa: F722
        style_prompt = None,
        lens: int["b"] | None = None,  # noqa: F821
        start_time = None,
        duration_abs = None,
        duration_rel = None,
    ):

        batch, seq_len, dtype, device, _σ1 = *inp.shape[:2], inp.dtype, self.device, self.sigma

        # lens and mask
        if not exists(lens):
            lens = torch.full((batch,), seq_len, device=device)

        mask = lens_to_mask(lens, length=seq_len)  # useless here, as collate_fn will pad to max length in batch

        # get a random span to mask out for training conditionally
        frac_lengths = torch.zeros((batch,), device=self.device).float().uniform_(*self.frac_lengths_mask)
        rand_span_mask = mask_from_frac_lengths(lens, frac_lengths, self.max_frames)

        if exists(mask):
            rand_span_mask = mask

        # mel is x1
        x1 = inp

        # x0 is gaussian noise
        x0 = torch.randn_like(x1)

        # time step
        time = torch.normal(mean=0, std=1, size=(batch,), device=self.device)
        time = torch.nn.functional.sigmoid(time)
        # TODO. noise_scheduler

        # sample xt (φ_t(x) in the paper)
        t = time.unsqueeze(-1).unsqueeze(-1)
        φ = (1 - t) * x0 + t * x1
        flow = x1 - x0

        # only predict what is within the random mask span for infilling
        cond = torch.where(rand_span_mask[..., None], torch.zeros_like(x1), x1)

        if self.dual_drop_prob is not None:
            drop_prompt = random() < self.dual_drop_prob[0]
            drop_text = drop_prompt and (random() < self.dual_drop_prob[1])
        else:
            drop_text = random() < self.lrc_drop_prob
            drop_prompt = random() < self.style_drop_prob
        if self.no_cond_drop:
            drop_text = False
            drop_prompt = False

        # transformer and cfg training with a drop rate
        drop_audio_cond = random() < self.audio_drop_prob  # p_drop in voicebox paper
        if self.no_edit:
            drop_audio_cond = True

        # if want rigourously mask out padding, record in collate_fn in dataset.py, and pass in here
        # adding mask will use more memory, thus also need to adjust batchsampler with scaled down threshold for long sequences
        pred = self.transformer(
            x=φ, cond=cond, text=text, time=time, drop_audio_cond=drop_audio_cond, drop_text=drop_text, drop_prompt=drop_prompt,
            style_prompt=style_prompt, start_time=start_time, duration_abs=duration_abs, duration_rel=duration_rel
        )

        # flow matching loss
        loss = F.mse_loss(pred, flow, reduction="none")
        if not self.no_edit:
            loss = loss[rand_span_mask]

        return loss.mean(), cond, pred

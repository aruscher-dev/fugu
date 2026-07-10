"""Hand-rolled Singular Value Fine-tuning (SVF), per PLAN.md's Architecture
section: peft 0.19.1 has no SVF support -- all of its adapters (LoRA,
AdaLoRA, IA3, ...) add a new low-rank delta-W rather than reparameterizing
and freezing the base weight's own SVD. Mirrors SakanaAI's real
Transformer^2 codebase (`SakanaAI/self-adaptive-llms`), the precedent
PLAN.md names: one-time `torch.linalg.svd` per target `nn.Linear`, freeze
`U`/`S`/`Vh` as buffers, train only a per-singular-value scale vector `z`
(initialized to all-ones, so swapping this in for the original Linear is a
no-op until `z` is trained away from 1).
"""
from __future__ import annotations

from typing import List, Tuple

import torch
import torch.nn as nn


class SVFLinear(nn.Module):
    """Drop-in replacement for one `nn.Linear`: effective weight is
    `U @ diag(S * z) @ Vh`. `U`/`S`/`Vh` are frozen buffers; `z` (and the
    bias, if any) are the only parameters left trainable on this module."""

    def __init__(self, linear: nn.Linear):
        super().__init__()
        self.in_features = linear.in_features
        self.out_features = linear.out_features

        orig_dtype = linear.weight.dtype
        weight_f32 = linear.weight.data.to(torch.float32)
        U, S, Vh = torch.linalg.svd(weight_f32, full_matrices=False)
        self.register_buffer("U", U.to(orig_dtype))
        self.register_buffer("S", S.to(orig_dtype))
        self.register_buffer("Vh", Vh.to(orig_dtype))
        self.z = nn.Parameter(torch.ones(S.shape[0], dtype=torch.float32, device=S.device))

        self.bias = linear.bias
        if self.bias is not None:
            self.bias.requires_grad_(False)  # SFT trains z + the selection head only, per PLAN.md

    def effective_weight(self) -> torch.Tensor:
        scaled_s = (self.S.to(torch.float32) * self.z).to(self.U.dtype)
        return (self.U * scaled_s.unsqueeze(0)) @ self.Vh

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return nn.functional.linear(x, self.effective_weight(), self.bias)


def _get_decoder_layers(model: nn.Module) -> List[nn.Module]:
    """Qwen2/Llama-family causal LMs (this project's orchestrator-backbone
    candidates -- Qwen2.5-1.5B/3B-Instruct) expose the decoder stack as
    `model.model.layers`. Not a general solution for arbitrary
    architectures, deliberately -- PLAN.md fixes the backbone family."""
    inner = getattr(model, "model", model)
    layers = getattr(inner, "layers", None)
    if layers is None:
        raise AttributeError(
            f"{type(model).__name__} has no `.model.layers` -- SVF's decoder-layer "
            f"lookup assumes a Qwen2/Llama-style causal LM (see PLAN.md's orchestrator "
            f"backbone candidates)."
        )
    return list(layers)


DEFAULT_SVF_TARGETS: Tuple[str, ...] = ("self_attn.o_proj", "mlp.down_proj")


def apply_svf(model: nn.Module, n_last_layers: int = 3,
              target_names: Tuple[str, ...] = DEFAULT_SVF_TARGETS) -> List[SVFLinear]:
    """Replace `o_proj`/`down_proj` in the last `n_last_layers` decoder
    layers with `SVFLinear`, in place. Returns the created modules -- their
    `.z` parameters (plus the selection head's own parameters) are exactly
    what Phase 4's SFT training loop optimizes; every other backbone
    parameter stays frozen."""
    layers = _get_decoder_layers(model)
    target_layers = layers[-n_last_layers:] if n_last_layers > 0 else []
    created: List[SVFLinear] = []
    for layer in target_layers:
        for dotted_name in target_names:
            *parents, leaf = dotted_name.split(".")
            parent = layer
            for p in parents:
                parent = getattr(parent, p)
            original = getattr(parent, leaf)
            if isinstance(original, SVFLinear):
                continue  # idempotent re-apply (e.g. a resumed/re-run training script)
            setattr(parent, leaf, SVFLinear(original))
            created.append(getattr(parent, leaf))
    return created

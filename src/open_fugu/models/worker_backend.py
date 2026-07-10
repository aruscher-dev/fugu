"""The Fugu orchestrator's backbone, per PLAN.md's Architecture section: a
small open model (Qwen2.5-1.5B-Instruct, falling back to -3B if 1.5B's
hidden states don't discriminate workers well) computes a hidden state from
the running blindfold-chess prompt, and a lightweight selection head
(`nn.Linear(hidden_size, L)`) turns the last-token hidden state into logits
over the L candidate workers. SVF (models/svf.py) adapts the backbone's last
2-3 layers; the backbone's remaining parameters stay frozen throughout.

This is the model Phase 4 trains (SFT: selection head + SVF's `z` vectors)
and Phase 5+ would use for real per-query dispatch -- NOT one of the worker
LLMs themselves (see models/local_worker.py for those, which this module
never imports: worker generation and orchestrator routing are deliberately
separate models/processes, matching the paper's backbone-vs-worker-pool
split).
"""
from __future__ import annotations

import os

# Same reasoning/ordering requirement as models/local_worker.py -- must be
# set before any huggingface_hub/transformers import.
os.environ.setdefault("HF_HOME", "/Data/.hf_cache")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from dataclasses import dataclass, field
from typing import Iterator, List

import torch
import torch.nn as nn
from transformers import AutoModelForCausalLM, AutoTokenizer

from open_fugu.models.svf import SVFLinear, apply_svf

DEFAULT_BACKBONE_MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"


@dataclass
class OrchestratorBackboneConfig:
    worker_ids: List[str] = field(default_factory=list)  # fixed order == selection-head output order
    backbone_model_id: str = DEFAULT_BACKBONE_MODEL_ID
    svf_n_last_layers: int = 3
    device: str = "cuda:0"
    dtype: torch.dtype = torch.bfloat16


class OrchestratorBackbone(nn.Module):
    """Frozen backbone LM + SVF-adapted last few layers (trainable `z`) +
    selection head (trainable `nn.Linear`) -> logits over
    `config.worker_ids`, in that fixed order."""

    def __init__(self, config: OrchestratorBackboneConfig):
        super().__init__()
        if not config.worker_ids:
            raise ValueError("OrchestratorBackboneConfig.worker_ids must be non-empty")
        self.config = config
        self.tokenizer = AutoTokenizer.from_pretrained(config.backbone_model_id)
        self.backbone = AutoModelForCausalLM.from_pretrained(
            config.backbone_model_id,
            torch_dtype=config.dtype,
            device_map=config.device,
        )
        self.backbone.eval()
        for p in self.backbone.parameters():
            p.requires_grad_(False)

        self.svf_linears: List[SVFLinear] = apply_svf(self.backbone, n_last_layers=config.svf_n_last_layers)

        hidden_size = self.backbone.config.hidden_size
        self.selection_head = nn.Linear(hidden_size, len(config.worker_ids), bias=True)
        self.selection_head.to(device=self.backbone.device, dtype=torch.float32)

    def trainable_parameters(self) -> Iterator[nn.Parameter]:
        """Exactly the parameters Phase 4's SFT loop should optimize --
        every other backbone weight stays frozen (see __init__)."""
        for m in self.svf_linears:
            yield m.z
        yield from self.selection_head.parameters()

    def forward(self, prompt: str) -> torch.Tensor:
        """One blindfold-chess prompt in (the same `format_opening_prompt`
        text harness.py builds for a worker), un-normalized logits (shape
        `[len(worker_ids)]`) over `config.worker_ids` out. Caller decides
        what to do with them -- train_sft.py trains against a soft target
        via KL/cross-entropy; real dispatch would argmax."""
        # Deliberately NOT wrapped in torch.no_grad(): every backbone weight
        # except the SVF z vectors has requires_grad=False (see __init__),
        # but autograd still needs to build the graph through those SVFLinear
        # layers so gradients can reach z during training. no_grad() here
        # would silently zero out Phase 4's whole SVF half of the SFT loss.
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.backbone.device)
        hidden_states = self.backbone.model(**inputs).last_hidden_state
        last_token_hidden = hidden_states[0, -1, :].to(torch.float32)
        return self.selection_head(last_token_hidden)

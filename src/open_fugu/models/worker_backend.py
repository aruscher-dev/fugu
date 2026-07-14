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


def load_from_checkpoint(checkpoint_path, available_worker_ids: List[str], device: str = "cuda:0") -> "OrchestratorBackbone":
    """Loads a trained OrchestratorBackbone from a checkpoint file, shared by
    every caller that needs to go from "path on disk" to "backbone ready to
    forward() prompts through": a2a.orchestrator_agent.FuguSelectionDispatch
    (real A2A per-query dispatch, Phase 5+) and m7's in-process fixed eval
    suite (checkpoints/m5_sft/backbone_head_svf.pt and
    checkpoints/m6_cmaes/selection_head.pt) both need the exact same
    load-and-wire-up logic. Previously duplicated inline in
    FuguSelectionDispatch.__init__ before m7's session factored it out here.

    Two checkpoint shapes exist: train_sft.py's (Phase 4/m5, SFT-trained --
    "selection_head", "worker_ids", "backbone_model_id", "svf_n_last_layers",
    "svf_z") and train_cmaes.py-driven pilots' (Phase 8/m6/m7's own
    "m6_cmaes" condition -- same keys but WITHOUT "svf_z", since CMA-ES only
    evolves the selection head and leaves SVF's `z` frozen at its
    freshly-constructed no-op default). "svf_z" is loaded only if present --
    a freshly-constructed OrchestratorBackbone already has the right no-op
    `z` values otherwise, so omitting it is not an error.
    """
    import torch

    state = torch.load(checkpoint_path, map_location=device)
    checkpoint_worker_ids = state["worker_ids"]
    missing = [w for w in checkpoint_worker_ids if w not in available_worker_ids]
    if missing:
        raise ValueError(
            f"Checkpoint at {checkpoint_path} expects worker(s) {missing} but only "
            f"{list(available_worker_ids)} are available -- the selection head's output "
            f"order/size is fixed at training time (see OrchestratorBackboneConfig.worker_ids)."
        )

    config = OrchestratorBackboneConfig(
        worker_ids=checkpoint_worker_ids,
        backbone_model_id=state["backbone_model_id"],
        svf_n_last_layers=state["svf_n_last_layers"],
        device=device,
    )
    backbone = OrchestratorBackbone(config)
    backbone.selection_head.load_state_dict(state["selection_head"])
    if "svf_z" in state:
        for module, z in zip(backbone.svf_linears, state["svf_z"]):
            module.z.data = z.to(device=module.z.device, dtype=module.z.dtype)
    backbone.eval()
    return backbone

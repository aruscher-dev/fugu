"""A GTBench-compatible Model that wraps the Fugu orchestrator itself: on
every `query()` call it runs `OrchestratorBackbone`'s forward pass to pick
one worker from a fixed pool (real per-query dispatch, the same design
`a2a/orchestrator_agent.py`'s `FuguSelectionDispatch` uses for full chess --
argmax over the backbone's logits, `torch.no_grad()` inference-only), then
delegates the actual generation to that worker's `LocalTransformersModel`.

This is what `scripts/phase7_cmaes_kuhn_pilot.py`'s CMA-ES fitness function
plays games through -- the flat parameter vector CMA-ES evolves IS this
class's `backbone.selection_head` weight+bias (see `train_cmaes.py`'s
`flatten_params`/`unflatten_params`, and that script for how a candidate
vector gets loaded into `selection_head` before each fitness evaluation).

See `local_transformers_model.py`'s docstring for why this duck-types
`gamingbench.models.base_model.BaseModel` instead of importing/subclassing
it, and why it's constructed directly in Python rather than through
GTBench's YAML-config-driven `utils.load_model()`.
"""
from __future__ import annotations

from typing import Dict, List, Tuple


class OrchestratorRouterModel:
    """`config` needs: `.max_tokens`, `.timeout`, `.temperature`,
    `.nick_name` (same `BaseModel`-shape fields as `LocalTransformersModel`,
    forwarded to whichever worker gets picked -- see that class), plus:
      - `.worker_models`: `dict[str, LocalTransformersModel]`, iteration
        order fixed to match `.backbone`'s `config.worker_ids` (see
        `models/worker_backend.py`'s `OrchestratorBackboneConfig`).
      - `.backbone`: an already-constructed
        `open_fugu.models.worker_backend.OrchestratorBackbone`.
    Both are live Python objects, not YAML-loadable config fields -- this is
    exactly why `phase7_cmaes_kuhn_pilot.py` builds `OrchestratorRouterModel`
    directly (`OrchestratorRouterModel(config)`) rather than through
    GTBench's `utils.load_model()`, which only ever calls
    `getattr(models, model_config.model_type)(model_config)` with a `Box`
    parsed straight from a YAML file -- no live tensors possible there.
    """

    def __init__(self, config):
        self.max_tokens = config.max_tokens
        self.timeout = config.timeout
        self.temperature = config.temperature
        self.nick_name = config.nick_name

        self.worker_ids: List[str] = list(config.worker_models.keys())
        self.worker_models: Dict[str, object] = dict(config.worker_models)
        self.backbone = config.backbone
        if list(self.backbone.config.worker_ids) != self.worker_ids:
            raise ValueError(
                f"OrchestratorRouterModel's worker_models keys {self.worker_ids} must match "
                f"backbone.config.worker_ids {list(self.backbone.config.worker_ids)} exactly "
                f"(order included -- it's the selection head's output order)."
            )

        self.last_routed_worker: str = ""   # last query()'s pick -- eval/demo logging hook
        self.last_routing_logits: List[float] = []

    def query(self, messages: List[dict], n: int, stop, prompt_type: str) -> Tuple[List[str], int, int]:
        import torch

        prompt = _messages_to_prompt(messages)
        with torch.no_grad():
            logits = self.backbone.forward(prompt)
        short_id = self.worker_ids[int(torch.argmax(logits).item())]
        self.last_routed_worker = short_id
        self.last_routing_logits = logits.tolist()

        return self.worker_models[short_id].query(messages, n, stop, prompt_type)


def _messages_to_prompt(messages: List[dict]) -> str:
    """`OrchestratorBackbone.forward()` takes one plain string (see
    `models/worker_backend.py` -- it tokenizes with the backbone's own
    tokenizer directly, no chat template), but GTBench's agents build a
    role/content message list (`BaseAgent.construct_init_messages`, confirmed
    by reading `base_agent.py`). Flattens role-tagged, in order -- simple and
    deterministic; the backbone only needs *some* text signal correlated with
    the current game state to route on, not a faithfully-reconstructed chat
    template."""
    return "\n".join(f"[{m['role']}] {m['content']}" for m in messages)

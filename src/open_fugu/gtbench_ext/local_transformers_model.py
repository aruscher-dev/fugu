"""A GTBench-compatible Model wrapping one local open-weight worker LLM
(models/local_worker.py), so a real self-hosted worker can play a
`gtbench` (vendor/gtbench, `jinhaoduan/GTBench`, gitignored/host-specific
like every other vendored dep) game in place of GTBench's own remote-API-only
model (`vendor/gtbench/gamingbench/models/llm_model.py`'s `LLMModel` only
supports OpenAI/Anyscale/DeepInfra via `chat.py`'s `chat_llm`, confirmed by
reading that file directly -- no local-inference path exists upstream).

Duck-types `vendor/gtbench/gamingbench/models/base_model.py`'s `BaseModel`:
same constructor contract (`config.llm_model_path`/`.max_tokens`/`.timeout`/
`.temperature`/`.nick_name`) and the same
`query(messages, n, stop, prompt_type) -> (generations, completion_tokens,
prompt_tokens)` return shape `LLMModel.query()` has, confirmed by reading
that file too -- so gtbench's own agent/game-loop code
(`gamingbench.agents.base_agent.BaseAgent.llm_query`,
`gamingbench.games.openspiel_adapter.OpenSpielGame.play`) can use this
interchangeably with GTBench's remote-API model, zero game-loop changes
needed. Deliberately does NOT subclass
`gamingbench.models.base_model.BaseModel` (that import only resolves once
vendor/gtbench is actually cloned on the GPU host -- this module must stay
importable and `py_compile`-able even when it isn't, same reasoning
`minichess/engine.py` documents for not depending on `bin/fairy-stockfish`
at import time).

Constructed directly in Python by scripts/phase7_cmaes_kuhn_pilot.py (a
plain `types.SimpleNamespace` stands in for the `Box`-parsed YAML config
GTBench's own `utils.load_model()` would build) rather than through
GTBench's config-file-driven model loading -- see that script's own
docstring for why (this project's CMA-ES fitness function needs to swap
live parameter tensors in and out every generation; GTBench's config
loading path only ever reads static YAML).
"""
from __future__ import annotations

from typing import List, Tuple


class LocalTransformersModel:
    """`config` needs: `.max_tokens`, `.timeout` (unused here -- local
    inference has no network timeout, kept only for `BaseModel` config-shape
    parity), `.temperature`, `.nick_name`, and `.model_id` (an HF repo id or
    a `models.local_worker.CANDIDATE_WORKERS` short id)."""

    def __init__(self, config):
        self.model_path = getattr(config, "llm_model_path", None) or config.model_id
        self.max_tokens = config.max_tokens
        self.timeout = config.timeout
        self.temperature = config.temperature
        self.nick_name = config.nick_name

        from open_fugu.models.local_worker import CANDIDATE_WORKERS, LocalWorker, LocalWorkerConfig

        model_id = CANDIDATE_WORKERS.get(config.model_id, config.model_id)
        worker_config = LocalWorkerConfig(
            model_id=model_id,
            max_new_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        self.worker = LocalWorker(worker_config)

    def query(self, messages: List[dict], n: int, stop, prompt_type: str) -> Tuple[List[str], int, int]:
        assert prompt_type in ("move", "plan", "vote")
        generations = [self.worker.generate(messages) for _ in range(n)]
        # LocalWorker has no API metering to reconcile against (no remote
        # call). GTBench's own BaseAgent.llm_query only ever SUMS
        # completion_tokens+prompt_tokens into a Query's `token_size` for
        # history-log bookkeeping (confirmed by reading base_agent.py) -- it
        # never branches on the value -- so 0 is a safe, documented
        # placeholder rather than a guessed token count.
        return generations, 0, 0

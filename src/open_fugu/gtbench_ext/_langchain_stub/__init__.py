"""Why this exists: `vendor/gtbench` (gitignored, own git history, cloned by
scripts/phase7_setup_gtbench.sh) is `jinhaoduan/GTBench`. Its
`gamingbench/games/__init__.py` eagerly imports every game module (not just
the one this project needs, `kuhn_poker`), which chains through
`gamingbench/utils/utils.py` -> `gamingbench/models/__init__.py` ->
`gamingbench/models/base_model.py` -> `gamingbench/chat/chat.py`, which does
a bare, unconditional `from langchain.chat_models import ChatOpenAI,
ChatAnyscale` at module scope (confirmed by reading that file directly --
this is not a lazy/optional import). Real `langchain`/`langchain-community`
pin an old (Feb-2024-era) dependency stack GTBench's own requirements.txt
specifies -- installing it risks pydantic-version conflicts with this
project's already-validated torch/transformers/peft/trl/a2a-sdk stack (see
STATUS.md's Phase 0 notes on why those versions are pinned), for a code path
(`chat_llm()`, GTBench's remote-API model) this project's `gtbench_ext/`
never actually calls -- every generation here goes through
`LocalTransformersModel`/`OrchestratorRouterModel` instead.

So: this directory is a minimal same-named stub for exactly the handful of
symbols `gamingbench/chat/chat.py` imports at module scope (just enough to
make the import succeed; every stubbed class raises NotImplementedError if
anyone ever actually tries to instantiate it, which should never happen
here). `ensure_importable()` prepends this directory to `sys.path` -- ahead
of any real `langchain` that might exist elsewhere on the host -- before
`vendor/gtbench` gets imported anywhere. Call it once, before the first
`from gamingbench... import ...`; see scripts/phase7_cmaes_kuhn_pilot.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

_STUB_DIR = Path(__file__).resolve().parent


def ensure_importable() -> None:
    stub_path = str(_STUB_DIR)
    if stub_path not in sys.path:
        sys.path.insert(0, stub_path)

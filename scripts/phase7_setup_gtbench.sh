#!/usr/bin/env bash
# Phase 7 setup: idempotent clone of GTBench (jinhaoduan/GTBench upstream,
# the `gtbench` harness PLAN.md's Context section names) into vendor/gtbench
# (gitignored, own git history, re-clonable -- same pattern Phase 0's
# vendor/llm_chess and m0's bin/fairy-stockfish both follow) plus the two
# pure-Python/C-extension deps this project's gtbench_ext/ actually needs
# (`open_spiel`'s `pyspiel` for game legality/state, `python-box` for the
# YAML config Box objects GTBench's own game-config loader uses).
#
# Deliberately does NOT `pip install -r vendor/gtbench/requirements.txt` --
# that file pins an old langchain/gym/etc. stack for GTBench's own
# remote-API-only LLMModel (see gamingbench/chat/chat.py), which this
# project's gtbench_ext/ never calls (LocalTransformersModel bypasses it
# entirely) and which risks clobbering this venv's already-validated
# torch/transformers/peft/trl versions (see STATUS.md's Phase 0 notes on why
# those are pinned). Only the two packages gtbench_ext/ + the pilot script
# actually import get installed.
set -euo pipefail
umask 077

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_DIR="$PROJECT_DIR/vendor"
GTBENCH_DIR="$VENDOR_DIR/gtbench"
VENV_PYTHON="/Data/.venv/bin/python3"
UV_BIN="$HOME/.local/bin/uv"

mkdir -p "$VENDOR_DIR"
chmod 700 "$VENDOR_DIR"

if [ ! -d "$GTBENCH_DIR" ]; then
    echo "[phase7-setup] cloning jinhaoduan/GTBench into $GTBENCH_DIR"
    git clone --depth 1 https://github.com/jinhaoduan/GTBench.git "$GTBENCH_DIR"
    chmod 700 "$GTBENCH_DIR"
else
    echo "[phase7-setup] $GTBENCH_DIR already present, skipping clone"
fi

if ! "$VENV_PYTHON" -c "import pyspiel" 2>/dev/null; then
    echo "[phase7-setup] installing open_spiel (pyspiel) into $VENV_PYTHON's venv via uv"
    "$UV_BIN" pip install --python "$VENV_PYTHON" open_spiel
else
    echo "[phase7-setup] pyspiel already installed, skipping"
fi

if ! "$VENV_PYTHON" -c "import box" 2>/dev/null; then
    echo "[phase7-setup] installing python-box into $VENV_PYTHON's venv via uv"
    "$UV_BIN" pip install --python "$VENV_PYTHON" python-box
else
    echo "[phase7-setup] python-box already installed, skipping"
fi

echo "[phase7-setup] verifying kuhn_poker loads via pyspiel"
"$VENV_PYTHON" -c "
import pyspiel
game = pyspiel.load_game('kuhn_poker')
state = game.new_initial_state()
assert not state.is_terminal()
print('[phase7-setup] pyspiel kuhn_poker OK:', game)
"

echo "[phase7-setup] verifying vendor/gtbench's kuhn_poker game module imports"
PYTHONPATH="$GTBENCH_DIR" "$VENV_PYTHON" -c "
from gamingbench.games.kuhn_poker import KuhnPoker
g = KuhnPoker()
assert not g.env.is_terminal()
print('[phase7-setup] gamingbench.games.kuhn_poker.KuhnPoker OK')
"

echo "[phase7-setup] setup OK -- vendor/gtbench cloned, pyspiel/python-box importable, kuhn_poker verified"

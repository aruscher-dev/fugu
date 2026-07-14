#!/usr/bin/env bash
# Phase 7 setup: idempotent clone of GTBench (jinhaoduan/GTBench upstream,
# the `gtbench` harness PLAN.md's Context section names) into vendor/gtbench
# (gitignored, own git history, re-clonable -- same pattern Phase 0's
# vendor/llm_chess and m0's bin/fairy-stockfish both follow) plus the two
# pure-Python/C-extension deps this project's gtbench_ext/ actually needs
# (`open_spiel`'s `pyspiel` for game legality/state, `python-box` for the
# YAML config Box objects GTBench's own game-config loader uses).
#
# Deliberately does NOT `pip install -r vendor/gtbench/requirements.txt`
# wholesale -- that file pins an old langchain/gym/etc. stack for GTBench's
# own remote-API-only LLMModel (see gamingbench/chat/chat.py), which this
# project's gtbench_ext/ never calls (LocalTransformersModel bypasses it
# entirely). `gamingbench/games/__init__.py` eagerly imports EVERY game
# (tic_tac_toe first), which transitively imports gamingbench.utils.utils ->
# gamingbench.models -> gamingbench.chat.chat -> a bare unconditional
# `from langchain.chat_models import ...` -- so merely importing
# gamingbench.games.kuhn_poker (which the real pilot script and
# gtbench_ext/game_registry.py both do) unavoidably requires langchain to be
# importable, even though nothing here ever calls it. Rather than installing
# real langchain (which would pull in an old pinned stack + a numpy<2
# downgrade purely to satisfy an unused import), open_fugu.gtbench_ext
# ._langchain_stub provides a minimal same-named stub (every symbol raises
# NotImplementedError if ever actually called, which should never happen
# here) and prepends itself to sys.path via ensure_importable() -- see that
# module's own docstring. scripts/phase7_cmaes_kuhn_pilot.py already calls
# this before its own gamingbench imports; this verification snippet below
# must do the same (2026-07-14: found the hard way when this snippet's own
# bare import crashed on a real ModuleNotFoundError for langchain -- the fix
# is calling ensure_importable() here too, NOT installing the real package).
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
# LLMBenchLogger (gamingbench/utils/utils.py) is a singleton keyed off
# whichever caller constructs it FIRST -- OpenSpielGame.__init__ (KuhnPoker's
# base class) calls LLMBenchLogger(None) internally if nothing else has
# claimed the singleton yet, which crashes (logging.FileHandler(None)).
# scripts/phase7_cmaes_kuhn_pilot.py's real main() avoids this by
# constructing LLMBenchLogger with a real path before touching any game
# object -- this verification snippet must do the same, found the hard way
# (2026-07-14) when this exact bare `KuhnPoker()` call crashed setup with a
# TypeError from deep inside logging.FileHandler.
PYTHONPATH="$GTBENCH_DIR:$PROJECT_DIR/src" "$VENV_PYTHON" -c "
from open_fugu.gtbench_ext._langchain_stub import ensure_importable
ensure_importable()
from gamingbench.utils.utils import LLMBenchLogger
LLMBenchLogger('${PROJECT_DIR}/logs/phase7_setup_verify.log')
from gamingbench.games.kuhn_poker import KuhnPoker
g = KuhnPoker()
assert not g.env.is_terminal()
print('[phase7-setup] gamingbench.games.kuhn_poker.KuhnPoker OK')
"

echo "[phase7-setup] setup OK -- vendor/gtbench cloned, pyspiel/python-box importable, kuhn_poker verified"

#!/usr/bin/env bash
# M0: idempotent setup for the 5x5 (Gardner Minichess) fast-validation track.
# Installs pyffish into the project venv via `uv` (per PLAN.md's dependency
# convention -- this project is uv-managed, not raw pip) and downloads the
# Fairy-Stockfish binary into bin/ (gitignored, host-specific, same pattern
# as Phase 0's Stockfish 18 download).
set -euo pipefail
umask 077

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$PROJECT_DIR/bin"
VENV_PYTHON="/Data/.venv/bin/python3"
UV_BIN="$HOME/.local/bin/uv"

mkdir -p "$BIN_DIR"
chmod 700 "$BIN_DIR"

if ! "$VENV_PYTHON" -c "import pyffish" 2>/dev/null; then
    echo "[m0] installing pyffish into $VENV_PYTHON's venv via uv"
    "$UV_BIN" pip install --python "$VENV_PYTHON" pyffish
else
    echo "[m0] pyffish already installed, skipping"
fi

FAIRY_SF="$BIN_DIR/fairy-stockfish"
if [ ! -x "$FAIRY_SF" ]; then
    echo "[m0] downloading Fairy-Stockfish binary"
    curl -sL -o "$FAIRY_SF" \
        "https://github.com/fairy-stockfish/Fairy-Stockfish/releases/download/fairy_sf_14/fairy-stockfish_x86-64-bmi2"
    chmod 700 "$FAIRY_SF"
else
    echo "[m0] $FAIRY_SF already present, skipping"
fi

echo "[m0] verifying gardner variant is recognized"
if ! echo "uci" | timeout 5 "$FAIRY_SF" | grep -q "var gardner"; then
    echo "[m0] FATAL: fairy-stockfish binary does not list 'gardner' among UCI_Variant options" >&2
    exit 1
fi

echo "[m0] setup OK -- pyffish importable, fairy-stockfish supports gardner"

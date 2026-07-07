#!/bin/bash
# Installs (idempotently) a plain user crontab entry that runs orchestrate.py
# every 15 minutes. No root needed. This is what makes phase progression
# survive SSH disconnects and Claude Code session/token gaps: cron is a host
# daemon independent of any of that, and orchestrate.py + state.json make each
# invocation a safe, idempotent "advance by one step if possible" call.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="/Data/.venv/bin/python"
MARKER="# openfugu-orchestrate"
CRON_LINE="*/15 * * * * cd ${PROJECT_DIR} && ${VENV_PYTHON} scripts/orchestrate.py >> logs/orchestrate_cron.log 2>&1 ${MARKER}"

mkdir -p "${PROJECT_DIR}/logs"
chmod 700 "${PROJECT_DIR}/logs"

existing="$(crontab -l 2>/dev/null || true)"
if echo "${existing}" | grep -qF "${MARKER}"; then
    echo "Crontab entry already present, leaving as-is:"
    echo "${existing}" | grep -F "${MARKER}"
else
    { echo "${existing}"; echo "${CRON_LINE}"; } | grep -v '^$' | crontab -
    echo "Installed crontab entry:"
    echo "${CRON_LINE}"
fi

crontab -l

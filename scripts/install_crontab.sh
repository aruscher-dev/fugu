#!/bin/bash
# Installs (idempotently) a plain user crontab entry that runs orchestrate.py
# every 15 minutes. No root needed. This is what makes phase progression
# survive SSH disconnects and Claude Code session/token gaps: cron is a host
# daemon independent of any of that, and orchestrate.py + state.json make each
# invocation a safe, idempotent "advance by one step if possible" call.
#
# This host reboots periodically for OS patching (observed via `last` -- kernel
# patch level differs across recent boots), and its per-user crontab spool
# (/var/spool/cron, outside /Data and home) does NOT survive that -- confirmed
# by finding the crontab gone, with no other explanation (host uptime was
# continuous when it first vanished; a later disappearance lined up exactly
# with a reboot). A systemd --user timer (openfugu-orchestrate.timer, enabled
# via `loginctl enable-linger` so it starts at boot without a login) is the
# primary mechanism now, since its enablement state lives under
# ~/.config/systemd/user (NFS home, persists across reboots) -- but it hasn't
# been observed across an actual reboot yet either, so this script is called
# from that timer's service on every 15-minute tick too, making the two
# mechanisms mutually self-healing: whichever one does survive a given reboot
# re-installs the other.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="/Data/.venv/bin/python"
MARKER="# openfugu-orchestrate"
CRON_LINE="*/15 * * * * cd ${PROJECT_DIR} && bash scripts/install_systemd_timer.sh >> logs/orchestrate_cron.log 2>&1 && ${VENV_PYTHON} scripts/orchestrate.py >> logs/orchestrate_cron.log 2>&1 ${MARKER}"

mkdir -p "${PROJECT_DIR}/logs"
chmod 700 "${PROJECT_DIR}/logs"

existing="$(crontab -l 2>/dev/null || true)"
if echo "${existing}" | grep -qF "${CRON_LINE}"; then
    : # already present and up to date -- stay quiet when called every 15min from the timer
else
    { echo "${existing}" | grep -vF "${MARKER}"; echo "${CRON_LINE}"; } | grep -v '^$' | crontab -
    echo "[install_crontab] (re)installed crontab entry (was missing, or content was stale)"
fi

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
#
# Both mechanisms share the identical 15-minute schedule by design (so
# whichever one is alive covers the full cadence alone) -- which means when
# BOTH are alive at once (the normal, healthy state) they fire in the same
# clock tick and would otherwise race two concurrent orchestrate.py
# invocations against the same git working tree/state.json. Confirmed this
# actually happens (not just theoretical): both fired at :30 on
# 2026-07-14T07:30 CEST, one got a "Cannot fast-forward your working tree"
# git error and the other's push was rejected non-fast-forward, and the
# resulting churn is almost certainly what produced the many paired
# near-simultaneous "automated status sync" commits seen historically
# (previously misattributed to a lingering lotte.polytechnique.fr cron -- that
# may ALSO be happening, but this same-host race reproduces the exact same
# symptom on its own). `flock -n` below makes the loser of the race skip
# cleanly (exit immediately, no output, no git operations) instead of
# colliding -- do not remove even though "both mechanisms run orchestrate.py
# every tick" looks redundant; that redundancy is what survives the crontab
# spool getting wiped on this host's periodic reboots.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="/Data/.venv/bin/python"
LOCK_FILE="${PROJECT_DIR}/.orchestrate.lock"
MARKER="# openfugu-orchestrate"
CRON_LINE="*/15 * * * * cd ${PROJECT_DIR} && bash scripts/install_systemd_timer.sh >> logs/orchestrate_cron.log 2>&1 && /usr/bin/flock -n ${LOCK_FILE} ${VENV_PYTHON} scripts/orchestrate.py >> logs/orchestrate_cron.log 2>&1 ${MARKER}"

mkdir -p "${PROJECT_DIR}/logs"
chmod 700 "${PROJECT_DIR}/logs"

existing="$(crontab -l 2>/dev/null || true)"
if echo "${existing}" | grep -qF "${CRON_LINE}"; then
    : # already present and up to date -- stay quiet when called every 15min from the timer
else
    { echo "${existing}" | grep -vF "${MARKER}"; echo "${CRON_LINE}"; } | grep -v '^$' | crontab -
    echo "[install_crontab] (re)installed crontab entry (was missing, or content was stale)"
fi

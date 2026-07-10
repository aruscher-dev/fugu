#!/bin/bash
# Installs (idempotently) the openfugu-orchestrate systemd --user timer, the
# primary scheduling mechanism (see install_crontab.sh's header comment for
# why: plain crontab doesn't survive this host's periodic reboot-for-patching
# cycle, but systemd --user unit *enablement* lives under
# ~/.config/systemd/user on NFS home, which does). Also enables lingering so
# the user's systemd instance starts at boot without an active login.
#
# Called from the crontab entry too (mutual self-healing with
# install_crontab.sh -- whichever mechanism survives a given reboot
# re-installs the other), so this must be safe to run every 15 minutes: no
# output, no error, when everything is already in the desired state.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="/Data/.venv/bin/python"
UNIT_DIR="${HOME}/.config/systemd/user"
SERVICE_NAME="openfugu-orchestrate.service"
TIMER_NAME="openfugu-orchestrate.timer"

mkdir -p "${UNIT_DIR}"
chmod 700 "${HOME}/.config" "${HOME}/.config/systemd" "${UNIT_DIR}" 2>/dev/null || true
mkdir -p "${PROJECT_DIR}/logs"
chmod 700 "${PROJECT_DIR}/logs"

desired_service="[Unit]
Description=Open-Fugu orchestrate.py -- advance one phase step if possible

[Service]
Type=oneshot
WorkingDirectory=${PROJECT_DIR}
ExecStart=/bin/bash ${PROJECT_DIR}/scripts/install_crontab.sh
ExecStart=${VENV_PYTHON} ${PROJECT_DIR}/scripts/orchestrate.py
StandardOutput=append:${PROJECT_DIR}/logs/orchestrate_systemd.log
StandardError=append:${PROJECT_DIR}/logs/orchestrate_systemd.log
"

desired_timer="[Unit]
Description=Run openfugu-orchestrate every 15 minutes, catching up on boot

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
"

changed=0
if [ "$(cat "${UNIT_DIR}/${SERVICE_NAME}" 2>/dev/null || true)" != "${desired_service}" ]; then
    printf '%s' "${desired_service}" > "${UNIT_DIR}/${SERVICE_NAME}"
    chmod 600 "${UNIT_DIR}/${SERVICE_NAME}"
    changed=1
fi
if [ "$(cat "${UNIT_DIR}/${TIMER_NAME}" 2>/dev/null || true)" != "${desired_timer}" ]; then
    printf '%s' "${desired_timer}" > "${UNIT_DIR}/${TIMER_NAME}"
    chmod 600 "${UNIT_DIR}/${TIMER_NAME}"
    changed=1
fi

if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)" != "yes" ]; then
    loginctl enable-linger "$(id -un)" 2>/dev/null || true
fi

if [ "${changed}" = "1" ]; then
    systemctl --user daemon-reload
fi

if ! systemctl --user is-enabled "${TIMER_NAME}" >/dev/null 2>&1; then
    systemctl --user enable --now "${TIMER_NAME}"
    echo "[install_systemd_timer] enabled ${TIMER_NAME} (was not enabled -- likely wiped by a host reboot)"
elif ! systemctl --user is-active "${TIMER_NAME}" >/dev/null 2>&1; then
    systemctl --user start "${TIMER_NAME}"
    echo "[install_systemd_timer] restarted ${TIMER_NAME} (was enabled but not active)"
fi

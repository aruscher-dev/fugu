#!/usr/bin/env python3
"""Phase-resumability driver for Open-Fugu.

Design goal (per the approved plan): progress must not depend on any particular
SSH session, VSCode connection, or Claude Code agent session staying alive.
This script is meant to be invoked repeatedly and unattendedly by a plain user
crontab entry (see scripts/install_crontab.sh) -- each invocation looks at
state.json, advances the first non-done phase by one step if it can, and
exits. Long-running GPU work is launched into a detached `tmux` session so it
keeps running across cron invocations and host reconnects; this script itself
never blocks waiting for one.

IMPORTANT: this script can only *run* phases whose implementation already
exists. Phases without a script yet (see state.json's "not_started" phases)
require a human/agent development session to write the phase script -- cron
alone cannot invent new code. When orchestrate.py reaches such a phase it logs
that fact and exits cleanly (no error, no busy-loop).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_DIR / "state.json"
LOG_DIR = PROJECT_DIR / "logs"
VENV_PYTHON = "/Data/.venv/bin/python"

TMUX_SESSION_PREFIX = "openfugu-phase"


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    state["last_orchestrate_run"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2))
    STATE_PATH.chmod(0o600)


def tmux_session_exists(name: str) -> bool:
    result = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True)
    return result.returncode == 0


def tmux_launch(name: str, command: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.chmod(0o700)
    subprocess.run(["tmux", "new-session", "-d", "-s", name, command], check=True)
    print(f"[orchestrate] launched tmux session '{name}': {command}")


# --- Per-phase advance functions -------------------------------------------
# Each returns one of: "done", "in_progress", "blocked", or "no_script_yet".

def advance_phase_0(state: dict) -> str:
    return "done"  # verified manually this session; nothing left to automate


def advance_phase_0_5(state: dict) -> str:
    results_dir = PROJECT_DIR / "logs" / "phase0_5_floor_check"
    default_workers = ["qwen2.5-7b", "mistral-7b", "deepseek-r1-distill-qwen-7b"]
    session = f"{TMUX_SESSION_PREFIX}0_5"

    reports_done = [w for w in default_workers if (results_dir / f"{w}.json").exists()]
    if len(reports_done) == len(default_workers):
        return "done"

    if tmux_session_exists(session):
        print(f"[orchestrate] phase 0.5 still running in tmux session '{session}' "
              f"({len(reports_done)}/{len(default_workers)} workers done)")
        return "in_progress"

    # Not running and not done -- (re)launch. The underlying script is
    # idempotent (skips workers with an existing report unless --force), so a
    # crash-and-cron-relaunch just picks up remaining workers.
    log_path = LOG_DIR / "phase0_5_floor_check.log"
    cmd = (
        f"cd {PROJECT_DIR} && {VENV_PYTHON} scripts/phase0_5_blindfold_floor_check.py "
        f">> {log_path} 2>&1"
    )
    tmux_launch(session, cmd)
    return "in_progress"


PHASE_ADVANCERS = {
    "0": advance_phase_0,
    "0.5": advance_phase_0_5,
}


def main():
    state = load_state()
    advanced_any = False

    for phase_id in state["phase_order"]:
        phase = state["phases"][phase_id]
        if phase["status"] == "done":
            continue

        advancer = PHASE_ADVANCERS.get(phase_id)
        if advancer is None:
            print(f"[orchestrate] phase {phase_id} has no script yet -- "
                  f"needs a development session, not autonomous cron work. Stopping.")
            break

        result = advancer(state)
        if result == "done":
            phase["status"] = "done"
            print(f"[orchestrate] phase {phase_id}: DONE")
            advanced_any = True
            continue  # fall through to check the next phase in this same run
        elif result == "in_progress":
            phase["status"] = "in_progress"
            print(f"[orchestrate] phase {phase_id}: in progress, will re-check next run")
            break
        else:
            print(f"[orchestrate] phase {phase_id}: {result}")
            break

    save_state(state)
    if not advanced_any:
        print("[orchestrate] no phase completed this run")


if __name__ == "__main__":
    main()

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

Note for a future dev session (human or the scheduled cloud dev routine, which
has no GPU/host access and can only write+push code): to make a newly-written
phase script actually runnable by this cron loop, add a matching
`advance_phase_N(state)` function here (following the `advance_phase_0_5`
pattern -- check for the phase's output artifact, launch its script into a
detached tmux session if not already running, return "done"/"in_progress")
and register it in PHASE_ADVANCERS below. A phase script committed without an
advancer just sits there; orchestrate.py will still report "no script yet"
for it.

Also pulls the latest code via `git pull --ff-only` at the start of each run
(so phases written by the cloud dev routine get picked up automatically) and
pushes state.json + reports/ (small tracked status summaries) at the end, so
that routine can see current progress without needing access to this host.
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
REPORTS_DIR = PROJECT_DIR / "reports"  # tracked (not gitignored) -- small summaries
VENV_PYTHON = "/Data/.venv/bin/python"

TMUX_SESSION_PREFIX = "openfugu-phase"


def _run_git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT_DIR), *args],
                           capture_output=True, text=True)


def git_pull_if_clean() -> None:
    """Pick up new phase code written by the cloud dev routine. Only pulls
    when the working tree is clean, so a crashed run's local edits (there
    shouldn't be any -- orchestrate.py only ever touches state.json/reports/)
    never get clobbered by a remote merge."""
    status = _run_git("status", "--porcelain")
    if status.stdout.strip():
        print("[orchestrate] working tree not clean, skipping git pull this run")
        return
    result = _run_git("pull", "--ff-only", "origin", "main")
    if result.returncode != 0:
        print(f"[orchestrate] git pull failed (non-fatal, continuing with local state):\n{result.stderr}")
    elif "Already up to date" not in result.stdout:
        print(f"[orchestrate] git pull: {result.stdout.strip()}")


def git_commit_and_push(message: str) -> None:
    """Push state.json/reports/ updates so the cloud dev routine can see
    current progress (e.g. the Phase 0.5 gate verdict) without needing access
    to this host. Scoped to state.json + reports/ -- never touches gitignored
    host-specific paths (bin/, logs/, vendor/, checkpoints/)."""
    _run_git("add", "state.json", str(REPORTS_DIR))
    diff = _run_git("diff", "--cached", "--quiet")
    if diff.returncode == 0:
        return  # nothing staged
    commit = _run_git("commit", "-m", message)
    if commit.returncode != 0:
        print(f"[orchestrate] git commit failed:\n{commit.stderr}")
        return
    push = _run_git("push", "origin", "main")
    if push.returncode != 0:
        print(f"[orchestrate] git push failed (non-fatal):\n{push.stderr}")
    else:
        print(f"[orchestrate] pushed status update: {message}")


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
        write_phase0_5_summary(default_workers, results_dir)
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
        f"cd {PROJECT_DIR} && HF_HOME=/Data/.hf_cache HF_HUB_DISABLE_XET=1 {VENV_PYTHON} "
        f"scripts/phase0_5_blindfold_floor_check.py >> {log_path} 2>&1"
    )
    tmux_launch(session, cmd)
    return "in_progress"


def write_phase0_5_summary(workers: list, results_dir: Path) -> None:
    """Compact, tracked (non-gitignored) gate-verdict summary so the cloud
    dev routine (no access to this host's logs/) can decide whether Phase 1
    is worth building at all."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.chmod(0o700)
    per_worker = {}
    for w in workers:
        r = json.loads((results_dir / f"{w}.json").read_text())
        per_worker[w] = r["summary"]
    verdict = "PASS" if all(
        (s.get("mean_legal_move_rate") or 0) > 0.5
        for s in per_worker.values()
    ) else "REVIEW_NEEDED"
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workers": per_worker,
        "verdict": verdict,
        "note": ("verdict is a rough heuristic (mean_legal_move_rate > 0.5) -- "
                 "read per-worker numbers before deciding to build Phase 1"),
    }
    out_path = REPORTS_DIR / "phase0_5_summary.json"
    out_path.write_text(json.dumps(summary, indent=2))
    out_path.chmod(0o600)
    print(f"[orchestrate] wrote {out_path} (verdict={verdict})")


def advance_phase_1(state: dict) -> str:
    """Phase 1: A2A/AgentBeats wiring -- worker purple agent, Fugu
    orchestrator purple agent (random-routing dummy), blindfold-chess green
    judge, all talking over A2A. Gates on the smoke test's marker file
    (returncode==0), not on the chess result -- Phase 0.5 already gates
    quality; this phase is purely "does the pipeline run end to end"."""
    marker = REPORTS_DIR / "phase1_smoke_test_result.json"
    session = f"{TMUX_SESSION_PREFIX}1_smoke"

    if marker.exists():
        summary = json.loads(marker.read_text())
        if summary.get("passed"):
            return "done"
        print(f"[orchestrate] phase 1 smoke test previously failed (returncode="
              f"{summary.get('returncode')}) -- needs a dev session to fix, not a cron retry.")
        return "blocked"

    if tmux_session_exists(session):
        print(f"[orchestrate] phase 1 smoke test still running in tmux session '{session}'")
        return "in_progress"

    log_path = LOG_DIR / "phase1_agentbeats_smoke.log"
    cmd = f"cd {PROJECT_DIR} && {VENV_PYTHON} scripts/phase1_agentbeats_smoke_test.py >> {log_path} 2>&1"
    tmux_launch(session, cmd)
    return "in_progress"


PHASE_ADVANCERS = {
    "0": advance_phase_0,
    "0.5": advance_phase_0_5,
    "1": advance_phase_1,
}


def main():
    git_pull_if_clean()
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
    git_commit_and_push(f"orchestrate: automated status sync ({datetime.now(timezone.utc).isoformat()})")
    if not advanced_any:
        print("[orchestrate] no phase completed this run")


if __name__ == "__main__":
    main()

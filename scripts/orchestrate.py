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


def advance_phase_2(state: dict) -> str:
    """Phase 2: Stockfish reward pipeline sanity check -- hand-verified
    positions (a free-queen best move, a hang-the-queen blunder, an illegal
    move, a forced-mate move) plus a Ruy Lopez opening replay, gating whether
    StockfishScorer is trustworthy before Phase 3 spends real GPU-hours
    collecting SFT data against it. No GPU/model download needed -- only the
    Stockfish binary, so this can run as soon as Phase 0's wrapper works."""
    marker = REPORTS_DIR / "phase2_stockfish_sanity_result.json"
    session = f"{TMUX_SESSION_PREFIX}2_sanity"

    if marker.exists():
        summary = json.loads(marker.read_text())
        if summary.get("passed"):
            return "done"
        print(f"[orchestrate] phase 2 sanity check previously failed (engine_error="
              f"{summary.get('engine_error')}) -- needs a dev session to fix, not a cron retry.")
        return "blocked"

    if tmux_session_exists(session):
        print(f"[orchestrate] phase 2 sanity check still running in tmux session '{session}'")
        return "in_progress"

    log_path = LOG_DIR / "phase2_stockfish_sanity.log"
    cmd = f"cd {PROJECT_DIR} && {VENV_PYTHON} scripts/phase2_stockfish_setup_sanity.py >> {log_path} 2>&1"
    tmux_launch(session, cmd)
    return "in_progress"


def advance_phase_3(state: dict) -> str:
    """Phase 3: SFT data collection (PLAN.md training recipe step 1) -- query
    every candidate worker n=3-4 times on ~300-600 self-play blindfold
    positions, score each reply via StockfishScorer, write raw scored
    records for Phase 4 to build a soft target distribution from. Long
    background job (PLAN.md estimate: 2-4 days, ~10-20 GPU-hours), so this
    follows Phase 0.5's pattern (not Phase 1/2's) -- check per-worker
    progress via the tracked summary rather than a single pass/fail marker,
    and re-launch (idempotently -- the underlying script resumes from
    whatever it already has) if the tmux session isn't currently running but
    the work also isn't complete yet.

    Unlike every earlier phase, this one does NOT auto-launch off a bare
    'not done yet' check: Phase 0.5's floor check
    (reports/phase0_5_summary.json) came back REVIEW_NEEDED (0%/44%/22% mean
    legal-move rate across these same three default workers), and this phase
    is exactly the ~10-20 GPU-hour spend that verdict is meant to gate --
    unlike Phase 2, which only sanity-checked the reward *scorer* and was
    safe to auto-run regardless. So this additionally requires an explicit
    human sign-off flag in state.json (phases["3"].gpu_spend_approved ==
    true) before it will tmux-launch the actual data collection script. A
    human should review (or improve) Phase 0.5's numbers, then flip that
    flag to true -- see STATUS.md."""
    summary_path = REPORTS_DIR / "phase3_summary.json"
    session = f"{TMUX_SESSION_PREFIX}3_sft_data"

    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        if summary.get("verdict") == "COMPLETE":
            return "done"
        done = {w: v["collected"] for w, v in summary.get("workers", {}).items()}
    else:
        done = None

    if tmux_session_exists(session):
        print(f"[orchestrate] phase 3 SFT data collection still running in tmux "
              f"session '{session}'" + (f" (progress: {done})" if done else ""))
        return "in_progress"

    if not state["phases"].get("3", {}).get("gpu_spend_approved"):
        print("[orchestrate] phase 3 BLOCKED pending human sign-off: Phase 0.5's floor check came back "
              "REVIEW_NEEDED (see reports/phase0_5_summary.json) and this phase spends real GPU-hours "
              "against that same worker pool. Set phases[\"3\"].gpu_spend_approved = true in state.json "
              "once reviewed (see STATUS.md) to let this launch.")
        return "blocked"

    # Not running, not complete, and human-approved -- (re)launch. collect_for_worker() skips
    # (position_idx, sample_idx) pairs already written, so a crash-and-cron-
    # relaunch resumes rather than restarting from scratch.
    log_path = LOG_DIR / "phase3_sft_data_collection.log"
    cmd = (
        f"cd {PROJECT_DIR} && HF_HOME=/Data/.hf_cache HF_HUB_DISABLE_XET=1 {VENV_PYTHON} "
        f"scripts/phase3_collect_sft_data.py >> {log_path} 2>&1"
    )
    tmux_launch(session, cmd)
    return "in_progress"


def advance_phase_4(state: dict) -> str:
    """Phase 4: SVF + selection head implementation, SFT training (PLAN.md
    training recipe step 1's second half). Phase 3 already collected and
    scored the raw per-(worker, position, sample) records; this phase turns
    them into a per-position soft target distribution (softmax-tau over mean
    reward) and trains the orchestrator backbone's selection head + SVF `z`
    vectors (src/open_fugu/models/{svf,worker_backend}.py,
    src/open_fugu/train/train_sft.py) against it via a plain AdamW loop.

    Short relative to Phase 3 (PLAN.md estimate: 0.5-1 day -- head+SVF-z is a
    tiny parameter count and Phase 3 already paid the expensive part), but
    still launched via tmux/cron like every GPU phase rather than run
    synchronously, since it needs the GPU and downloads the orchestrator
    backbone model (Qwen2.5-1.5B-Instruct) on first use.

    Unlike Phase 3, this does NOT require an extra gpu_spend_approved-style
    human sign-off gate: it only reads Phase 3's already-collected (and
    already human-approved) data and trains a small number of parameters --
    not a new multi-GPU-hour spend against the borderline worker-quality
    numbers that gate exists to protect. See STATUS.md and this phase's own
    `reports/phase4_summary.json` note for why a human should still
    sanity-check the resulting loss/checkpoint before Phase 5 spends
    GPU-hours on real matches using it."""
    summary_path = REPORTS_DIR / "phase4_summary.json"
    session = f"{TMUX_SESSION_PREFIX}4_sft_train"

    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        if summary.get("verdict") == "COMPLETE":
            return "done"
        print(f"[orchestrate] phase 4 previous run verdict was '{summary.get('verdict')}' "
              f"(not COMPLETE) -- needs a dev session to look at, not a cron retry.")
        return "blocked"

    if tmux_session_exists(session):
        print(f"[orchestrate] phase 4 SFT training still running in tmux session '{session}'")
        return "in_progress"

    if not (PROJECT_DIR / "logs" / "phase3_sft_data" / "positions.jsonl").exists():
        print("[orchestrate] phase 4 waiting on Phase 3's output "
              "(logs/phase3_sft_data/positions.jsonl not found yet)")
        return "blocked"

    # Not running and not complete -- (re)launch. build_soft_targets()/
    # collect_for_worker()'s underlying data is static once Phase 3 finished,
    # so re-running from scratch on a crash-and-cron-relaunch is cheap here
    # (unlike Phase 3's multi-day resumable job).
    log_path = LOG_DIR / "phase4_sft_train.log"
    cmd = (
        f"cd {PROJECT_DIR} && HF_HOME=/Data/.hf_cache HF_HUB_DISABLE_XET=1 {VENV_PYTHON} "
        f"scripts/phase4_train_sft.py >> {log_path} 2>&1"
    )
    tmux_launch(session, cmd)
    return "in_progress"


# Tunable thresholds for advance_phase_4_5's automated gate review -- kept as
# module-level constants (not buried in the function body) so a future
# session can find and adjust them without re-reading the whole function.
PHASE_4_5_MIN_VAL_TARGETS = 10          # below this, final_val_loss is too noisy to trust
PHASE_4_5_BASELINE_MARGIN = 0.90        # final_val_loss must be <= this x uniform_baseline_cross_entropy
PHASE_4_5_OVERFIT_TOLERANCE = 1.10      # final_val_loss must be <= this x the best epoch's val loss
PHASE_4_5_MIN_WORKERS_PASSING = 2       # of the default 3, how many must clear Phase 0.5's >50% bar


def advance_phase_4_5(state: dict) -> str:
    """Phase 4.5: automated deep-review gate between Phase 4 (SFT training)
    and Phase 5 (real match GPU-hours) -- added 2026-07-12 per explicit user
    request ("set it up so that this review is automated"), replacing the
    manual step where a human (or a Claude session, per
    feedback_deep_review_gates in memory) had to read logs/phase4_sft_train.log
    and decide by hand whether to flip phases["5"].gpu_spend_approved.

    This is NOT a rubber stamp on the aggregate summary the way that
    approach's earlier failure mode was: the 2026-07-12 deep-review that
    found the ORIGINAL phase4_summary.json's final_loss/mean_loss_last_50
    misleading (in-sample only, hid two positions getting worse across
    epochs and a trailing loss worse than random) is exactly why
    phase4_train_sft.py was rewritten to report final_val_loss/
    val_loss_per_epoch/uniform_baseline_cross_entropy in the first place --
    a genuine held-out generalization signal that didn't exist before. This
    function's checks read those fields and cross-reference Phase 0.5's own
    worker-pool floor check, mirroring the reasoning a human/Claude deep-
    review applied by hand before those fields existed to surface it. If any
    check fails, phases["5"].gpu_spend_approved is left untouched (stays
    false) and the specific failing reasons are written to
    reports/phase4_5_gate_review.json -- this function only ever
    auto-*approves*, it never auto-denies permanently or retries training
    with different hyperparameters on its own; a NOT_READY verdict still
    needs a human or a future session to look at the reasons (and, if
    warranted, the raw log) before deciding what to change and re-running
    Phase 4.

    No GPU/model access needed (pure JSON + arithmetic), so unlike Phase
    3/5/7/8/9 this carries no gpu_spend_approved gate of its own -- it only
    ever reads already-collected data, same reasoning as Phase 6's gate-free
    status. Runs synchronously (no tmux), like Phase 6/m1's fast checks.
    """
    review_path = REPORTS_DIR / "phase4_5_gate_review.json"
    if review_path.exists():
        return "done"

    p4_path = REPORTS_DIR / "phase4_summary.json"
    if not p4_path.exists():
        # Shouldn't happen -- advance_track only reaches "4.5" once phase 4
        # itself returned "done", which requires this file to exist.
        return "blocked"
    p4 = json.loads(p4_path.read_text())

    p0_5_path = REPORTS_DIR / "phase0_5_summary.json"
    p0_5 = json.loads(p0_5_path.read_text()) if p0_5_path.exists() else {"workers": {}}

    final_val = p4.get("final_val_loss")
    baseline = p4.get("uniform_baseline_cross_entropy")
    val_losses = p4.get("val_loss_per_epoch") or []
    n_val = p4.get("n_val_targets") or 0

    checks: dict = {}
    reasons: list = []

    checks["enough_val_data"] = n_val >= PHASE_4_5_MIN_VAL_TARGETS
    if not checks["enough_val_data"]:
        reasons.append(f"n_val_targets={n_val} < {PHASE_4_5_MIN_VAL_TARGETS} -- too few held-out "
                        f"examples to trust final_val_loss as a generalization signal")

    checks["beats_uniform_baseline"] = (
        final_val is not None and baseline is not None
        and final_val <= baseline * PHASE_4_5_BASELINE_MARGIN
    )
    if not checks["beats_uniform_baseline"]:
        reasons.append(f"final_val_loss={final_val} is not meaningfully below "
                        f"uniform_baseline_cross_entropy={baseline} (need <= "
                        f"{PHASE_4_5_BASELINE_MARGIN}x baseline) -- the head may not have learned "
                        f"a real routing signal, just fit near-uniform soft targets")

    if val_losses:
        best_val = min(val_losses)
        checks["not_overfitting_late"] = (
            final_val is not None and final_val <= best_val * PHASE_4_5_OVERFIT_TOLERANCE
        )
        if not checks["not_overfitting_late"]:
            reasons.append(f"final_val_loss={final_val} is more than "
                            f"{(PHASE_4_5_OVERFIT_TOLERANCE - 1) * 100:.0f}% worse than the best "
                            f"epoch's val loss ({best_val}) -- looks like it overfit after that point")
    else:
        checks["not_overfitting_late"] = False
        reasons.append("no val_loss_per_epoch data to check the overfitting trend")

    workers = p0_5.get("workers", {})
    n_passing = sum(1 for w in workers.values() if (w.get("mean_legal_move_rate") or 0) > 0.5)
    checks["worker_pool_viable"] = n_passing >= PHASE_4_5_MIN_WORKERS_PASSING
    if not checks["worker_pool_viable"]:
        reasons.append(f"only {n_passing}/{len(workers)} workers clear Phase 0.5's >50% "
                        f"legal-move-rate signal threshold (reports/phase0_5_summary.json)")

    ready = all(checks.values())
    verdict = "READY_FOR_PHASE_5" if ready else "NOT_READY"

    review = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "reasons": reasons,
        "verdict": verdict,
        "phase4_summary_snapshot": {
            "final_val_loss": final_val,
            "uniform_baseline_cross_entropy": baseline,
            "val_loss_per_epoch": val_losses,
            "n_val_targets": n_val,
        },
        "note": ("Automated gate review (see advance_phase_4_5's docstring in orchestrate.py "
                 "for the full reasoning and thresholds). NOT_READY does not mean 'never' -- it "
                 "means the current checkpoint didn't clear a specific, documented bar; see "
                 "`reasons` above for exactly which one(s), then decide whether to adjust "
                 "training (more data, more epochs, a different tau, etc.) and re-run Phase 4 "
                 "(delete this file and reset state.json's phase 4 to 'pending' to retry)."),
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.chmod(0o700)
    review_path.write_text(json.dumps(review, indent=2))
    review_path.chmod(0o600)
    print(f"[orchestrate] phase 4.5 gate review: {verdict}" +
          (f" -- {'; '.join(reasons)}" if reasons else " -- all checks passed"))

    if ready:
        state["phases"]["5"]["gpu_spend_approved"] = True
        state["phases"]["5"]["note"] = (
            state["phases"]["5"].get("note", "")
            + " [Auto-approved by phase 4.5's automated gate review on "
            + review["generated_at"] + " -- see reports/phase4_5_gate_review.json.]"
        )
        print("[orchestrate] phase 4.5: auto-approved phases['5'].gpu_spend_approved = true")

    return "done"


def advance_phase_5(state: dict) -> str:
    """Phase 5: baseline + Open-Fugu blindfold matches (PLAN.md phase table:
    "5 conditions x ~25 games" -- 3 solo-worker baselines, Phase 1's random-
    routing orchestrator, and Phase 5's own per-query Open-Fugu SFT-routed
    orchestrator using Phase 4's checkpoint). Follows advance_phase_3/4's
    pattern (not Phase 0.5/m2's) -- scripts/phase5_baseline_and_fugu_matches.py
    writes its own tracked reports/phase5_summary.json (IN_PROGRESS while any
    of the 5 conditions are still missing, COMPLETE once all are in), so this
    advancer just reads that verdict rather than re-deriving it, same as
    advance_phase_3/4 do with their own summary files.

    Unlike Phase 2/4 (safe to auto-run) but like Phase 3: this is exactly
    the ~25 GPU-hour spend Phase 0.5's REVIEW_NEEDED floor check
    (reports/phase0_5_summary.json) is meant to gate, PLUS it plays real
    matches with Phase 4's checkpoint -- whose own reports/phase4_summary.json
    explicitly flags that a human should sanity-check final_loss/
    mean_loss_last_50 before trusting it here. So this additionally requires
    the same explicit human sign-off flag Phase 3 used
    (phases["5"].gpu_spend_approved == true) before it will tmux-launch the
    match script. See STATUS.md."""
    summary_path = REPORTS_DIR / "phase5_summary.json"
    session = f"{TMUX_SESSION_PREFIX}5_matches"

    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        if summary.get("verdict") == "COMPLETE":
            return "done"
        done = {c: v["n_games"] for c, v in summary.get("conditions", {}).items()}
    else:
        done = None

    if tmux_session_exists(session):
        print(f"[orchestrate] phase 5 matches still running in tmux session '{session}'"
              + (f" (progress: {done})" if done else ""))
        return "in_progress"

    if not state["phases"].get("5", {}).get("gpu_spend_approved"):
        print("[orchestrate] phase 5 BLOCKED pending human sign-off: Phase 0.5's floor check came back "
              "REVIEW_NEEDED (see reports/phase0_5_summary.json) and Phase 4's checkpoint "
              "(reports/phase4_summary.json) hasn't been sanity-checked either -- this phase spends "
              "~25 real GPU-hours playing matches with both. Set phases[\"5\"].gpu_spend_approved = true "
              "in state.json once reviewed (see STATUS.md) to let this launch.")
        return "blocked"

    if not (PROJECT_DIR / "checkpoints" / "phase4_sft" / "backbone_head_svf.pt").exists():
        print("[orchestrate] phase 5 waiting on Phase 4's checkpoint "
              "(checkpoints/phase4_sft/backbone_head_svf.pt not found yet)")
        return "blocked"

    # Not running and not complete, and human-approved -- (re)launch. The
    # underlying script skips any condition whose result file already
    # exists, so a crash-and-cron-relaunch resumes rather than restarting.
    log_path = LOG_DIR / "phase5_baseline_and_fugu_matches.log"
    cmd = (
        f"cd {PROJECT_DIR} && HF_HOME=/Data/.hf_cache HF_HUB_DISABLE_XET=1 {VENV_PYTHON} "
        f"scripts/phase5_baseline_and_fugu_matches.py >> {log_path} 2>&1"
    )
    tmux_launch(session, cmd)
    return "in_progress"


def advance_phase_6(state: dict) -> str:
    """Phase 6: evaluation report (PLAN.md phase table: "ACPL, blunder rate,
    win-rate, illegal-move rate"). Pure aggregation over Phase 5's raw
    per-condition game records (logs/phase5_matches/<condition>.json,
    written by scripts/phase5_baseline_and_fugu_matches.py) -- no GPU/model
    access needed, so unlike Phase 3/5 this carries no gpu_spend_approved
    gate: it only reads data a human already approved collecting, same
    reasoning as Phase 4's own gate-free status. Runs synchronously (no
    tmux), like advance_m0/m1's fast CPU-only checks -- aggregating a few
    hundred already-written JSON game records is not a long-running job.

    Blocks until Phase 5 itself is fully done (state.json phase "5" ==
    "done", i.e. reports/phase5_summary.json's own verdict is COMPLETE) --
    a report built from an in-progress Phase 5 run would be a misleading
    final deliverable, even though scripts/phase6_eval_report.py itself is
    capable of writing a PARTIAL one (see its own verdict handling) if
    invoked manually before that."""
    report_path = REPORTS_DIR / "phase6_eval_report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text())
        if report.get("verdict") == "COMPLETE":
            return "done"

    if state["phases"].get("5", {}).get("status") != "done":
        print("[orchestrate] phase 6 waiting on Phase 5 to finish (reports/phase5_summary.json "
              "not yet verdict=COMPLETE) -- an evaluation report over partial match data would "
              "be a misleading final deliverable.")
        return "blocked"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.chmod(0o700)
    log_path = LOG_DIR / "phase6_eval_report.log"
    result = subprocess.run(
        [VENV_PYTHON, str(PROJECT_DIR / "scripts" / "phase6_eval_report.py")],
        cwd=PROJECT_DIR, capture_output=True, text=True,
    )
    log_path.write_text(result.stdout + result.stderr)
    log_path.chmod(0o600)
    if result.returncode != 0:
        print(f"[orchestrate] phase 6 report generation failed (see {log_path})")
        return "blocked"

    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    if report.get("verdict") == "COMPLETE":
        return "done"
    print(f"[orchestrate] phase 6 report verdict='{report.get('verdict')}' (not COMPLETE yet)")
    return "in_progress"


def advance_phase_7(state: dict) -> str:
    """Phase 7 (stretch): sep-CMA-ES pilot on gtbench's kuhn_poker (PLAN.md
    training recipe step 2: "Validate the loop first on gtbench's cheap
    kuhn_poker before spending chess GPU-hours on it"). Evolves the Fugu
    orchestrator's selection head directly against real end-to-end game
    reward (win/loss/draw vs. a fixed RandomAgent baseline) via
    open_fugu.train.train_cmaes.run_cmaes -- validates the whole CMA-ES
    mechanism end-to-end before Phase 8 spends real chess GPU-hours on the
    same loop. Independent of the main chess track's worker pool/checkpoints
    (different game, own fresh orchestrator backbone) -- follows
    advance_phase_3/5's summary-file + tmux pattern, not advance_phase_0_5/m2's
    per-worker one.

    Gated the same way Phase 3/5 are (phases["7"].gpu_spend_approved ==
    true) even though it's independent of Phase 0.5's chess-quality verdict
    -- PLAN.md's own estimate is "2-5 days" of real GPU-hours, spent
    autonomously across many CMA-ES generations, which is exactly the kind
    of nontrivial autonomous spend this project's convention (Phase 3/5)
    requires an explicit human sign-off for before a cron tick will launch
    it. A human should set that flag in state.json once ready -- see
    STATUS.md."""
    summary_path = REPORTS_DIR / "phase7_summary.json"
    session = f"{TMUX_SESSION_PREFIX}7_cmaes_kuhn"

    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        if summary.get("verdict") == "COMPLETE":
            return "done"

    if tmux_session_exists(session):
        print(f"[orchestrate] phase 7 CMA-ES kuhn_poker pilot still running in tmux session '{session}'")
        return "in_progress"

    if not state["phases"].get("7", {}).get("gpu_spend_approved"):
        print("[orchestrate] phase 7 BLOCKED pending human sign-off: this is a stretch-goal CMA-ES "
              "pilot (PLAN.md estimate: 2-5 days of real GPU-hours across many generations). Set "
              "phases[\"7\"].gpu_spend_approved = true in state.json once reviewed (see STATUS.md) "
              "to let this launch.")
        return "blocked"

    # Not running and not complete, and human-approved -- (re)launch. The
    # underlying script is idempotent (checks reports/phase7_summary.json's
    # own verdict before doing any work, see its main()), so a
    # crash-and-cron-relaunch just re-runs cleanly rather than resuming
    # mid-CMA-ES-run (unlike Phase 3's per-position resumability, a partial
    # CMA-ES run isn't meaningfully resumable -- the ask/tell state lives
    # only in that one process).
    log_path = LOG_DIR / "phase7_cmaes_kuhn_pilot.log"
    cmd = (
        f"cd {PROJECT_DIR} && HF_HOME=/Data/.hf_cache HF_HUB_DISABLE_XET=1 {VENV_PYTHON} "
        f"scripts/phase7_cmaes_kuhn_pilot.py >> {log_path} 2>&1"
    )
    tmux_launch(session, cmd)
    return "in_progress"


def advance_phase_8(state: dict) -> str:
    """Phase 8 (stretch): sep-CMA-ES on truncated blindfold chess (PLAN.md
    phase table: "CMA-ES on truncated blindfold chess | open-ended,
    explicitly under-converged"). Evolves the Fugu orchestrator's selection
    head against real end-to-end truncated blindfold-game reward (blended
    win/loss/draw + graded -ACPL, open_fugu.train.rollout_chess.blend_reward)
    via the same open_fugu.train.train_cmaes.run_cmaes Phase 7 already
    validated against real kuhn_poker reward -- this is the "spend chess
    GPU-hours on it" step PLAN.md's training recipe deferred until Phase 7's
    mechanics were proven. Follows advance_phase_3/5/7's summary-file + tmux
    + gpu_spend_approved pattern (not advance_phase_0_5/m2's per-worker one).

    Gated the same way Phase 3/5/7 are (phases["8"].gpu_spend_approved ==
    true) -- unlike Phase 7's kuhn_poker pilot, this ALSO loads the same
    worker pool Phase 0.5's floor check flagged REVIEW_NEEDED (0%/44%/22%
    legal-move rate) to actually play real blindfold chess, so a human
    reviewing this flag should look at both reports/phase0_5_summary.json
    and reports/phase7_summary.json (confirms the CMA-ES mechanism itself
    works) before approving. PLAN.md's own estimate is "open-ended,
    explicitly under-converged" -- i.e. even a full run is expected to be a
    scoped proof-of-concept, not a finished result, same as Phase 7's
    kuhn_poker pilot but for real 7-8B-model blindfold-chess generation
    (considerably more expensive per rollout than poker's few-token action
    space)."""
    summary_path = REPORTS_DIR / "phase8_summary.json"
    session = f"{TMUX_SESSION_PREFIX}8_cmaes_chess"

    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        if summary.get("verdict") == "COMPLETE":
            return "done"

    if tmux_session_exists(session):
        print(f"[orchestrate] phase 8 CMA-ES truncated-blindfold-chess pilot still running in tmux session '{session}'")
        return "in_progress"

    if not state["phases"].get("8", {}).get("gpu_spend_approved"):
        print("[orchestrate] phase 8 BLOCKED pending human sign-off: this is a stretch-goal CMA-ES "
              "pilot against real blindfold chess (PLAN.md estimate: 'open-ended, explicitly "
              "under-converged'), loading the same worker pool Phase 0.5's floor check flagged "
              "REVIEW_NEEDED. Set phases[\"8\"].gpu_spend_approved = true in state.json once reviewed "
              "(see STATUS.md) to let this launch.")
        return "blocked"

    # Not running and not complete, and human-approved -- (re)launch. The
    # underlying script is idempotent (checks reports/phase8_summary.json's
    # own verdict before doing any work, see its main()), same
    # not-meaningfully-resumable-mid-run reasoning as advance_phase_7.
    log_path = LOG_DIR / "phase8_cmaes_chess_pilot.log"
    cmd = (
        f"cd {PROJECT_DIR} && HF_HOME=/Data/.hf_cache HF_HUB_DISABLE_XET=1 {VENV_PYTHON} "
        f"scripts/phase8_cmaes_chess_pilot.py >> {log_path} 2>&1"
    )
    tmux_launch(session, cmd)
    return "in_progress"


def advance_phase_9(state: dict) -> str:
    """Phase 9 (stretch): gtbench extension (PLAN.md phase table: "gtbench
    extension (connect_four/breakthrough + kuhn_poker) | 2-4 days"). Extends
    Phase 7's kuhn_poker-only sep-CMA-ES pilot to two more gtbench games with
    meaningfully different action/observation shapes (connect_four's column
    picks, breakthrough's coordinate moves on a deliberately small 3-column
    board -- see gtbench_ext/game_registry.py), reusing the exact same
    open_fugu.train.train_cmaes.run_cmaes mechanism Phase 7 already validated
    -- this phase checks that mechanism *generalizes* across game types, not
    that it converges deeply on any one (deliberately smaller per-game CMA-ES
    budget than Phase 7's kuhn_poker-only pilot, see the script's own
    docstring). Follows advance_phase_7/8's summary-file + tmux +
    gpu_spend_approved pattern, not advance_phase_0_5/m2's per-worker one --
    but scripts/phase9_gtbench_extension.py's own summary is itself
    per-game-resumable (a crash-and-cron-relaunch skips already-COMPLETE
    games in reports/phase9_summary.json's "games" dict rather than
    re-running the whole --games list from scratch), unlike Phase 7/8's
    single-game not-meaningfully-resumable-mid-run scripts.

    Gated the same way Phase 3/5/7/8 are (phases["9"].gpu_spend_approved ==
    true) -- independent of Phase 0.5's chess-quality verdict (none of these
    three games touch chess skill at all, same reasoning as Phase 7's gate),
    but still a multi-day autonomous GPU spend (PLAN.md's own estimate:
    "2-4 days") a human should sign off on first, same reasoning as every
    other gpu_spend_approved gate in this project."""
    summary_path = REPORTS_DIR / "phase9_summary.json"
    session = f"{TMUX_SESSION_PREFIX}9_gtbench_extension"

    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        if summary.get("verdict") == "COMPLETE":
            return "done"

    if tmux_session_exists(session):
        print(f"[orchestrate] phase 9 gtbench extension still running in tmux session '{session}'")
        return "in_progress"

    if not state["phases"].get("9", {}).get("gpu_spend_approved"):
        print("[orchestrate] phase 9 BLOCKED pending human sign-off: this is a stretch-goal multi-game "
              "gtbench CMA-ES extension (PLAN.md estimate: 2-4 days of real GPU-hours across "
              "connect_four/breakthrough/kuhn_poker). Set phases[\"9\"].gpu_spend_approved = true in "
              "state.json once reviewed (see STATUS.md) to let this launch.")
        return "blocked"

    # Not running and not complete, and human-approved -- (re)launch. The
    # underlying script is idempotent at the per-game granularity (skips any
    # game whose own entry in reports/phase9_summary.json's "games" dict is
    # already COMPLETE, see that script's main()), so a crash-and-cron-relaunch
    # resumes at the first unfinished game rather than restarting every game
    # from scratch -- more granular than Phase 7/8's single-game scripts,
    # which can only restart their one CMA-ES run wholesale.
    log_path = LOG_DIR / "phase9_gtbench_extension.log"
    cmd = (
        f"cd {PROJECT_DIR} && HF_HOME=/Data/.hf_cache HF_HUB_DISABLE_XET=1 {VENV_PYTHON} "
        f"scripts/phase9_gtbench_extension.py >> {log_path} 2>&1"
    )
    tmux_launch(session, cmd)
    return "in_progress"


PHASE_ADVANCERS = {
    "0": advance_phase_0,
    "0.5": advance_phase_0_5,
    "1": advance_phase_1,
    "2": advance_phase_2,
    "3": advance_phase_3,
    "4": advance_phase_4,
    "4.5": advance_phase_4_5,
    "5": advance_phase_5,
    "6": advance_phase_6,
    "7": advance_phase_7,
    "8": advance_phase_8,
    "9": advance_phase_9,
}


# --- Minichess (5x5, Gardner variant) track advance functions --------------
# Independent phase track (see state.json's "minichess_phase_order" /
# "minichess_phases" and PLAN.md's "5x5 fast-validation track" addendum) --
# runs alongside the main full-chess track, not serialized behind it. Cheap
# enough (no GPU needed until m4) to validate the full Fugu pipeline (SFT +
# CMA-ES) once here before Phases 3-9 spend real GPU-hours on full chess.

def advance_m0(state: dict) -> str:
    """m0: install pyffish + download the Fairy-Stockfish binary. Fast,
    CPU-only, deterministic -- run synchronously rather than via tmux."""
    result = subprocess.run(["bash", str(PROJECT_DIR / "scripts" / "m0_setup_gardner_engine.sh")],
                             capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(f"[orchestrate] m0 setup failed:\n{result.stderr}")
        return "blocked"
    return "done"


def advance_m1(state: dict) -> str:
    """m1: GardnerBoard/GardnerScorer sanity check against hand-verified 5x5
    positions -- gates whether the minichess reward pipeline is trustworthy
    before m2's floor check spends any GPU-hours against it."""
    marker = REPORTS_DIR / "m1_gardner_engine_verify_result.json"
    session = f"{TMUX_SESSION_PREFIX}_m1_verify"

    if marker.exists():
        summary = json.loads(marker.read_text())
        if summary.get("passed"):
            return "done"
        print(f"[orchestrate] m1 verify previously failed (engine_error="
              f"{summary.get('engine_error')}) -- needs a dev session to fix, not a cron retry.")
        return "blocked"

    if tmux_session_exists(session):
        print(f"[orchestrate] m1 verify still running in tmux session '{session}'")
        return "in_progress"

    log_path = LOG_DIR / "m1_verify_gardner_engine.log"
    cmd = f"cd {PROJECT_DIR} && {VENV_PYTHON} scripts/m1_verify_gardner_engine.py >> {log_path} 2>&1"
    tmux_launch(session, cmd)
    return "in_progress"


def advance_m2(state: dict) -> str:
    """m2: floor check on 5x5 with the existing worker pool (same three
    default workers Phase 0.5 tested), using harness.py's
    board_factory=GardnerBoard + GardnerScorer and a small hand-verified
    opening book (see scripts/m2_gardner_floor_check.py). Follows
    advance_phase_0_5's pattern (per-worker tracked reports + a compact
    aggregate summary), not advance_m1's single-marker pattern, since this
    reports per-worker legal-move-rate numbers the same way Phase 0.5 does."""
    results_dir = PROJECT_DIR / "logs" / "m2_gardner_floor_check"
    default_workers = ["qwen2.5-7b", "mistral-7b", "deepseek-r1-distill-qwen-7b"]
    session = f"{TMUX_SESSION_PREFIX}_m2_floor_check"

    reports_done = [w for w in default_workers if (results_dir / f"{w}.json").exists()]
    if len(reports_done) == len(default_workers):
        write_m2_summary(default_workers, results_dir)
        return "done"

    if tmux_session_exists(session):
        print(f"[orchestrate] m2 floor check still running in tmux session '{session}' "
              f"({len(reports_done)}/{len(default_workers)} workers done)")
        return "in_progress"

    # Not running and not done -- (re)launch. The underlying script is
    # idempotent (skips workers with an existing report unless --force), so a
    # crash-and-cron-relaunch just picks up remaining workers.
    log_path = LOG_DIR / "m2_gardner_floor_check.log"
    cmd = (
        f"cd {PROJECT_DIR} && HF_HOME=/Data/.hf_cache HF_HUB_DISABLE_XET=1 {VENV_PYTHON} "
        f"scripts/m2_gardner_floor_check.py >> {log_path} 2>&1"
    )
    tmux_launch(session, cmd)
    return "in_progress"


def write_m2_summary(workers: list, results_dir: Path) -> None:
    """Compact, tracked (non-gitignored) gate-verdict summary -- mirrors
    write_phase0_5_summary, so the cloud dev routine (no access to this
    host's logs/) can decide whether m3's A2A wiring / m4's SFT data
    collection are worth building against this same worker pool on 5x5."""
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
                 "read per-worker numbers before deciding to build m3+ on this worker pool"),
    }
    out_path = REPORTS_DIR / "m2_gardner_floor_check_summary.json"
    out_path.write_text(json.dumps(summary, indent=2))
    out_path.chmod(0o600)
    print(f"[orchestrate] wrote {out_path} (verdict={verdict})")


def advance_m3(state: dict) -> str:
    """m3: A2A/AgentBeats wiring on 5x5 -- reuses Phase 1's worker_agent.py/
    orchestrator_agent.py verbatim (board-agnostic, see chess_green_agent.py's
    module docstring) plus chess_green_agent.py's new --variant gardner flag
    (GardnerBoard/GardnerScorer via harness.py's board_factory param). Same
    marker-file/tmux pattern as advance_phase_1 -- gates on the smoke test's
    returncode (does the pipeline run end to end on this board size), not on
    chess quality: m2's floor check already gates quality for this track
    (currently REVIEW_NEEDED, 0% legal-move rate for all 3 default workers --
    see reports/m2_gardner_floor_check_summary.json and STATUS.md), same
    reasoning Phase 1 used to proceed despite Phase 0.5's own REVIEW_NEEDED
    verdict on the full-chess track."""
    marker = REPORTS_DIR / "m3_gardner_smoke_test_result.json"
    session = f"{TMUX_SESSION_PREFIX}_m3_smoke"

    if marker.exists():
        summary = json.loads(marker.read_text())
        if summary.get("passed"):
            return "done"
        print(f"[orchestrate] m3 smoke test previously failed (returncode="
              f"{summary.get('returncode')}) -- needs a dev session to fix, not a cron retry.")
        return "blocked"

    if tmux_session_exists(session):
        print(f"[orchestrate] m3 smoke test still running in tmux session '{session}'")
        return "in_progress"

    log_path = LOG_DIR / "m3_gardner_agentbeats_smoke.log"
    cmd = f"cd {PROJECT_DIR} && {VENV_PYTHON} scripts/m3_gardner_agentbeats_smoke_test.py >> {log_path} 2>&1"
    tmux_launch(session, cmd)
    return "in_progress"


MINICHESS_PHASE_ADVANCERS = {
    "m0": advance_m0,
    "m1": advance_m1,
    "m2": advance_m2,
    "m3": advance_m3,
}


def advance_track(state: dict, phase_order_key: str, phases_key: str, advancers: dict) -> bool:
    """Advance one independent phase track by (at most) one step, following
    the same never-block/first-non-done-phase logic as the main track.
    Returns True if any phase in this track completed this run."""
    advanced_any = False
    for phase_id in state.get(phase_order_key, []):
        phase = state[phases_key][phase_id]
        if phase["status"] == "done":
            continue

        advancer = advancers.get(phase_id)
        if advancer is None:
            print(f"[orchestrate] [{phases_key}] phase {phase_id} has no script yet -- "
                  f"needs a development session, not autonomous cron work. Stopping this track.")
            break

        result = advancer(state)
        if result == "done":
            phase["status"] = "done"
            print(f"[orchestrate] [{phases_key}] phase {phase_id}: DONE")
            advanced_any = True
            continue
        elif result == "in_progress":
            phase["status"] = "in_progress"
            print(f"[orchestrate] [{phases_key}] phase {phase_id}: in progress, will re-check next run")
            break
        else:
            print(f"[orchestrate] [{phases_key}] phase {phase_id}: {result}")
            break
    return advanced_any


def main():
    git_pull_if_clean()
    state = load_state()

    advanced_main = advance_track(state, "phase_order", "phases", PHASE_ADVANCERS)
    advanced_minichess = advance_track(
        state, "minichess_phase_order", "minichess_phases", MINICHESS_PHASE_ADVANCERS
    ) if "minichess_phase_order" in state else False

    save_state(state)
    git_commit_and_push(f"orchestrate: automated status sync ({datetime.now(timezone.utc).isoformat()})")
    if not (advanced_main or advanced_minichess):
        print("[orchestrate] no phase completed this run")


if __name__ == "__main__":
    main()

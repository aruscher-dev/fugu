#!/usr/bin/env python3
"""Phase 5 -- baseline + Open-Fugu blindfold matches (PLAN.md's phase table:
"5 conditions x ~25 games"): 3 solo-worker baselines, Phase 1's random-
routing orchestrator, and Phase 5's own per-query Open-Fugu SFT-routed
orchestrator (src/open_fugu/a2a/orchestrator_agent.py's FuguSelectionDispatch,
using Phase 4's trained checkpoint). Plays real games through the A2A
pipeline (src/open_fugu/eval/run_eval_matches.py does the process
launching/teardown per condition) and writes raw per-condition results.

Does NOT itself compute the ACPL/blunder-rate/win-rate evaluation report --
that's Phase 6's job (PLAN.md's phase table), same collect-then-aggregate
split as Phase 3 (collect) -> Phase 4 (aggregate + train). This phase's own
tracked summary (reports/phase5_summary.json) is a compact per-condition
digest only.

Idempotent per condition: writes logs/phase5_matches/<condition_id>.json
(gitignored) and skips any condition whose file already exists, unless
--force -- same resumability pattern as Phase 0.5/m2's per-worker loop, so a
crash-and-cron-relaunch just picks up whichever conditions are left. Long
background job (PLAN.md estimate: 1-3 days, ~25 GPU-hours) -- meant to span
many orchestrate.py/cron cycles.

Real GPU-hour spend against a worker pool whose floor check
(reports/phase0_5_summary.json) came back REVIEW_NEEDED, playing matches
with an orchestrator checkpoint (reports/phase4_summary.json) that same
verdict already flagged as needing a human sanity-check first -- see
scripts/orchestrate.py's advance_phase_5 for the gpu_spend_approved gate
this requires before it will launch automatically (mirrors Phase 3's gate).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from disk_guard import check_disk_budget  # noqa: E402
from open_fugu.eval.run_eval_matches import (  # noqa: E402
    DEFAULT_GAMES_PER_REQUEST,
    DEFAULT_OPENING,
    DEFAULT_WORKERS,
    default_conditions,
    run_condition,
    unknown_workers,
)

RESULTS_DIR = PROJECT_DIR / "logs" / "phase5_matches"  # gitignored, host-specific
REPORTS_DIR = PROJECT_DIR / "reports"  # tracked -- small summaries only
CHECKPOINT_PATH = PROJECT_DIR / "checkpoints" / "phase4_sft" / "backbone_head_svf.pt"

DEFAULT_N_GAMES = 25   # within PLAN.md's "~25 games" per condition
DEFAULT_MAX_PLIES = 60  # real matches, not Phase 0.5's short 24-ply floor check


def write_summary(conditions: list, results_dir: Path) -> dict:
    per_condition = {}
    for c in conditions:
        path = results_dir / f"{c.condition_id}.json"
        if not path.exists():
            continue
        r = json.loads(path.read_text())
        games = r.get("detail", {}).get("games", [])
        legal_rates = [g["legal_move_rate"] for g in games if g.get("legal_move_rate") is not None]
        per_condition[c.condition_id] = {
            "n_games": len(games),
            "mean_legal_move_rate": (sum(legal_rates) / len(legal_rates)) if legal_rates else None,
            "any_illegal_termination": any(g.get("termination") == "illegal_move" for g in games),
        }
    all_done = bool(conditions) and len(per_condition) == len(conditions)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "conditions": per_condition,
        "verdict": "COMPLETE" if all_done else "IN_PROGRESS",
        "note": ("compact per-condition digest only -- full per-game records (incl. "
                 "per-ply centipawn loss / blunder flags, from chess_green_agent.py's "
                 "EvalResult) live in logs/phase5_matches/<condition>.json (gitignored). "
                 "Phase 6 is what turns this into the full ACPL/blunder-rate/win-rate "
                 "evaluation report PLAN.md's Verification section calls for."),
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.chmod(0o700)
    out_path = REPORTS_DIR / "phase5_summary.json"
    out_path.write_text(json.dumps(summary, indent=2))
    out_path.chmod(0o600)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", nargs="*", default=DEFAULT_WORKERS,
                     help="Short ids from CANDIDATE_WORKERS -- the swarm the random/fugu "
                          "orchestrator conditions dispatch across, and the solo baselines tested")
    ap.add_argument("--n-games", type=int, default=DEFAULT_N_GAMES)
    ap.add_argument("--max-plies", type=int, default=DEFAULT_MAX_PLIES)
    ap.add_argument("--stockfish-skill-level", type=int, default=1)
    ap.add_argument("--games-per-request", type=int, default=DEFAULT_GAMES_PER_REQUEST)
    ap.add_argument("--checkpoint", default=str(CHECKPOINT_PATH),
                     help="Phase 4's trained checkpoint, used by the open_fugu_sft condition")
    ap.add_argument("--force", action="store_true", help="Re-run even if a condition's result already exists")
    args = ap.parse_args()

    bad = unknown_workers(args.workers)
    if bad:
        print(f"Unknown worker id(s) {bad}, aborting. Known: see CANDIDATE_WORKERS.")
        sys.exit(1)

    check_disk_budget()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.chmod(0o700)

    checkpoint_path = Path(args.checkpoint)
    conditions = default_conditions(args.workers, checkpoint_path if checkpoint_path.exists() else None)

    for condition in conditions:
        out_path = RESULTS_DIR / f"{condition.condition_id}.json"
        if out_path.exists() and not args.force:
            print(f"Skipping '{condition.condition_id}' (result already exists at {out_path})")
            continue
        if condition.kind == "orchestrator" and condition.router == "fugu" and not condition.checkpoint:
            print(f"Skipping '{condition.condition_id}': no checkpoint found at {checkpoint_path} yet "
                  f"(Phase 4 must complete first) -- will retry on the next run.")
            continue

        print(f"\n=== Condition: {condition.condition_id} ({args.n_games} games) ===", flush=True)
        try:
            result = run_condition(
                condition, args.workers, args.n_games, args.max_plies,
                args.stockfish_skill_level, DEFAULT_OPENING,
                games_per_request=args.games_per_request,
            )
        except Exception as e:
            print(f"  CRASHED: {type(e).__name__}: {e}")
            result = {"winner": "none", "detail": {"games": [], "mean_legal_move_rate": None,
                                                     "error": f"{type(e).__name__}: {e}"}}

        out_path.write_text(json.dumps(result, indent=2))
        out_path.chmod(0o600)
        mean_rate = result.get("detail", {}).get("mean_legal_move_rate")
        print(f"  wrote {out_path} (mean_legal_move_rate={mean_rate})")

    summary = write_summary(conditions, RESULTS_DIR)
    print(f"\nVerdict: {summary['verdict']}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Phase 3 -- SFT data collection (per the approved plan's training recipe,
step 1): sample ~300-600 blindfold-chess positions (self-play move-history
prefixes -- see src/open_fugu/data/chess_positions.py for why not FEN
puzzles), query every candidate worker n=3-4 times each on each position,
score every response via StockfishScorer, and write one JSONL record per
(worker, position, sample). Phase 4 is what turns this into r̄-per-worker/
position + a softmax-τ soft target distribution and actually trains against
it; this phase's job stops at "collect scored raw samples."

Long-running (PLAN.md estimate: 2-4 days background, ~10-20 GPU-hours for
the default 3-worker pool) -- meant to span many orchestrate.py/cron cycles,
same as Phase 0.5. Idempotent: positions are generated once (deterministic
given a fixed seed) and persisted to
logs/phase3_sft_data/positions.jsonl -- once that file exists it is the
source of truth, regardless of seed -- and each worker's own JSONL file
resumes from whatever (position_idx, sample_idx) pairs it already has.
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
from open_fugu.data.chess_positions import SampledPosition, generate_positions  # noqa: E402
from open_fugu.data.collect_sft_data import collect_for_worker, load_done_keys  # noqa: E402
from open_fugu.models.local_worker import CANDIDATE_WORKERS  # noqa: E402

RESULTS_DIR = PROJECT_DIR / "logs" / "phase3_sft_data"  # gitignored, host-specific -- *.jsonl in particular
POSITIONS_PATH = RESULTS_DIR / "positions.jsonl"
REPORTS_DIR = PROJECT_DIR / "reports"  # tracked -- small summaries only

# Per PLAN.md's Architecture section: "Start the SFT/eval pipeline with the
# smaller 3-4 model pool" -- reuses the exact 3 workers Phase 0.5 already
# floor-checked, rather than introducing untested workers into Phase 3.
DEFAULT_WORKERS = ["qwen2.5-7b", "mistral-7b", "deepseek-r1-distill-qwen-7b"]
DEFAULT_N_POSITIONS = 400   # within PLAN.md's 300-600 range
DEFAULT_N_SAMPLES = 4       # within PLAN.md's 3-4 samples/worker/position


def load_or_generate_positions(n_positions: int) -> list:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.chmod(0o700)
    if POSITIONS_PATH.exists():
        positions = []
        with POSITIONS_PATH.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                positions.append(SampledPosition(position_idx=rec["position_idx"],
                                                  opening_uci_moves=rec["opening_uci_moves"]))
        return positions

    positions = generate_positions(n_positions)
    with POSITIONS_PATH.open("w") as f:
        for p in positions:
            f.write(json.dumps({"position_idx": p.position_idx, "opening_uci_moves": p.opening_uci_moves}) + "\n")
    POSITIONS_PATH.chmod(0o600)
    return positions


def write_summary(workers: list, n_positions: int, n_samples: int) -> dict:
    per_worker = {}
    for w in workers:
        out_path = RESULTS_DIR / f"{w}.jsonl"
        done = load_done_keys(out_path)
        expected = n_positions * n_samples
        per_worker[w] = {
            "collected": len(done),
            "expected": expected,
            "complete": len(done) >= expected,
        }
    all_complete = bool(per_worker) and all(v["complete"] for v in per_worker.values())
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_positions": n_positions,
        "n_samples_per_position": n_samples,
        "workers": per_worker,
        "verdict": "COMPLETE" if all_complete else "IN_PROGRESS",
        "note": ("raw scored (position, worker, sample) records for Phase 4's SFT target "
                 "construction -- this phase does not itself compute mean reward per "
                 "worker/position or the softmax-tau soft target distribution."),
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.chmod(0o700)
    out_path = REPORTS_DIR / "phase3_summary.json"
    out_path.write_text(json.dumps(summary, indent=2))
    out_path.chmod(0o600)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", nargs="*", default=DEFAULT_WORKERS,
                     help="Short ids from CANDIDATE_WORKERS to collect data for")
    ap.add_argument("--n-positions", type=int, default=DEFAULT_N_POSITIONS)
    ap.add_argument("--n-samples", type=int, default=DEFAULT_N_SAMPLES)
    ap.add_argument("--max-new-tokens", type=int, default=256)
    ap.add_argument("--batch-size", type=int, default=32,
                     help="independent (position, sample) queries generated per model.generate() "
                          "call for non-reasoning workers (measured ~2.8x throughput at this size "
                          "on this host's RTX 3090) -- reasoning-distill workers always run "
                          "sequentially regardless of this flag, see collect_for_worker()")
    args = ap.parse_args()

    check_disk_budget()
    positions = load_or_generate_positions(args.n_positions)
    print(f"Using {len(positions)} positions from {POSITIONS_PATH}", flush=True)

    for short_id in args.workers:
        if short_id not in CANDIDATE_WORKERS:
            print(f"Unknown worker id '{short_id}', skipping. Known: {list(CANDIDATE_WORKERS)}")
            continue
        out_path = RESULTS_DIR / f"{short_id}.jsonl"
        expected = len(positions) * args.n_samples
        done_before = len(load_done_keys(out_path))
        if done_before >= expected:
            print(f"Skipping {short_id} ({done_before}/{expected} already collected)")
            continue
        print(f"\n=== Collecting SFT data: {short_id} ({done_before}/{expected} done) ===", flush=True)
        collect_for_worker(short_id, CANDIDATE_WORKERS[short_id], positions, args.n_samples, out_path,
                            max_new_tokens=args.max_new_tokens, batch_size=args.batch_size)
        done_after = len(load_done_keys(out_path))
        print(f"  {short_id}: {done_after}/{expected} records collected", flush=True)

    summary = write_summary(args.workers, len(positions), args.n_samples)
    print(f"\nVerdict: {summary['verdict']}")


if __name__ == "__main__":
    main()

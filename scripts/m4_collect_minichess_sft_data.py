#!/usr/bin/env python3
"""m4 -- SFT data collection on 5x5 Gardner Minichess (PLAN.md's minichess
phase table: "SFT data collection on 5x5 (reuse Phase 3's design, cheap
here)"). Direct twin of scripts/phase3_collect_sft_data.py, swapped onto
open_fugu.minichess.positions/collect_sft_data instead of
open_fugu.data.chess_positions/collect_sft_data -- same collect-then-summarize
discipline, same idempotent resume-from-JSONL contract.

Long-running relative to a single floor check but shorter than Phase 3 in
wall-clock (Gardner games/prompts are much shorter than full chess), still
launched via tmux/cron rather than run synchronously since it needs the GPU
and downloads worker models on first use, same as every other GPU phase.

Gated behind gpu_spend_approved in state.json (see advance_m4 in
scripts/orchestrate.py) -- unlike m3 (pipeline-wiring only, gated on
returncode not chess quality), this phase spends real GPU-hours collecting
data whose quality m2's floor check (reports/m2_gardner_floor_check_summary.json)
flagged as REVIEW_NEEDED with a 0% legal-move rate for ALL 3 default workers,
markedly worse than the full-chess track's own REVIEW_NEEDED (61%/64%/25%).
A human/dev session with real log access should look into *why* before
approving -- see that report and STATUS.md.
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
from open_fugu.data.chess_positions import SampledPosition  # noqa: E402
from open_fugu.minichess.collect_sft_data import collect_for_worker, load_done_keys  # noqa: E402
from open_fugu.minichess.positions import generate_gardner_positions  # noqa: E402
from open_fugu.models.local_worker import CANDIDATE_WORKERS  # noqa: E402

RESULTS_DIR = PROJECT_DIR / "logs" / "m4_minichess_sft_data"  # gitignored, host-specific -- *.jsonl in particular
POSITIONS_PATH = RESULTS_DIR / "positions.jsonl"
REPORTS_DIR = PROJECT_DIR / "reports"  # tracked -- small summaries only

# Same 3 workers m2's floor check already exercised, same reasoning Phase 3's
# own DEFAULT_WORKERS comment gives -- don't introduce untested workers here.
DEFAULT_WORKERS = ["qwen2.5-7b", "mistral-7b", "deepseek-r1-distill-qwen-7b"]
DEFAULT_N_POSITIONS = 400   # same as Phase 3, for direct comparability across tracks
DEFAULT_N_SAMPLES = 4       # same as Phase 3


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

    positions = generate_gardner_positions(n_positions)
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
        "note": ("raw scored (position, worker, sample) records on 5x5 Gardner Minichess for m5's "
                 "SFT target construction -- this phase does not itself compute mean reward per "
                 "worker/position or the softmax-tau soft target distribution, same split as Phase 3/4."),
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.chmod(0o700)
    out_path = REPORTS_DIR / "m4_summary.json"
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
                          "call for non-reasoning workers -- see collect_for_worker()'s docstring; "
                          "reasoning-distill workers always run sequentially regardless of this flag")
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
        print(f"\n=== Collecting m4 SFT data: {short_id} ({done_before}/{expected} done) ===", flush=True)
        collect_for_worker(short_id, CANDIDATE_WORKERS[short_id], positions, args.n_samples, out_path,
                            max_new_tokens=args.max_new_tokens, batch_size=args.batch_size)
        done_after = len(load_done_keys(out_path))
        print(f"  {short_id}: {done_after}/{expected} records collected", flush=True)

    summary = write_summary(args.workers, len(positions), args.n_samples)
    print(f"\nVerdict: {summary['verdict']}")


if __name__ == "__main__":
    main()

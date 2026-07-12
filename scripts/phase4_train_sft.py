#!/usr/bin/env python3
"""Phase 4 -- SVF + selection head implementation, SFT training (PLAN.md
training recipe step 1, continued from Phase 3): load Phase 3's raw scored
records, build a per-position soft target distribution over the worker
swarm (mean reward -> softmax-tau), then train the orchestrator backbone's
selection head + SVF z vectors against it (src/open_fugu/train/train_sft.py)
via a plain AdamW loop. Writes the trained head+SVF state dict to
checkpoints/ (gitignored, host-specific) and a summary to
reports/phase4_summary.json.

Short relative to Phase 3 (PLAN.md estimate: 0.5-1 day) -- head+SVF-z is a
tiny parameter count and Phase 3 already paid the expensive part (worker
inference + Stockfish scoring). Still launched via tmux/cron like every
other GPU phase rather than run synchronously, since it needs the GPU and
downloads the orchestrator backbone model on first use.

Does NOT itself play any blindfold games -- this only produces a checkpoint.
Phase 5 is what plays Open-Fugu (this checkpoint's routing) vs. baselines.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from disk_guard import check_disk_budget  # noqa: E402
from open_fugu.train.train_sft import (  # noqa: E402
    build_soft_targets, load_jsonl, load_positions, split_train_val, train,
)

SFT_DATA_DIR = PROJECT_DIR / "logs" / "phase3_sft_data"      # Phase 3's output (gitignored)
POSITIONS_PATH = SFT_DATA_DIR / "positions.jsonl"
CHECKPOINT_DIR = PROJECT_DIR / "checkpoints" / "phase4_sft"  # gitignored, host-specific
REPORTS_DIR = PROJECT_DIR / "reports"

# Must match Phase 3's collected pool (scripts/phase3_collect_sft_data.py's
# DEFAULT_WORKERS) -- a soft target needs every worker's data for the same
# position, so training against a different pool than what Phase 3 actually
# collected would silently drop every position down to zero targets.
DEFAULT_WORKERS = ["qwen2.5-7b", "mistral-7b", "deepseek-r1-distill-qwen-7b"]


def load_worker_records(workers: list) -> dict:
    out = {}
    for w in workers:
        path = SFT_DATA_DIR / f"{w}.jsonl"
        if not path.exists():
            raise FileNotFoundError(
                f"Missing Phase 3 output for worker '{w}': {path} -- Phase 3 must complete "
                f"for every requested worker before Phase 4 can build soft targets."
            )
        out[w] = load_jsonl(path)
    return out


def write_summary(n_targets: int, n_positions: int, workers: list, losses: list,
                   val_losses: list, n_val: int, checkpoint_path: Path) -> dict:
    uniform_baseline = math.log(len(workers)) if workers else None
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_positions_available": n_positions,
        "n_soft_targets": n_targets,
        "n_val_targets": n_val,
        "workers": workers,
        "final_loss": losses[-1] if losses else None,
        "mean_loss_last_50": (sum(losses[-50:]) / len(losses[-50:])) if losses else None,
        "val_loss_per_epoch": val_losses,
        "final_val_loss": val_losses[-1] if val_losses else None,
        "uniform_baseline_cross_entropy": uniform_baseline,
        "checkpoint_path": str(checkpoint_path) if losses else None,
        "verdict": "COMPLETE" if n_targets > 0 and losses else "NO_DATA",
        "note": ("selection head + SVF-z state dict trained via SFT (cross-entropy vs. a "
                 "softmax-tau soft target built from Phase 3's per-worker mean reward per "
                 "position), now with a held-out validation split (split_train_val(), never "
                 "trained on) and per-epoch train-order shuffling -- 2026-07-12 fix for the "
                 "deep-review finding that the original run's final_loss/mean_loss_last_50 "
                 "were in-sample-only and couldn't distinguish real routing signal from "
                 "memorizing a fixed-order training set. Compare final_val_loss against "
                 "uniform_baseline_cross_entropy (ln(n_workers), i.e. what a routing head "
                 "that ignores the position entirely would score) -- val_loss should be "
                 "meaningfully BELOW that baseline and should trend down (not flat/up) across "
                 "val_loss_per_epoch before trusting this checkpoint. Does not itself play any "
                 "blindfold games -- Phase 5 is what actually plays Open-Fugu (this "
                 "checkpoint's routing) vs. baselines."),
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.chmod(0o700)
    out_path = REPORTS_DIR / "phase4_summary.json"
    out_path.write_text(json.dumps(summary, indent=2))
    out_path.chmod(0o600)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", nargs="*", default=DEFAULT_WORKERS)
    ap.add_argument("--backbone-model-id", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--tau", type=float, default=1.0)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--svf-n-last-layers", type=int, default=3)
    ap.add_argument("--val-frac", type=float, default=0.15,
                     help="fraction of soft targets held out for validation, never trained on")
    ap.add_argument("--split-seed", type=int, default=0)
    args = ap.parse_args()

    check_disk_budget()

    if not POSITIONS_PATH.exists():
        print(f"No Phase 3 positions found at {POSITIONS_PATH} -- Phase 3 must run first.")
        sys.exit(1)

    positions = load_positions(POSITIONS_PATH)
    worker_records = load_worker_records(args.workers)
    targets = build_soft_targets(positions, worker_records, tau=args.tau)
    print(f"Built {len(targets)} soft targets from {len(positions)} positions x "
          f"{len(args.workers)} workers", flush=True)

    checkpoint_path = CHECKPOINT_DIR / "backbone_head_svf.pt"
    if not targets:
        write_summary(0, len(positions), args.workers, [], [], 0, checkpoint_path)
        print("No position has scored data from every requested worker -- nothing to train on.")
        return

    train_targets, val_targets = split_train_val(targets, val_frac=args.val_frac, seed=args.split_seed)
    print(f"Split into {len(train_targets)} train / {len(val_targets)} held-out val targets "
          f"(val_frac={args.val_frac}, seed={args.split_seed})", flush=True)

    import torch

    from open_fugu.models.worker_backend import OrchestratorBackbone, OrchestratorBackboneConfig

    config = OrchestratorBackboneConfig(
        worker_ids=targets[0].worker_ids,
        backbone_model_id=args.backbone_model_id,
        svf_n_last_layers=args.svf_n_last_layers,
    )
    backbone = OrchestratorBackbone(config)
    losses, val_losses = train(train_targets, val_targets, backbone, epochs=args.epochs, lr=args.lr)

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_DIR.chmod(0o700)
    state = {
        "selection_head": backbone.selection_head.state_dict(),
        "svf_z": [m.z.detach().cpu() for m in backbone.svf_linears],
        "worker_ids": config.worker_ids,
        "backbone_model_id": config.backbone_model_id,
        "svf_n_last_layers": config.svf_n_last_layers,
    }
    torch.save(state, checkpoint_path)
    checkpoint_path.chmod(0o600)

    summary = write_summary(len(targets), len(positions), args.workers, losses,
                             val_losses, len(val_targets), checkpoint_path)
    print(f"\nVerdict: {summary['verdict']} (final_loss={summary['final_loss']}, "
          f"final_val_loss={summary['final_val_loss']}, "
          f"uniform_baseline={summary['uniform_baseline_cross_entropy']:.4f})")


if __name__ == "__main__":
    main()

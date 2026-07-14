#!/usr/bin/env python3
"""m6 -- sep-CMA-ES evolutionary fine-tuning on 5x5 Gardner Minichess
(PLAN.md's minichess phase table: "originally stretch Phase 8 for full
chess -- done here first since it's cheap and de-risks that stretch goal").
Direct twin of scripts/phase8_cmaes_chess_pilot.py, swapped onto
GardnerBoard/GardnerScorer via open_fugu.train.rollout_chess.play_one_rollout's
new board_factory= parameter (added this session -- rollout_chess.py turned
out to already be board-agnostic everywhere else, same "duck-typed, thread a
board_factory through" pattern m1 established for harness.py itself, rather
than forking a whole new rollout module).

What this evolves: the Fugu orchestrator's selection head (weight+bias
only), same scoping decision Phase 7/8 made and flagged as open --
SVF's `z` vectors stay frozen at their no-op default. Starts from a FRESH,
randomly-initialized OrchestratorBackbone (does NOT load m5's SFT
checkpoint) -- same as Phase 8's own design: this produces this track's
coordination checkpoint #3 (CMA-ES-evolved routing) as an independently
evolved policy over the same worker pool, not a fine-tune of checkpoint #2
(m5's SFT head). m7's fixed eval suite is what actually compares all three
checkpoints (m3 random / m5 SFT / m6 CMA-ES) against each other.

Rollouts are real per-query Fugu-routed blindfold games
(open_fugu.train.rollout_chess.make_dispatch_move_fn, in-process rather than
over A2A, same reasoning Phase 8's own docstring gives: CMA-ES needs far
more rollouts per generation than any one A2A condition plays) against a
fixed, very weak local Fairy-Stockfish opponent (GardnerScorer at a shallow
depth) -- explicitly NOT a chess-strength claim, matching PLAN.md's
Verification section ("Any CMA-ES results (stretch phases) are explicitly
reported as scoped proof-of-concept"), same as Phase 8.

"Truncated" games, same reasoning as Phase 8: keeping each rollout's real
7-8B-model generation cost bounded is why max_plies defaults short and the
reward blends in graded -ACPL (rollout_chess.blend_reward) rather than
relying on win/loss/draw alone -- most truncated games never reach a
decisive result.

Needs the GPU (a real OrchestratorBackbone + the same worker pool m2/m4/m5
use, all loaded at once) plus pyffish/bin/fairy-stockfish -- cannot run in
the cloud dev sandbox that wrote this file; see reports/m6_summary.json's
gpu_spend_approved-gated advancer (advance_m6) in scripts/orchestrate.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from disk_guard import check_disk_budget  # noqa: E402
from open_fugu.minichess.board import BLACK, WHITE, GardnerBoard  # noqa: E402
from open_fugu.minichess.engine import GardnerScorer  # noqa: E402
from open_fugu.models.local_worker import CANDIDATE_WORKERS, LocalWorker, LocalWorkerConfig  # noqa: E402

REPORT_PATH = PROJECT_DIR / "reports" / "m6_summary.json"
CHECKPOINT_DIR = PROJECT_DIR / "checkpoints" / "m6_cmaes"

DEFAULT_WORKERS = ["qwen2.5-7b", "mistral-7b", "deepseek-r1-distill-qwen-7b"]  # same pool as m2/m4/m5
# "pawn_knight_skirmish_c" -- taken verbatim from scripts/m2_gardner_floor_check.py's
# hand-verified GARDNER_OPENING_BOOK / a2a/chess_green_agent.py's DEFAULT_OPENING_GARDNER,
# reused rather than re-verified (same convention m3 already established).
DEFAULT_OPENING = ["c2c3", "b4c3", "b2c3", "b5c3"]
DEFAULT_MAX_PLIES = 16          # "truncated" -- keeps one rollout's LLM-call cost bounded, same as Phase 8
GARDNER_ENGINE_DEPTH = 1        # shallowest of positions.py's DEPTH_LEVELS -- very weak, matches Phase 8's STOCKFISH_OPPONENT_SKILL=1 intent (Fairy-Stockfish has no confirmed Skill Level option, per m2's own note, so depth is the weakening lever here instead)


def build_worker_pool(worker_ids: list, max_new_tokens: int, temperature: float, device_map: str) -> dict:
    pool = {}
    for short_id in worker_ids:
        if short_id not in CANDIDATE_WORKERS:
            raise ValueError(f"Unknown worker '{short_id}' -- not in models.local_worker.CANDIDATE_WORKERS")
        cfg = LocalWorkerConfig(model_id=CANDIDATE_WORKERS[short_id], max_new_tokens=max_new_tokens,
                                 temperature=temperature, device_map=device_map)
        pool[short_id] = LocalWorker(cfg)
    return pool


def make_fitness_fn(backbone, worker_pool: dict, weight_shape, bias_shape, scorer, opening: list,
                     max_plies: int, n_games_per_eval: int):
    import torch

    from open_fugu.train.rollout_chess import make_dispatch_move_fn, play_one_rollout
    from open_fugu.train.train_cmaes import unflatten_params

    def fitness_fn(flat_params):
        weight, bias = unflatten_params(flat_params, weight_shape, bias_shape)
        head = backbone.selection_head
        with torch.no_grad():
            head.weight.copy_(torch.as_tensor(weight, dtype=head.weight.dtype, device=head.weight.device))
            head.bias.copy_(torch.as_tensor(bias, dtype=head.bias.dtype, device=head.bias.device))

        move_fn = make_dispatch_move_fn(backbone, worker_pool)
        rewards = []
        for i in range(n_games_per_eval):
            llm_color = WHITE if i % 2 == 0 else BLACK
            rollout = play_one_rollout(move_fn, llm_color, opening, scorer, max_plies,
                                        board_factory=GardnerBoard)
            rewards.append(rollout.reward)
        return sum(rewards) / len(rewards)

    return fitness_fn


def final_eval(backbone, worker_pool: dict, scorer, opening: list, max_plies: int, n_games: int) -> dict:
    from open_fugu.train.rollout_chess import make_dispatch_move_fn, play_one_rollout

    move_fn = make_dispatch_move_fn(backbone, worker_pool)
    rollouts = []
    for i in range(n_games):
        llm_color = WHITE if i % 2 == 0 else BLACK
        rollouts.append(play_one_rollout(move_fn, llm_color, opening, scorer, max_plies,
                                          board_factory=GardnerBoard))

    return {
        "n_games": n_games,
        "win_rate": sum(1 for r in rollouts if r.outcome == "win") / n_games,
        "loss_rate": sum(1 for r in rollouts if r.outcome == "loss") / n_games,
        "draw_rate": sum(1 for r in rollouts if r.outcome == "draw") / n_games,
        "unresolved_rate": sum(1 for r in rollouts if r.outcome == "unresolved") / n_games,
        "mean_reward": sum(r.reward for r in rollouts) / n_games,
        "mean_centipawn_loss": (
            sum(r.mean_centipawn_loss for r in rollouts if r.mean_centipawn_loss is not None)
            / max(1, sum(1 for r in rollouts if r.mean_centipawn_loss is not None))
        ) if any(r.mean_centipawn_loss is not None for r in rollouts) else None,
    }


def write_summary(args, n_params: int, cma_result, held_out: dict, checkpoint_path: Path) -> dict:
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "game": "gardner_minichess_blindfold_truncated",
        "workers": args.workers,
        "backbone_model_id": args.backbone_model_id,
        "n_evolved_params": n_params,
        "cmaes": {
            "n_generations": args.n_generations,
            "popsize": args.popsize,
            "n_games_per_eval": args.n_games_per_eval,
            "max_plies": args.max_plies,
            "sigma0": args.sigma0,
            "best_fitness_mean_reward": cma_result.best_fitness,
            "history_best": cma_result.history_best,
            "history_mean": cma_result.history_mean,
        },
        "held_out_eval_vs_weak_fairy_stockfish": held_out,
        "checkpoint_path": str(checkpoint_path),
        "verdict": "COMPLETE",
        "note": ("Scoped proof-of-concept only (PLAN.md's Verification section: 'Any CMA-ES "
                 "results (stretch phases) are explicitly reported as scoped proof-of-concept'), "
                 "direct twin of Phase 8's own (full-chess) run -- this run's n_generations/popsize/"
                 "n_games_per_eval are deliberately modest (real 7-8B-model generation per ply, per "
                 "rollout, per candidate adds up fast even on a 5x5 board) and should NOT be read as "
                 "a converged result. Validates that open_fugu.train.train_cmaes.run_cmaes (already "
                 "proven against real kuhn_poker reward in Phase 7, and against real full-chess "
                 "rollouts in Phase 8) also works end-to-end against real Gardner Minichess blindfold "
                 "rollouts via open_fugu.train.rollout_chess.play_one_rollout's new board_factory= "
                 "parameter. Opponent is local Fairy-Stockfish (GardnerScorer) at depth "
                 f"{GARDNER_ENGINE_DEPTH} (very weak, shallowest of positions.py's DEPTH_LEVELS) -- "
                 "chosen as a cheap, always-available fixed baseline to validate the training loop "
                 "against, not a benchmark. Evolved parameters are the selection head's weight+bias "
                 "ONLY -- SVF's `z` vectors are frozen at their default no-op value here (same scoping "
                 "decision Phase 7/8 made). Starts from a freshly-initialized OrchestratorBackbone, NOT "
                 "m5's SFT checkpoint -- this is coordination checkpoint #3 (an independently-evolved "
                 "routing policy), evaluated against checkpoints #1 (m3 random routing) and #2 (m5 SFT) "
                 "by m7's fixed eval suite, not a fine-tune of #2. IMPORTANT CAVEAT carried over from "
                 "m4/m5: m2's floor check (reports/m2_gardner_floor_check_summary.json) is REVIEW_NEEDED "
                 "with a 0% legal-move rate for all 3 default workers on this board size -- a future "
                 "session should weigh that when interpreting this run's win/loss/draw/unresolved "
                 "rates, same evidence-based-verdict standard used throughout this track."),
    }
    reports_dir = PROJECT_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.chmod(0o700)
    REPORT_PATH.write_text(json.dumps(summary, indent=2))
    REPORT_PATH.chmod(0o600)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", nargs="*", default=DEFAULT_WORKERS)
    ap.add_argument("--backbone-model-id", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--svf-n-last-layers", type=int, default=3)
    ap.add_argument("--worker-max-tokens", type=int, default=200)  # matches Phase 0.5/8's post-fix bump
    ap.add_argument("--worker-temperature", type=float, default=0.7)
    ap.add_argument("--max-plies", type=int, default=DEFAULT_MAX_PLIES)
    ap.add_argument("--n-generations", type=int, default=6)
    ap.add_argument("--popsize", type=int, default=4)
    ap.add_argument("--n-games-per-eval", type=int, default=4)
    ap.add_argument("--n-final-eval-games", type=int, default=16)
    ap.add_argument("--sigma0", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--engine-depth", type=int, default=GARDNER_ENGINE_DEPTH)
    args = ap.parse_args()

    if REPORT_PATH.exists():
        existing = json.loads(REPORT_PATH.read_text())
        if existing.get("verdict") == "COMPLETE":
            print(f"{REPORT_PATH} already COMPLETE, nothing to do (delete it to force a re-run).")
            return

    check_disk_budget()

    import torch

    from open_fugu.models.worker_backend import OrchestratorBackbone, OrchestratorBackboneConfig
    from open_fugu.train.train_cmaes import flatten_params, run_cmaes, unflatten_params

    backbone_config = OrchestratorBackboneConfig(
        worker_ids=args.workers,
        backbone_model_id=args.backbone_model_id,
        svf_n_last_layers=args.svf_n_last_layers,
        device=args.device,
    )
    backbone = OrchestratorBackbone(backbone_config)
    backbone.eval()

    worker_pool = build_worker_pool(args.workers, args.worker_max_tokens, args.worker_temperature, args.device)
    scorer = GardnerScorer(depth=args.engine_depth)

    weight_shape = tuple(backbone.selection_head.weight.shape)
    bias_shape = tuple(backbone.selection_head.bias.shape)
    x0 = flatten_params(
        backbone.selection_head.weight.detach().cpu().numpy(),
        backbone.selection_head.bias.detach().cpu().numpy(),
    )
    print(f"Evolving {x0.shape[0]} selection-head parameters "
          f"(weight {weight_shape} + bias {bias_shape}) over {len(args.workers)} workers, "
          f"max_plies={args.max_plies} on 5x5 Gardner Minichess", flush=True)

    try:
        fitness_fn = make_fitness_fn(backbone, worker_pool, weight_shape, bias_shape, scorer,
                                      DEFAULT_OPENING, args.max_plies, args.n_games_per_eval)

        result = run_cmaes(fitness_fn, x0, sigma0=args.sigma0, n_generations=args.n_generations,
                            popsize=args.popsize, seed=args.seed, maximize=True)

        best_weight, best_bias = unflatten_params(result.best_params, weight_shape, bias_shape)
        with torch.no_grad():
            backbone.selection_head.weight.copy_(
                torch.as_tensor(best_weight, dtype=backbone.selection_head.weight.dtype,
                                 device=backbone.selection_head.weight.device))
            backbone.selection_head.bias.copy_(
                torch.as_tensor(best_bias, dtype=backbone.selection_head.bias.dtype,
                                 device=backbone.selection_head.bias.device))

        held_out = final_eval(backbone, worker_pool, scorer, DEFAULT_OPENING, args.max_plies,
                               args.n_final_eval_games)
        print(f"Held-out eval vs. weak Fairy-Stockfish: {held_out}", flush=True)
    finally:
        scorer.close()

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_DIR.chmod(0o700)
    checkpoint_path = CHECKPOINT_DIR / "selection_head.pt"
    torch.save({
        "selection_head": backbone.selection_head.state_dict(),
        # SVF's z is frozen at its no-op default throughout this run (see
        # module docstring -- only the selection head is evolved), but saved
        # anyway for shape-consistency with m5/Phase 4's SFT checkpoints:
        # m7's fixed eval suite loads all three coordination checkpoints
        # through the same open_fugu.models.worker_backend.load_from_checkpoint
        # (which tolerates this key's absence, but writing it here means this
        # checkpoint is never the special case).
        "svf_z": [m.z.detach().cpu() for m in backbone.svf_linears],
        "worker_ids": args.workers,
        "backbone_model_id": args.backbone_model_id,
        "svf_n_last_layers": args.svf_n_last_layers,
        "game": "gardner_minichess_blindfold_truncated",
    }, checkpoint_path)
    checkpoint_path.chmod(0o600)

    summary = write_summary(args, x0.shape[0], result, held_out, checkpoint_path)
    print(f"\nVerdict: {summary['verdict']} (mean_reward={held_out['mean_reward']})")


if __name__ == "__main__":
    main()

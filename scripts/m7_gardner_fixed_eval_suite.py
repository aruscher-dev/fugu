#!/usr/bin/env python3
"""m7 -- fixed evaluation suite on 5x5 Gardner Minichess (PLAN.md's minichess
phase table: "run all 3 checkpoints (m3 random / m5 SFT / m6 CMA-ES) through
the same fixed set of openings/positions, log full move-by-move data
(routing choice, worker move, legality, ACPL, outcome) to
reports/minichess_demo/*.json"). This is the data source m8's interactive
HTML demo (Claude Artifact) is built from -- unlike every earlier phase's
logs/ (gitignored, host-only), these per-game records are written straight
under reports/ (tracked, per PLAN.md's own instruction), so a future session
with no GPU-host access at all can still build m8 directly from this file.

Runs all three coordination checkpoints IN-PROCESS
(open_fugu.train.rollout_chess -- the same machinery m6's CMA-ES pilot
already validated against real GardnerBoard/GardnerScorer rollouts) rather
than over A2A (Phase 5's per-condition-subprocess approach): all three
conditions here share the exact same worker pool weights, so loading it once
and reusing it across conditions is both simpler and avoids paying A2A's
per-condition process-startup cost 3x for no benefit (m7 doesn't need
conditions running as separate long-lived services, only as separate move_fn
policies over the same in-memory workers) -- same reasoning Phase 7/8/m6
already gave for staying in-process.

The three conditions:
  - "m3_random": open_fugu.train.rollout_chess.make_sticky_random_move_fn,
    an in-process twin of a2a.orchestrator_agent.RandomStickyDispatch's
    per-game policy. m3 itself never produced a checkpoint file (it only
    proved the A2A pipeline runs end-to-end on this board size, gated on
    smoke-test returncode not chess quality per its own note) -- this
    replays that SAME "one random worker per game" routing policy rather
    than loading a file that was never meant to exist.
  - "m5_sft": open_fugu.models.worker_backend.load_from_checkpoint against
    checkpoints/m5_sft/backbone_head_svf.pt, then
    open_fugu.train.rollout_chess.make_dispatch_move_fn.
  - "m6_cmaes": same, against checkpoints/m6_cmaes/selection_head.pt.

Fixed openings: scripts/m2_gardner_floor_check.py's GARDNER_OPENING_BOOK (4
hand-verified lines -- reused verbatim rather than re-verified, same
convention m3/m6 already established for this constant, see that book's own
docstring for the pyffish.legal_moves() ply-by-ply verification each line
already got). One game per opening per condition (12 games total), LLM color
alternating by opening index so every condition plays the identical
(opening, color) pairing -- the actual "same fixed set of openings"
comparison PLAN.md's m7 row asks for. Deliberately small relative to
m4/m6's GPU spend: m7 is a fixed comparison suite over already-trained
checkpoints, not a training loop that needs many samples per candidate.

Per-ply routing choice is captured via make_dispatch_move_fn/
make_sticky_random_move_fn's new `routing_log` parameter (added this
session) rather than harness.PlyRecord.worker_id, which
play_blindfold_vs_engine only ever sets from a single fixed `worker_id=`
argument for the whole game -- correct for a solo worker, not expressive
enough for a per-query router whose choice can change every ply.

GATED behind minichess_phases["m7"].gpu_spend_approved (see
scripts/orchestrate.py's advance_m7): plays real games against the same
3-worker pool m2's floor check flagged REVIEW_NEEDED (0% legal-move rate for
all 3 default workers), same reasoning as m4/m6's gates -- even though the
total game count here (12) is much smaller than either, this still spends
real GPU-hours evaluating chess quality against that same flagged pool, not
just pipeline wiring (m3's own, ungated, precedent).

Needs the GPU (worker pool + orchestrator backbone loaded at once) plus
pyffish/bin/fairy-stockfish -- cannot run in the cloud dev sandbox that
wrote this file; see reports/m7_summary.json's gpu_spend_approved-gated
advancer (advance_m7) in scripts/orchestrate.py.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from disk_guard import check_disk_budget  # noqa: E402
from open_fugu.chess_blindfold.harness import play_blindfold_vs_engine  # noqa: E402
from open_fugu.eval.aggregate_metrics import game_outcome  # noqa: E402
from open_fugu.minichess.board import BLACK, GARDNER_VARIANT_DESCRIPTION, WHITE, GardnerBoard  # noqa: E402
from open_fugu.minichess.engine import GardnerScorer  # noqa: E402
from open_fugu.models.local_worker import (  # noqa: E402
    CANDIDATE_WORKERS, LocalWorker, LocalWorkerConfig, scaled_max_new_tokens,
)
from open_fugu.models.worker_backend import load_from_checkpoint  # noqa: E402
from open_fugu.train.rollout_chess import make_dispatch_move_fn, make_sticky_random_move_fn  # noqa: E402

REPORT_PATH = PROJECT_DIR / "reports" / "m7_summary.json"
DEMO_DIR = PROJECT_DIR / "reports" / "minichess_demo"  # tracked (per PLAN.md) -- m8's data source

DEFAULT_WORKERS = ["qwen2.5-7b", "mistral-7b", "deepseek-r1-distill-qwen-7b"]  # same pool as m2/m4/m5/m6

# Verbatim from scripts/m2_gardner_floor_check.py's GARDNER_OPENING_BOOK,
# reused rather than re-verified -- see that constant's own docstring for
# the pyffish.legal_moves() ply-by-ply verification each line already got.
GARDNER_OPENING_BOOK = {
    "pawn_knight_skirmish_c": ["c2c3", "b4c3", "b2c3", "b5c3"],
    "knight_pawn_flank_b": ["b1a3", "b4b3", "a2b3", "e4e3"],
    "pawn_queen_skirmish_d": ["d2d3", "e4d3", "e2d3", "d5e4"],
    "pawn_bishop_skirmish_e": ["e2e3", "d4e3", "d2e3", "c5e3"],
}

MAX_PLIES = 24          # matches m2's evaluation-scale (not m6's cost-truncated 16 -- m7 isn't a training rollout)
ENGINE_EVAL_DEPTH = 8   # matches m2's floor-check depth, for cross-phase comparability on this track

CHECKPOINTS = {
    "m5_sft": PROJECT_DIR / "checkpoints" / "m5_sft" / "backbone_head_svf.pt",
    "m6_cmaes": PROJECT_DIR / "checkpoints" / "m6_cmaes" / "selection_head.pt",
}
CONDITIONS = ["m3_random", "m5_sft", "m6_cmaes"]
UPSTREAM_PHASE = {"m5_sft": "m5", "m6_cmaes": "m6"}


def build_worker_pool(worker_ids: list, max_new_tokens_base: int, temperature: float, device_map: str) -> dict:
    pool = {}
    for short_id in worker_ids:
        if short_id not in CANDIDATE_WORKERS:
            raise ValueError(f"Unknown worker '{short_id}' -- not in models.local_worker.CANDIDATE_WORKERS")
        gen_budget = scaled_max_new_tokens(short_id, max_new_tokens_base)  # reasoning-distill workers get more room
        cfg = LocalWorkerConfig(model_id=CANDIDATE_WORKERS[short_id], max_new_tokens=gen_budget,
                                 temperature=temperature, device_map=device_map)
        pool[short_id] = LocalWorker(cfg)
    return pool


def load_done_games(condition_path: Path) -> dict:
    """{opening_name: game dict} already recorded for this condition, so a
    crash-and-cron-relaunch resumes instead of re-playing (and re-billing
    GPU-hours for) games already finished -- same resumability discipline
    m4/collect_sft_data.py's load_done_keys() already established for this
    track."""
    if not condition_path.exists():
        return {}
    return {g["opening"]: g for g in json.loads(condition_path.read_text())}


def write_condition_games(condition_path: Path, games_by_opening: dict) -> None:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    DEMO_DIR.chmod(0o700)
    ordered = [games_by_opening[name] for name in GARDNER_OPENING_BOOK if name in games_by_opening]
    condition_path.write_text(json.dumps(ordered, indent=2))
    condition_path.chmod(0o600)


def play_one_game(condition: str, opening_name: str, opening_moves: list, llm_color: bool,
                   move_fn_factory, scorer_depth: int) -> dict:
    routing_log: list = []
    move_fn = move_fn_factory(routing_log)
    scorer = GardnerScorer(depth=scorer_depth)
    try:
        result = play_blindfold_vs_engine(
            move_fn=move_fn,
            llm_color=llm_color,
            opening_uci_moves=opening_moves,
            engine_best_move_fn=scorer.best_move,
            scorer=scorer,
            max_plies=MAX_PLIES,
            board_factory=GardnerBoard,
            variant_description=GARDNER_VARIANT_DESCRIPTION,
        )
    finally:
        scorer.close()

    llm_color_str = "white" if llm_color == WHITE else "black"
    legal_plies = [p for p in result.plies if p.legal]
    losses = [p.centipawn_loss for p in legal_plies if p.centipawn_loss is not None]

    # routing_log has exactly one entry per LLM ply attempted, appended in
    # the same order result.plies grows (both driven one-per-call by
    # harness.py's llm_turn(), see move_fn_factory's move_fn) -- zip them to
    # attach the REAL per-ply routing choice, overriding harness.py's own
    # PlyRecord.worker_id (which play_blindfold_vs_engine only ever sets
    # from a single fixed `worker_id=` argument for the whole game -- fine
    # for a solo worker, not expressive enough for a per-query router whose
    # choice can change every ply). Falls back to the (None) PlyRecord value
    # only if a mismatch ever occurred, so this can't raise IndexError.
    return {
        "condition": condition,
        "opening": opening_name,
        "opening_uci_moves": opening_moves,
        "llm_color": llm_color_str,
        "result": result.result,
        "termination": result.termination,
        "final_fen": result.final_fen,
        "outcome": game_outcome({"result": result.result, "llm_color": llm_color_str}),
        "legal_move_rate": (len(legal_plies) / len(result.plies)) if result.plies else None,
        "mean_acpl": (sum(losses) / len(losses)) if losses else None,
        "blunder_rate": (sum(p.is_blunder for p in legal_plies) / len(legal_plies)) if legal_plies else None,
        "plies": [
            {
                "ply": p.ply,
                "mover": p.mover,
                "worker_id": routing_log[i] if i < len(routing_log) else p.worker_id,
                "move_uci": p.move_uci,
                "raw_reply": p.raw_reply,
                "legal": p.legal,
                "centipawn_loss": p.centipawn_loss,
                "is_blunder": p.is_blunder,
                "is_mistake": p.is_mistake,
            }
            for i, p in enumerate(result.plies)
        ],
    }


def crashed_game_record(condition: str, opening_name: str, opening_moves: list, llm_color: bool, exc: Exception) -> dict:
    llm_color_str = "white" if llm_color == WHITE else "black"
    return {
        "condition": condition,
        "opening": opening_name,
        "opening_uci_moves": opening_moves,
        "llm_color": llm_color_str,
        "result": "*",
        "termination": f"error: {type(exc).__name__}: {exc}",
        "final_fen": None,
        "outcome": "unresolved",
        "legal_move_rate": None,
        "mean_acpl": None,
        "blunder_rate": None,
        "plies": [],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", nargs="*", default=DEFAULT_WORKERS)
    ap.add_argument("--worker-max-tokens", type=int, default=200)
    ap.add_argument("--worker-temperature", type=float, default=0.4)  # matches m2's floor-check temperature (evaluation, not exploratory sampling)
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--device-map", default="cuda:0")
    ap.add_argument("--engine-depth", type=int, default=ENGINE_EVAL_DEPTH)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="re-play games already recorded")
    args = ap.parse_args()

    if REPORT_PATH.exists():
        existing = json.loads(REPORT_PATH.read_text())
        if existing.get("verdict") == "COMPLETE":
            print(f"{REPORT_PATH} already COMPLETE, nothing to do (delete it to force a re-run).")
            return

    check_disk_budget()

    worker_pool = build_worker_pool(args.workers, args.worker_max_tokens, args.worker_temperature, args.device_map)

    condition_status = {}
    for condition in CONDITIONS:
        condition_path = DEMO_DIR / f"{condition}.json"
        done = {} if args.force else load_done_games(condition_path)

        if condition == "m3_random":
            rng = random.Random(args.seed)  # fixed seed -- deterministic worker pick across re-runs/resumes

            def move_fn_factory(routing_log, rng=rng):
                return make_sticky_random_move_fn(worker_pool, rng, max_new_tokens=args.worker_max_tokens,
                                                   routing_log=routing_log)
        else:
            checkpoint_path = CHECKPOINTS[condition]
            if not checkpoint_path.exists():
                print(f"[m7] {condition}: checkpoint not found at {checkpoint_path} -- skipping "
                      f"(expected once minichess_phases['{UPSTREAM_PHASE[condition]}'] reaches 'done'; "
                      f"advance_track's own phase ordering should prevent this script from being launched "
                      f"before then, but this script is defensive regardless).")
                condition_status[condition] = "missing_checkpoint"
                continue
            backbone = load_from_checkpoint(checkpoint_path, args.workers, device=args.device)

            def move_fn_factory(routing_log, backbone=backbone):
                return make_dispatch_move_fn(backbone, worker_pool, max_new_tokens=args.worker_max_tokens,
                                              routing_log=routing_log)

        for i, (opening_name, opening_moves) in enumerate(GARDNER_OPENING_BOOK.items()):
            if opening_name in done:
                continue
            llm_color = WHITE if i % 2 == 0 else BLACK
            print(f"[m7] {condition} / {opening_name} (llm={'white' if llm_color == WHITE else 'black'})...",
                  flush=True)
            try:
                game = play_one_game(condition, opening_name, opening_moves, llm_color, move_fn_factory,
                                      args.engine_depth)
            except Exception as e:
                game = crashed_game_record(condition, opening_name, opening_moves, llm_color, e)
                print(f"  CRASHED: {type(e).__name__}: {e}", flush=True)
            done[opening_name] = game
            write_condition_games(condition_path, done)  # flush after every game -- same discipline as m4's collector

        condition_status[condition] = "COMPLETE" if len(done) == len(GARDNER_OPENING_BOOK) else "PARTIAL"

    verdict = "COMPLETE" if all(v == "COMPLETE" for v in condition_status.values()) else "PARTIAL"
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "game": "gardner_minichess_blindfold_fixed_eval_suite",
        "workers": args.workers,
        "openings": list(GARDNER_OPENING_BOOK),
        "max_plies": MAX_PLIES,
        "engine_depth": args.engine_depth,
        "condition_status": condition_status,
        "demo_dir": str(DEMO_DIR.relative_to(PROJECT_DIR)),
        "verdict": verdict,
        "note": ("Compares the minichess track's 3 coordination checkpoints (m3 random-routing / "
                 "m5 SFT-routed / m6 CMA-ES-routed) over the SAME 4 fixed openings from "
                 "scripts/m2_gardner_floor_check.py's GARDNER_OPENING_BOOK, one game per opening per "
                 "condition (12 games total) -- a fixed comparison suite over already-trained "
                 "checkpoints, not a training loop, hence far fewer games than m4/m6's GPU spend. "
                 "Full move-by-move data (routing choice, worker move, legality, ACPL, outcome) is in "
                 "reports/minichess_demo/<condition>.json, tracked (not gitignored) per PLAN.md's own "
                 "instruction so m8's interactive HTML demo can be built from it directly, without GPU-"
                 "host access. condition_status['m5_sft'/'m6_cmaes'] reads 'missing_checkpoint' rather "
                 "than crashing if run before those phases finish -- shouldn't happen given "
                 "advance_track's first-non-done-phase ordering, but this script can also be run "
                 "standalone. IMPORTANT CAVEAT carried over from every phase on this track: m2's floor "
                 "check (reports/m2_gardner_floor_check_summary.json) is REVIEW_NEEDED with a 0% "
                 "legal-move rate for all 3 default workers on this board size -- expect most games "
                 "here to terminate on an early illegal move rather than reaching real endgame play; "
                 "that is itself the honest result this suite is meant to surface for m8's demo, not a "
                 "bug in this script."),
    }
    reports_dir = PROJECT_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.chmod(0o700)
    REPORT_PATH.write_text(json.dumps(summary, indent=2))
    REPORT_PATH.chmod(0o600)
    print(f"\nVerdict: {verdict} -- {condition_status}")


if __name__ == "__main__":
    main()

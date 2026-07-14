#!/usr/bin/env python3
"""M2 -- Gardner Minichess (5x5) floor check, the minichess track's equivalent
of Phase 0.5: before spending any GPU-hours on this track's SFT data
collection (m4), verify the existing worker pool can produce mostly LEGAL
blindfold moves on the 5x5 board above a random floor. Same rationale and
report shape as scripts/phase0_5_blindfold_floor_check.py, swapped onto
GardnerBoard/GardnerScorer via harness.py's board_factory= param instead of
python-chess + vanilla Stockfish.

Uses a small hand-verified opening book (GARDNER_OPENING_BOOK below) instead
of a single fixed opening -- one game per book entry, alternating LLM color,
so the floor check isn't accidentally measuring one specific position's
quirks. Every line was verified against pyffish.legal_moves() move-by-move
before being hardcoded here (see the book's docstring), the same way M1's
hand-constructed positions were verified -- see PLAN.md's "5x5 fast-validation
track" addendum, M2 row.

Idempotent-ish: writes one JSON report per worker under RESULTS_DIR, skips
workers that already have a report unless --force is passed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import torch  # noqa: E402
from disk_guard import check_disk_budget  # noqa: E402
from open_fugu.chess_blindfold.harness import play_blindfold_vs_engine  # noqa: E402
from open_fugu.minichess.board import (  # noqa: E402
    BLACK, GARDNER_VARIANT_DESCRIPTION, WHITE, GardnerBoard,
)
from open_fugu.minichess.engine import GardnerScorer  # noqa: E402
from open_fugu.models.local_worker import (  # noqa: E402
    CANDIDATE_WORKERS, LocalWorker, LocalWorkerConfig, scaled_max_new_tokens,
)

RESULTS_DIR = PROJECT_DIR / "logs" / "m2_gardner_floor_check"

# Small hand-verified Gardner opening book -- each line checked ply-by-ply
# against pyffish.legal_moves() (start_fen -> legal_moves -> get_fen -> repeat)
# before being hardcoded here, exactly per PLAN.md's "don't guess moves"
# instruction for this milestone. Deliberately short (4 plies, like M1's own
# OPENING_PLIES) and non-terminal (verified: is_game_over() False at the end
# of every line) -- these are fixed starting points for blindfold play, not a
# claim about objectively strong Gardner theory (the board is tiny enough
# that any developing move puts a piece in immediate pawn-capture range on
# rank 3, unlike full chess -- see the queenside/kingside pairs below trading
# a pawn or two off as a result).
GARDNER_OPENING_BOOK = {
    "pawn_knight_skirmish_c": ["c2c3", "b4c3", "b2c3", "b5c3"],
    "knight_pawn_flank_b": ["b1a3", "b4b3", "a2b3", "e4e3"],
    "pawn_queen_skirmish_d": ["d2d3", "e4d3", "e2d3", "d5e4"],
    "pawn_bishop_skirmish_e": ["e2e3", "d4e3", "d2e3", "c5e3"],
}

MAX_PLIES = 24              # short games are enough to gauge the legal-move floor, same as Phase 0.5
ENGINE_FLOOR_DEPTH = 8      # shallow/weak search depth -- Fairy-Stockfish has no confirmed "Skill
                             # Level" UCI option on this binary (unlike vanilla Stockfish, which
                             # Phase 0.5 weakens via skill_level=1), so depth is the lever here instead


def run_floor_check_for_worker(short_id: str, model_id: str, max_plies: int) -> dict:
    print(f"\n=== M2 floor check: {short_id} ({model_id}) ===", flush=True)
    check_disk_budget()  # models here are pre-cached, but be defensive anyway

    # This script predates the 2026-07-12 generation-budget fix (Phase 5's
    # root-cause writeup) and was never re-run after it landed -- the flat
    # 200-token budget below starved deepseek-r1-distill-qwen-7b's <think>
    # block the exact same way Phase 0.5 was starved before that fix took it
    # from 0%/44%/22% to 61%/64%/25%. Matching that fix here.
    gen_budget = scaled_max_new_tokens(short_id, 200)
    worker = LocalWorker(LocalWorkerConfig(model_id=model_id, max_new_tokens=gen_budget, temperature=0.4))
    games = []

    for g, (opening_name, opening_moves) in enumerate(GARDNER_OPENING_BOOK.items()):
        llm_color = WHITE if g % 2 == 0 else BLACK
        scorer = GardnerScorer(depth=ENGINE_FLOOR_DEPTH)

        def move_fn(messages):
            return worker.generate(messages, max_new_tokens=gen_budget)

        def engine_move_fn(board):
            return scorer.best_move(board)

        t0 = time.time()
        try:
            result = play_blindfold_vs_engine(
                move_fn=move_fn,
                llm_color=llm_color,
                opening_uci_moves=opening_moves,
                engine_best_move_fn=engine_move_fn,
                scorer=scorer,
                max_plies=max_plies,
                board_factory=GardnerBoard,
                variant_description=GARDNER_VARIANT_DESCRIPTION,
            )
        except Exception as e:
            # A single game/model quirk shouldn't take down the whole floor
            # check run -- record it and move on to the next game/worker,
            # same as Phase 0.5.
            elapsed = time.time() - t0
            scorer.close()
            games.append({
                "game_idx": g,
                "opening": opening_name,
                "llm_color": "white" if llm_color == WHITE else "black",
                "result": "*",
                "termination": f"error: {type(e).__name__}: {e}",
                "n_plies": 0,
                "legal_move_rate": None,
                "mean_acpl": None,
                "blunder_rate": None,
                "elapsed_sec": elapsed,
                "final_fen": None,
            })
            print(f"  game {g} ({opening_name}): CRASHED ({type(e).__name__}: {e}), elapsed={elapsed:.1f}s",
                  flush=True)
            continue
        elapsed = time.time() - t0
        scorer.close()

        legal_plies = [p for p in result.plies if p.legal]
        losses = [p.centipawn_loss for p in legal_plies if p.centipawn_loss is not None]
        games.append({
            "game_idx": g,
            "opening": opening_name,
            "llm_color": "white" if llm_color == WHITE else "black",
            "result": result.result,
            "termination": result.termination,
            "n_plies": len(result.plies),
            "legal_move_rate": (len(legal_plies) / len(result.plies)) if result.plies else None,
            "mean_acpl": (sum(losses) / len(losses)) if losses else None,
            "blunder_rate": (sum(p.is_blunder for p in legal_plies) / len(legal_plies)) if legal_plies else None,
            "elapsed_sec": elapsed,
            "final_fen": result.final_fen,
            "raw_replies": [p.raw_reply[:300] for p in result.plies],
        })
        print(f"  game {g} ({opening_name}): {result.result} ({result.termination}), "
              f"legal_rate={games[-1]['legal_move_rate']}, "
              f"acpl={games[-1]['mean_acpl']}, elapsed={elapsed:.1f}s", flush=True)

    worker.unload()

    all_legal_rates = [g["legal_move_rate"] for g in games if g["legal_move_rate"] is not None]
    report = {
        "worker": short_id,
        "model_id": model_id,
        "n_games": len(GARDNER_OPENING_BOOK),
        "max_plies": max_plies,
        "games": games,
        "summary": {
            "mean_legal_move_rate": sum(all_legal_rates) / len(all_legal_rates) if all_legal_rates else None,
            "any_illegal_termination": any(g["termination"] == "illegal_move" for g in games),
        },
    }
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", nargs="*", default=["qwen2.5-7b", "mistral-7b", "deepseek-r1-distill-qwen-7b"],
                     help="Short ids from CANDIDATE_WORKERS to test -- same default pool Phase 0.5 used")
    ap.add_argument("--max-plies", type=int, default=MAX_PLIES)
    ap.add_argument("--force", action="store_true", help="Re-run even if a report already exists")
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.chmod(0o700)

    for short_id in args.workers:
        if short_id not in CANDIDATE_WORKERS:
            print(f"Unknown worker id '{short_id}', skipping. Known: {list(CANDIDATE_WORKERS)}")
            continue
        out_path = RESULTS_DIR / f"{short_id}.json"
        if out_path.exists() and not args.force:
            print(f"Skipping {short_id} (report already exists at {out_path}, use --force to redo)")
            continue

        report = run_floor_check_for_worker(short_id, CANDIDATE_WORKERS[short_id], args.max_plies)
        out_path.write_text(json.dumps(report, indent=2))
        out_path.chmod(0o600)
        torch.cuda.empty_cache()
        print(f"Wrote {out_path}")

    print("\n=== M2 floor check complete. Summary ===")
    for short_id in args.workers:
        out_path = RESULTS_DIR / f"{short_id}.json"
        if out_path.exists():
            r = json.loads(out_path.read_text())
            print(f"  {short_id}: mean_legal_move_rate={r['summary']['mean_legal_move_rate']}, "
                  f"any_illegal_termination={r['summary']['any_illegal_termination']}")


if __name__ == "__main__":
    main()

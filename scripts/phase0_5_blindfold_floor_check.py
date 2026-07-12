#!/usr/bin/env python3
"""Phase 0.5 -- Floor check (per the approved plan): before spending any real
data-collection GPU-hours, verify open-source 7-8B workers can produce mostly
LEGAL blindfold chess moves above a random floor. If they can't, that's a
result to report, not a reason to silently push forward into Phase 3.

Runs a handful of short blindfold games per candidate worker against a
skill-limited local Stockfish, and reports: legal-move rate, ACPL, blunder
rate, and game outcomes. Idempotent-ish: writes one JSON report per worker,
skips workers that already have a report unless --force is passed.
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

import chess  # noqa: E402
import torch  # noqa: E402
from disk_guard import check_disk_budget  # noqa: E402
from open_fugu.chess_blindfold.harness import play_blindfold_vs_engine  # noqa: E402
from open_fugu.models.local_worker import (  # noqa: E402
    CANDIDATE_WORKERS, LocalWorker, LocalWorkerConfig, scaled_max_new_tokens,
)
from open_fugu.reward.stockfish_scorer import StockfishScorer  # noqa: E402

RESULTS_DIR = PROJECT_DIR / "logs" / "phase0_5_floor_check"

# A short, standard, symmetric opening -- 6 plies (3 full moves) -- so both
# sides start from a well-known, roughly balanced position before blindfold
# play begins, matching the paper's "fixed opening given to both models" setup.
DEFAULT_OPENING = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"]  # Ruy Lopez

GAMES_PER_WORKER = 3
MAX_PLIES = 24          # short games are enough to gauge legal-move floor
STOCKFISH_FLOOR_SKILL = 1  # very weak opponent -- this is a floor check, not a strength test


def run_floor_check_for_worker(short_id: str, model_id: str, n_games: int, max_plies: int) -> dict:
    print(f"\n=== Floor check: {short_id} ({model_id}) ===", flush=True)
    check_disk_budget()  # models here are pre-cached, but be defensive anyway

    gen_budget = scaled_max_new_tokens(short_id, 200)
    worker = LocalWorker(LocalWorkerConfig(model_id=model_id, max_new_tokens=gen_budget, temperature=0.4))
    games = []

    for g in range(n_games):
        llm_color = chess.WHITE if g % 2 == 0 else chess.BLACK
        scorer = StockfishScorer(skill_level=STOCKFISH_FLOOR_SKILL, depth=8)

        def move_fn(messages):
            return worker.generate(messages, max_new_tokens=gen_budget)

        def engine_move_fn(board):
            return scorer.best_move(board)

        t0 = time.time()
        try:
            result = play_blindfold_vs_engine(
                move_fn=move_fn,
                llm_color=llm_color,
                opening_uci_moves=DEFAULT_OPENING,
                engine_best_move_fn=engine_move_fn,
                scorer=scorer,
                max_plies=max_plies,
            )
        except Exception as e:
            # A single game/model quirk (e.g. a chat-template edge case)
            # shouldn't take down the whole floor check run -- record it and
            # move on to the next game/worker.
            elapsed = time.time() - t0
            scorer.close()
            games.append({
                "game_idx": g,
                "llm_color": "white" if llm_color == chess.WHITE else "black",
                "result": "*",
                "termination": f"error: {type(e).__name__}: {e}",
                "n_plies": 0,
                "legal_move_rate": None,
                "mean_acpl": None,
                "blunder_rate": None,
                "elapsed_sec": elapsed,
                "final_fen": None,
            })
            print(f"  game {g}: CRASHED ({type(e).__name__}: {e}), elapsed={elapsed:.1f}s", flush=True)
            continue
        elapsed = time.time() - t0
        scorer.close()

        legal_plies = [p for p in result.plies if p.legal]
        losses = [p.centipawn_loss for p in legal_plies if p.centipawn_loss is not None]
        games.append({
            "game_idx": g,
            "llm_color": "white" if llm_color == chess.WHITE else "black",
            "result": result.result,
            "termination": result.termination,
            "n_plies": len(result.plies),
            "legal_move_rate": (len(legal_plies) / len(result.plies)) if result.plies else None,
            "mean_acpl": (sum(losses) / len(losses)) if losses else None,
            "blunder_rate": (sum(p.is_blunder for p in legal_plies) / len(legal_plies)) if legal_plies else None,
            "elapsed_sec": elapsed,
            "final_fen": result.final_fen,
            # Truncated raw replies -- essential for debugging why a move was
            # judged illegal (unparseable text vs. a genuinely illegal move).
            "raw_replies": [p.raw_reply[:300] for p in result.plies],
        })
        print(f"  game {g}: {result.result} ({result.termination}), "
              f"legal_rate={games[-1]['legal_move_rate']}, "
              f"acpl={games[-1]['mean_acpl']}, elapsed={elapsed:.1f}s", flush=True)

    worker.unload()

    all_legal_rates = [g["legal_move_rate"] for g in games if g["legal_move_rate"] is not None]
    report = {
        "worker": short_id,
        "model_id": model_id,
        "n_games": n_games,
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
                     help="Short ids from CANDIDATE_WORKERS to test")
    ap.add_argument("--n-games", type=int, default=GAMES_PER_WORKER)
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

        report = run_floor_check_for_worker(short_id, CANDIDATE_WORKERS[short_id], args.n_games, args.max_plies)
        out_path.write_text(json.dumps(report, indent=2))
        out_path.chmod(0o600)
        torch.cuda.empty_cache()
        print(f"Wrote {out_path}")

    print("\n=== Floor check complete. Summary ===")
    for short_id in args.workers:
        out_path = RESULTS_DIR / f"{short_id}.json"
        if out_path.exists():
            r = json.loads(out_path.read_text())
            print(f"  {short_id}: mean_legal_move_rate={r['summary']['mean_legal_move_rate']}, "
                  f"any_illegal_termination={r['summary']['any_illegal_termination']}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""M1 -- Gardner Minichess (5x5) board/scorer sanity check, the minichess
track's equivalent of Phase 2's Stockfish reward-pipeline sanity check.
Verifies GardnerBoard (pyffish-backed) and GardnerScorer (Fairy-Stockfish
UCI driver) against hand-constructed positions with an objectively known
correct answer, before this track's floor check / SFT data collection spend
any real GPU-hours trusting them. See PLAN.md's "5x5 fast-validation track"
addendum.

Idempotent: skips (no-op) if a report already exists, unless --force.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from open_fugu.minichess.board import GardnerBoard  # noqa: E402
from open_fugu.minichess.engine import GardnerScorer  # noqa: E402

REPORT_PATH = PROJECT_DIR / "reports" / "m1_gardner_engine_verify_result.json"

# Kings + queens only, minimal and unambiguous regardless of engine
# strength/depth -- White queen c1 can either capture Black's undefended
# queen on c4 outright (objectively best) or shuffle to c3, where Black's
# queen recaptures for free next move (an objectively hanging blunder).
# Verified programmatically against pyffish before hardcoding here (legal
# move sets, and that c4c3 recaptures for free after c1c3).
QUEEN_HANG_FEN = "4k/2q2/5/5/K1Q2 w - - 0 1"
QUEEN_HANG_BEST_MOVE = "c1c4"     # Qxc4 -- captures the undefended black queen
QUEEN_HANG_BLUNDER_MOVE = "c1c3"  # walks adjacent to ...Qxc3, loses the queen for nothing
QUEEN_HANG_ILLEGAL_MOVE = "c1d3"  # not a straight/diagonal queen move

# Corner mate: Black king boxed into a5 by a lone queen supported by the
# White king -- Qa1-a4 is forced mate (a4 defended by the White king on b3,
# b5/b4 both covered by the queen). Verified programmatically: after a1a4,
# Black has zero legal moves and is in check.
MATE_FEN = "k4/5/1K3/5/Q4 w - - 0 1"
MATE_MOVE = "a1a4"

# Gardner's own opening theory is thin (5x5, very few playable first moves --
# see M2's floor check for the actual opening book choice); reuse the
# from-scratch position instead so this check doesn't depend on that
# decision. Plays a short, hand-verified-legal skirmish on c3 (pawn push,
# knight captures pawn, pawn recaptures knight, knight recaptures pawn) and
# requires every move stay legal with a bounded mean centipawn loss.
OPENING_PLIES = ["c2c3", "b4c3", "b2c3", "b5c3"]
OPENING_MAX_MEAN_ACPL = 150  # generous -- not opening theory, just "not obviously terrible";
                              # trading a knight for two pawns costs real centipawns by design


def check_best_move(scorer: GardnerScorer) -> dict:
    board = GardnerBoard(QUEEN_HANG_FEN)
    ms = scorer.score_move(board, QUEEN_HANG_BEST_MOVE)
    # Same rationale as Phase 2's equivalent check: two independent
    # depth-limited searches (before/after) can disagree by a small amount
    # even for an objectively free capture -- what matters is staying an
    # order of magnitude below a real blunder (>300cp).
    passed = ms.is_legal and not ms.is_mistake and ms.centipawn_loss <= 50
    return {
        "name": "queen_hang_best_move", "move": QUEEN_HANG_BEST_MOVE, "passed": passed,
        "is_legal": ms.is_legal, "centipawn_loss": ms.centipawn_loss,
    }


def check_blunder_move(scorer: GardnerScorer) -> dict:
    board = GardnerBoard(QUEEN_HANG_FEN)
    ms = scorer.score_move(board, QUEEN_HANG_BLUNDER_MOVE)
    passed = ms.is_legal and ms.is_blunder and ms.centipawn_loss > GardnerScorer.BLUNDER_THRESHOLD_CP
    return {
        "name": "queen_hang_blunder_move", "move": QUEEN_HANG_BLUNDER_MOVE, "passed": passed,
        "is_legal": ms.is_legal, "is_blunder": ms.is_blunder, "centipawn_loss": ms.centipawn_loss,
    }


def check_illegal_move(scorer: GardnerScorer) -> dict:
    board = GardnerBoard(QUEEN_HANG_FEN)
    ms = scorer.score_move(board, QUEEN_HANG_ILLEGAL_MOVE)
    passed = (not ms.is_legal) and ms.is_blunder
    return {
        "name": "queen_hang_illegal_move", "move": QUEEN_HANG_ILLEGAL_MOVE, "passed": passed,
        "is_legal": ms.is_legal,
    }


def check_forced_mate(scorer: GardnerScorer) -> dict:
    board = GardnerBoard(MATE_FEN)
    ms = scorer.score_move(board, MATE_MOVE)
    passed = ms.is_legal and not ms.is_blunder and ms.centipawn_loss == 0
    return {
        "name": "corner_mate_forced_move", "move": MATE_MOVE, "passed": passed,
        "is_legal": ms.is_legal, "centipawn_loss": ms.centipawn_loss,
        "played_cp": ms.played_cp, "best_cp": ms.best_cp,
    }


def check_opening_replay(scorer: GardnerScorer) -> dict:
    board = GardnerBoard()
    losses = []
    all_legal = True
    for mv in OPENING_PLIES:
        ms = scorer.score_move(board, mv)
        all_legal = all_legal and ms.is_legal
        if ms.centipawn_loss is not None:
            losses.append(ms.centipawn_loss)
        board.push_uci(mv)
    mean_acpl = (sum(losses) / len(losses)) if losses else None
    passed = all_legal and mean_acpl is not None and mean_acpl <= OPENING_MAX_MEAN_ACPL
    return {
        "name": "opening_replay", "moves": OPENING_PLIES, "passed": passed,
        "all_legal": all_legal, "mean_acpl": mean_acpl,
    }


def check_board_wrapper_basics() -> dict:
    """No engine needed -- exercises GardnerBoard's chess.Board-duck-typed
    surface directly (the exact methods harness.py calls)."""
    board = GardnerBoard()
    checks = {
        "start_fen_matches_pyffish": board.fen() == "rnbqk/ppppp/5/PPPPP/RNBQK w - - 0 1",
        "turn_is_white_at_start": board.turn is True,
        "legal_moves_nonempty": len(board.legal_moves) > 0 and hasattr(board.legal_moves[0], "uci"),
    }
    board.push_uci("c2c3")
    checks["push_uci_flips_turn"] = board.turn is False
    checks["move_stack_tracks_history"] = board.move_stack == ["c2c3"]
    checks["is_game_over_false_after_one_move"] = not board.is_game_over()
    mate_board = GardnerBoard(MATE_FEN)
    mate_board.push_uci(MATE_MOVE)
    checks["is_checkmate_true_after_forced_mate"] = mate_board.is_checkmate()
    checks["is_game_over_true_after_forced_mate"] = mate_board.is_game_over()
    passed = all(checks.values())
    return {"name": "board_wrapper_basics", "passed": passed, "checks": checks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Re-run even if a report already exists")
    ap.add_argument("--depth", type=int, default=14, help="Fairy-Stockfish analysis depth for these checks")
    args = ap.parse_args()

    if REPORT_PATH.exists() and not args.force:
        print(f"Skipping (report already exists at {REPORT_PATH}, use --force to redo)")
        return

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.parent.chmod(0o700)

    checks = [check_board_wrapper_basics()]
    engine_error = None
    try:
        with GardnerScorer(depth=args.depth) as scorer:
            checks.append(check_best_move(scorer))
            checks.append(check_blunder_move(scorer))
            checks.append(check_illegal_move(scorer))
            checks.append(check_forced_mate(scorer))
            checks.append(check_opening_replay(scorer))
    except Exception as e:
        engine_error = f"{type(e).__name__}: {e}"

    passed = engine_error is None and bool(checks) and all(c["passed"] for c in checks)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine_error": engine_error,
        "checks": checks,
        "passed": passed,
        "note": ("Sanity-checks GardnerBoard + GardnerScorer against hand-verified 5x5 "
                 "positions (a free-queen best move, a hang-the-queen blunder, an illegal "
                 "move, a forced corner-mate move) plus a short opening replay -- gates "
                 "whether the minichess reward pipeline is trustworthy enough for M2's "
                 "floor check and beyond."),
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    REPORT_PATH.chmod(0o600)
    print(json.dumps(report, indent=2))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()

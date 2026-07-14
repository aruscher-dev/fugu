"""Blindfold-position sourcing for m4's SFT data collection on 5x5 Gardner
Minichess -- the minichess track's twin of open_fugu.data.chess_positions,
same reasoning as board.py/engine.py being hand-rolled twins of chess.Board/
StockfishScorer rather than the full-chess classes themselves (GardnerScorer
has no python-chess SimpleEngine underneath it to call `.configure({"Skill
Level": ...})` on -- Fairy-Stockfish has no confirmed Skill Level UCI option
on this binary, per m2_gardner_floor_check.py's own note, so depth is the
diversity/weakening lever here instead of skill level).

Not a generalization of chess_positions.generate_positions() behind a shared
board_factory/engine_factory parameter -- unlike harness.py (where the
protocol logic was 100% shared and only the board/scorer objects differed),
StockfishScorer's Skill-Level config call and GardnerScorer's hand-rolled UCI
depth knob are different enough interfaces that forcing them through one
function would just relocate the branching, not remove it. A parallel
twin function, reusing the shared SampledPosition dataclass, keeps Phase 3's
already-audited code path completely untouched.

Positions are move-history prefixes reached via self-play against
GardnerScorer itself (never an LLM), same rationale as the full-chess
version: keeps this phase's cost/runtime independent of worker quality.
"""
from __future__ import annotations

import random
from typing import List

from open_fugu.data.chess_positions import SampledPosition
from open_fugu.minichess.board import GardnerBoard
from open_fugu.minichess.engine import GardnerScorer

# Fairy-Stockfish has no Skill Level UCI option on this binary (m2's own
# note) -- depth is the diversity/weakening lever instead, spanning
# "plays close to randomly" to "plays fairly strong" on a board this small.
DEPTH_LEVELS = [1, 2, 4, 6, 9, 13]
RANDOM_MOVE_PROB = 0.15  # per ply, overrides the engine move with a uniformly random legal one -- same as chess_positions.py
MIN_PLY = 4    # a handful of moves in, so "the position" is more than the bare start position -- same as chess_positions.py
MAX_PLY = 24   # Gardner games resolve much faster than full chess on a 5x5 board with only 5 pawns/back-rank pieces per side


def _play_one_self_play_game(depth: int, target_ply: int, rng: random.Random) -> tuple[List[str], bool]:
    """Returns (moves, is_terminal) -- is_terminal is True when the position
    reached after `moves` has no legal moves left (checkmate/stalemate). The
    loop only checks is_game_over() *before* generating each move, so a
    mating move played on the final iteration still gets appended -- callers
    must check is_terminal themselves rather than assume every returned
    position has a next move to query a worker about. Mirrors
    chess_positions._play_one_self_play_game() exactly, minus the
    chess.Board/StockfishScorer-specific pieces."""
    board = GardnerBoard()
    moves: List[str] = []
    with GardnerScorer(depth=depth) as engine:
        for _ in range(target_ply):
            if board.is_game_over():
                break
            if rng.random() < RANDOM_MOVE_PROB:
                mv = rng.choice(board.legal_moves_uci())
            else:
                mv = engine.best_move(board)
            board.push_uci(mv)
            moves.append(mv)
    return moves, board.is_game_over()


def generate_gardner_positions(n_positions: int, seed: int = 42) -> List[SampledPosition]:
    """Deterministic given `seed` -- mirrors
    chess_positions.generate_positions()'s own docstring/contract exactly
    (regenerating reproduces the same set, though the persisted positions
    file, once written, is the actual source of truth for a collection run).

    Unlike the full-chess version, this spawns one GardnerScorer subprocess
    per attempted game (each instance's `depth` is fixed at construction,
    unlike StockfishScorer's reconfigurable `Skill Level`) -- acceptable
    overhead here since Gardner games are short and this only runs once to
    build the fixed positions.jsonl file, not once per (worker, sample).
    """
    rng = random.Random(seed)
    positions: List[SampledPosition] = []
    idx = 0
    attempts = 0
    max_attempts = n_positions * 5  # generous slack for games that end too early to keep, same as chess_positions.py
    while len(positions) < n_positions and attempts < max_attempts:
        attempts += 1
        depth = rng.choice(DEPTH_LEVELS)
        target_ply = rng.randint(MIN_PLY, MAX_PLY)
        moves, is_terminal = _play_one_self_play_game(depth, target_ply, rng)
        if len(moves) < MIN_PLY or is_terminal:
            # Either ended too early (fast mate/stalemate at shallow depth), or
            # the LAST move played was itself the mating/stalemating move --
            # a position with zero legal moves left has nothing for a
            # blindfold worker to answer; every worker would trivially score
            # "illegal" on it regardless of quality, adding pure label noise.
            continue
        positions.append(SampledPosition(position_idx=idx, opening_uci_moves=moves))
        idx += 1
    return positions

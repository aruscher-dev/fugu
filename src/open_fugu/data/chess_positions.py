"""Blindfold-position sourcing for Phase 3's SFT data collection.

PLAN.md's training recipe originally sourced (position, solution) pairs from
a Lichess puzzle CSV (`~/Team/chess_bench/data/puzzles.csv`), not available
on this host. But blindfold play (harness.py's whole point, matching the
Fugu paper's Appendix B.2 protocol) never shows the model a FEN or board --
only a move-history-from-the-start-position prompt -- so a "position" here
means a *move-history prefix reachable via legal play*, not an arbitrary
FEN. Prefixes come from self-play games against the project's own local
Stockfish (never an LLM -- keeps this phase's cost and runtime independent
of worker quality): each game is played at a skill level randomized per
game (Stockfish's own Skill Level setting deliberately weakens/randomizes
move choice) plus a small per-ply chance of an explicit uniformly random
legal move, so repeated games don't all collapse onto the same few forcing
lines.

Each resulting position is exactly the List[str] of UCI moves that
harness.py's format_opening_prompt()/play_blindfold_vs_engine() already
expect as `opening_uci_moves` -- src/open_fugu/data/collect_sft_data.py
reuses that exact prompt format for a single-move query instead of a whole
game.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List

import chess

from open_fugu.reward.stockfish_scorer import StockfishScorer

# A spread from "plays almost randomly" to "full strength" -- diversity plus
# a realistic range of position "quality" for the resulting SFT data to span.
SKILL_LEVELS = [1, 3, 5, 8, 12, 16, 20]
RANDOM_MOVE_PROB = 0.15  # per ply, overrides the engine move with a uniformly random legal one
MIN_PLY = 4    # a handful of moves in, so "the position" is more than the bare start position
MAX_PLY = 40


@dataclass
class SampledPosition:
    position_idx: int
    opening_uci_moves: List[str]

    @property
    def color_to_move(self) -> str:
        return "white" if len(self.opening_uci_moves) % 2 == 0 else "black"


def _play_one_self_play_game(engine: StockfishScorer, target_ply: int, rng: random.Random) -> List[str]:
    board = chess.Board()
    moves: List[str] = []
    for _ in range(target_ply):
        if board.is_game_over():
            break
        if rng.random() < RANDOM_MOVE_PROB:
            mv = rng.choice(list(board.legal_moves)).uci()
        else:
            mv = engine.best_move(board)
        board.push_uci(mv)
        moves.append(mv)
    return moves


def generate_positions(n_positions: int, seed: int = 42, depth: int = 10) -> List[SampledPosition]:
    """Deterministic given `seed`, so regenerating (e.g. before the positions
    file described in collect_sft_data.py exists yet) reproduces the same
    set -- though the persisted file, once written, is the actual source of
    truth for every worker's collection run.
    """
    rng = random.Random(seed)
    positions: List[SampledPosition] = []
    with StockfishScorer(depth=depth) as engine:
        idx = 0
        attempts = 0
        max_attempts = n_positions * 5  # generous slack for games that end too early to keep
        while len(positions) < n_positions and attempts < max_attempts:
            attempts += 1
            skill = rng.choice(SKILL_LEVELS)
            engine.engine.configure({"Skill Level": skill})
            target_ply = rng.randint(MIN_PLY, MAX_PLY)
            moves = _play_one_self_play_game(engine, target_ply, rng)
            if len(moves) < MIN_PLY:
                continue  # game ended too early (fast mate/stalemate at low skill) -- not usable
            positions.append(SampledPosition(position_idx=idx, opening_uci_moves=moves))
            idx += 1
    return positions

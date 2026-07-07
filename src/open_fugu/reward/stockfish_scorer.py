"""Stockfish-based move/game scoring, following the chess_bench engine pattern
(chess.engine.SimpleEngine.popen_uci + per-position analyse()).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import chess
import chess.engine

DEFAULT_STOCKFISH_PATH = os.environ.get(
    "STOCKFISH_PATH",
    str((__import__("pathlib").Path(__file__).resolve().parents[3] / "bin" / "stockfish-wrapper.sh")),
)


@dataclass
class MoveScore:
    played_move: str
    played_cp: Optional[int]     # centipawn eval after the played move, from mover's POV (None if mate)
    best_cp: Optional[int]       # centipawn eval after the best move, from mover's POV
    centipawn_loss: int          # max(0, best_cp - played_cp), clamped, mate handled as large loss
    is_legal: bool
    is_blunder: bool             # loss > 300cp (per llm_chess's own conventions ballpark)
    is_mistake: bool             # loss > 100cp


class StockfishScorer:
    """Wraps a single persistent Stockfish process for repeated position analysis.

    Not thread-safe -- create one instance per worker process/thread.
    """

    BLUNDER_THRESHOLD_CP = 300
    MISTAKE_THRESHOLD_CP = 100
    MATE_SCORE_CP = 10000  # sentinel for "mate" so centipawn-loss arithmetic stays well-defined

    def __init__(self, stockfish_path: str = DEFAULT_STOCKFISH_PATH, depth: int = 12,
                 threads: int = 1, skill_level: Optional[int] = None):
        self.engine = chess.engine.SimpleEngine.popen_uci(stockfish_path)
        self.engine.configure({"Threads": threads})
        if skill_level is not None:
            self.engine.configure({"Skill Level": skill_level})
        self.depth = depth

    def _score_cp(self, score: chess.engine.PovScore, pov_color: chess.Color) -> int:
        pov = score.pov(pov_color)
        if pov.is_mate():
            mate_in = pov.mate()
            # Closer mates are "better" than distant ones; keep monotonic and bounded.
            sign = 1 if mate_in > 0 else -1
            return sign * (self.MATE_SCORE_CP - abs(mate_in))
        return pov.score()

    def score_move(self, board: chess.Board, move_uci: str) -> MoveScore:
        """Score a candidate move against Stockfish's own best move, both evaluated
        from the perspective of the side to move on `board` (i.e. the mover)."""
        mover = board.turn
        try:
            move = chess.Move.from_uci(move_uci)
        except Exception:
            return MoveScore(move_uci, None, None, self.MATE_SCORE_CP, False, True, True)

        if move not in board.legal_moves:
            return MoveScore(move_uci, None, None, self.MATE_SCORE_CP, False, True, True)

        best_info = self.engine.analyse(board, chess.engine.Limit(depth=self.depth))
        best_cp = self._score_cp(best_info["score"], mover)

        board_after = board.copy()
        board_after.push(move)
        if board_after.is_game_over():
            played_cp = self.MATE_SCORE_CP if board_after.is_checkmate() else 0
        else:
            played_info = self.engine.analyse(board_after, chess.engine.Limit(depth=self.depth))
            # analyse() on board_after evaluates from the perspective of the side now to move
            # (the opponent), so flip sign to get the mover's perspective.
            played_cp = -self._score_cp(played_info["score"], not mover)

        loss = max(0, best_cp - played_cp)
        return MoveScore(
            played_move=move_uci,
            played_cp=played_cp,
            best_cp=best_cp,
            centipawn_loss=loss,
            is_legal=True,
            is_blunder=loss > self.BLUNDER_THRESHOLD_CP,
            is_mistake=loss > self.MISTAKE_THRESHOLD_CP,
        )

    def best_move(self, board: chess.Board) -> str:
        result = self.engine.play(board, chess.engine.Limit(depth=self.depth))
        return result.move.uci()

    def close(self):
        self.engine.quit()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

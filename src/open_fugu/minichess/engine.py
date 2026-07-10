"""Gardner Minichess move/game scoring via a vendored Fairy-Stockfish binary,
mirroring open_fugu.reward.stockfish_scorer.StockfishScorer's MoveScore shape
so downstream code (harness.py's scorer= argument, reports) doesn't need to
special-case the variant.

python-chess's chess.engine.SimpleEngine assumes a chess.Board (calls
.fen()/.chess960 in ways that don't generalize to arbitrary UCI variants), so
this drives the UCI subprocess directly instead -- same protocol, hand-rolled
send/read loop (verified manually against this exact binary before writing
this module; see reports/m0_gardner_engine_verify.json).

IMPORTANT: a piped one-shot `printf 'uci\\n...\\ngo depth N\\n' | engine` will
return near-instantly with a garbage bestmove -- closing stdin (EOF) right
after `go` is treated by this engine as an implicit stop. Must keep the
subprocess's stdin open across the whole game, as this module does.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pyffish

from open_fugu.minichess.board import VARIANT, GardnerBoard

DEFAULT_ENGINE_PATH = os.environ.get(
    "FAIRY_STOCKFISH_PATH",
    str(Path(__file__).resolve().parents[3] / "bin" / "fairy-stockfish"),
)


@dataclass
class MoveScore:
    played_move: str
    played_cp: Optional[int]
    best_cp: Optional[int]
    centipawn_loss: int
    is_legal: bool
    is_blunder: bool
    is_mistake: bool


class GardnerScorer:
    """Wraps a single persistent Fairy-Stockfish process, UCI_Variant=gardner.

    Not thread-safe -- create one instance per worker process/thread, same
    convention as StockfishScorer.
    """

    BLUNDER_THRESHOLD_CP = 300
    MISTAKE_THRESHOLD_CP = 100
    MATE_SCORE_CP = 10000

    def __init__(self, engine_path: str = DEFAULT_ENGINE_PATH, depth: int = 14,
                 threads: int = 1):
        self.proc = subprocess.Popen(
            [engine_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self.depth = depth
        self._send("uci")
        self._read_until("uciok")
        self._send(f"setoption name UCI_Variant value {VARIANT}")
        self._send(f"setoption name Threads value {threads}")
        self._send("isready")
        self._read_until("readyok")

    def _send(self, cmd: str) -> None:
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _read_until(self, token: str, timeout: float = 30.0) -> list:
        lines = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip())
            if token in line:
                break
        return lines

    def _analyse(self, fen: str) -> int:
        """Search `fen` to self.depth, return the eval in centipawns from the
        perspective of the side to move in that FEN (standard UCI convention)."""
        self._send("ucinewgame")
        self._send(f"position fen {fen}")
        self._send(f"go depth {self.depth}")
        lines = self._read_until("bestmove")
        best_cp = None
        for line in lines:
            if line.startswith("info depth") and " score " in line:
                tokens = line.split()
                idx = tokens.index("score")
                kind, val = tokens[idx + 1], int(tokens[idx + 2])
                if kind == "mate":
                    sign = 1 if val > 0 else -1
                    best_cp = sign * (self.MATE_SCORE_CP - abs(val))
                else:
                    best_cp = val
        if best_cp is None:
            raise RuntimeError(f"engine produced no score info for fen={fen!r}: {lines}")
        return best_cp

    def score_move(self, board: GardnerBoard, move_uci: str) -> MoveScore:
        """Score a candidate move against the engine's own best move, both
        evaluated from the mover's perspective -- mirrors
        StockfishScorer.score_move exactly, minus python-chess Move objects."""
        mover_is_white = board.turn

        if move_uci not in board.legal_moves_uci():
            return MoveScore(move_uci, None, None, self.MATE_SCORE_CP, False, True, True)

        best_cp = self._analyse(board.fen())

        fen_after = pyffish.get_fen(VARIANT, board.fen(), [move_uci])
        after_board = GardnerBoard(fen_after)
        if after_board.is_checkmate():
            played_cp = self.MATE_SCORE_CP
        elif after_board.is_game_over():
            played_cp = 0
        else:
            # _analyse() returns the eval from the perspective of the side to
            # move in fen_after (the opponent, since the mover just moved) --
            # flip sign to get the mover's perspective back, same as
            # StockfishScorer.score_move's `played_cp = -self._score_cp(...)`.
            played_cp = -self._analyse(fen_after)

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

    def best_move(self, board: GardnerBoard) -> str:
        self._send("ucinewgame")
        self._send(f"position fen {board.fen()}")
        self._send(f"go depth {self.depth}")
        lines = self._read_until("bestmove")
        for line in lines:
            if line.startswith("bestmove"):
                return line.split()[1]
        raise RuntimeError(f"engine produced no bestmove for fen={board.fen()!r}: {lines}")

    def close(self):
        try:
            self._send("quit")
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

"""GardnerBoard: a minimal chess.Board-duck-typed wrapper around pyffish's
Gardner Minichess (5x5) move generation, so open_fugu.chess_blindfold.harness
can drive minichess games with the same code path as full chess (see harness.py's
`board_factory` parameter).

Only implements the subset of chess.Board's API that harness.py actually calls:
.turn, .legal_moves, .push_uci(), .is_game_over(), .is_checkmate(),
.is_stalemate(), .is_insufficient_material(), .is_seventyfive_moves(),
.is_fivefold_repetition(), .move_stack, .fen(), .parse_san().
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import pyffish

VARIANT = "gardner"

WHITE = True
BLACK = False

# 2026-07-14 finding: a re-run of m2's floor check (after ruling out the
# generation-budget bug that fixed the full-chess track) showed 0% legal-move
# rate persisted, every game dying on the LLM's very first move. Raw replies
# (logs/m2_gardner_floor_check/*.json) showed the model confidently describing
# a full standard 8x8 starting position ("King on e1, Queen on d1, Rooks on a1
# and h1...") -- harness.py's prompt never says this is a 5x5 variant at all,
# so a 7B model defaults to its training-dominant assumption (standard chess)
# and every move it produces is fantasy for the real (tiny, differently-set-up)
# board. This description is meant to be passed as harness.py's
# `variant_description` param so the opening prompt states the real board
# up front. Facts below were verified against pyffish directly (start_fen +
# legal_moves from it), not assumed from general Gardner Minichess trivia --
# see the 2026-07-14 session notes for the exact check (no double-step pawn
# moves and no castling are both available from ply 1).
GARDNER_VARIANT_DESCRIPTION = (
    "You are playing Gardner Minichess, a chess variant on a SMALL 5x5 BOARD "
    "-- NOT standard 8x8 chess. Files run a-e only (no f/g/h) and ranks run "
    "1-5 only (no 6/7/8). Each side has exactly ONE rook, ONE knight, ONE "
    "bishop, ONE queen, and ONE king (no second rook/knight/bishop the way "
    "standard chess has), plus 5 pawns. Starting position: White's back rank "
    "(rank 1) is a1=rook, b1=knight, c1=bishop, d1=queen, e1=king, with White "
    "pawns on all of rank 2; Black's back rank (rank 5) is a5=rook, "
    "b5=knight, c5=bishop, d5=queen, e5=king, with Black pawns on all of rank "
    "4; rank 3 starts empty. There is no castling, and pawns only ever "
    "advance one square at a time (no two-square first move, so no en "
    "passant either)."
)


@dataclass(frozen=True)
class _UciMove:
    """Duck-types chess.Move's only attribute harness.py touches: .uci()."""
    _uci: str

    def uci(self) -> str:
        return self._uci


class GardnerBoard:
    """Tracks a running Gardner Minichess position via pyffish. Not
    thread-safe; one instance per game, like chess.Board.
    """

    def __init__(self, fen: Optional[str] = None):
        self.fen_str = fen or pyffish.start_fen(VARIANT)
        self.move_stack: List[str] = []

    @property
    def turn(self) -> bool:
        # FEN's 2nd field is side-to-move ("w"/"b") -- same schema as standard chess.
        return self.fen_str.split(" ")[1] == "w"

    @property
    def legal_moves(self) -> List[_UciMove]:
        return [_UciMove(m) for m in pyffish.legal_moves(VARIANT, self.fen_str, [])]

    def legal_moves_uci(self) -> List[str]:
        return pyffish.legal_moves(VARIANT, self.fen_str, [])

    def push_uci(self, move_uci: str) -> None:
        if move_uci not in self.legal_moves_uci():
            raise ValueError(f"illegal move {move_uci!r} in position {self.fen_str!r}")
        self.fen_str = pyffish.get_fen(VARIANT, self.fen_str, [move_uci])
        self.move_stack.append(move_uci)

    def gives_check(self) -> bool:
        return pyffish.gives_check(VARIANT, self.fen_str, [])

    def is_checkmate(self) -> bool:
        return len(self.legal_moves_uci()) == 0 and self.gives_check()

    def is_stalemate(self) -> bool:
        return len(self.legal_moves_uci()) == 0 and not self.gives_check()

    def is_insufficient_material(self) -> bool:
        white_insufficient, black_insufficient = pyffish.has_insufficient_material(
            VARIANT, self.fen_str, []
        )
        return white_insufficient and black_insufficient

    def is_seventyfive_moves(self) -> bool:
        # FEN's 5th field is the halfmove clock, same schema as standard chess.
        halfmove_clock = int(self.fen_str.split(" ")[4])
        return halfmove_clock >= 150

    def is_fivefold_repetition(self) -> bool:
        # Not tracked (would need full position history + repetition counting) --
        # acceptable simplification for a fast-validation demo track; games are
        # short and draws-by-repetition are not the signal this track cares about.
        return False

    def is_game_over(self) -> bool:
        return (
            len(self.legal_moves_uci()) == 0
            or self.is_insufficient_material()
            or self.is_seventyfive_moves()
        )

    def fen(self) -> str:
        return self.fen_str

    def parse_san(self, token: str) -> _UciMove:
        """Match a free-text token against each legal move's SAN, mirroring
        python-chess's parse_san() contract (raises ValueError if no match)."""
        for uci in self.legal_moves_uci():
            san = pyffish.get_san(VARIANT, self.fen_str, uci)
            if san == token or san.rstrip("+#") == token.rstrip("+#"):
                return _UciMove(uci)
        raise ValueError(f"illegal SAN move {token!r} in position {self.fen_str!r}")

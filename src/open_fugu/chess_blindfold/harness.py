"""Blindfold chess game harness, matching the Fugu paper's Appendix B.2 protocol
(Listing 1): a fixed opening is given once, then each side is told only the
opponent's last move in UCI -- no board, no FEN, no legal-move list is ever
shown. The model must track the whole position from memory across a single
continuous conversation.

This is deliberately independent of the vendored llm_chess/AutoGen machinery:
llm_chess's default action-DSL (get_current_board / get_legal_moves / make_move)
is NOT blindfold by default -- it exists precisely so the model *can* query
board state, which is the opposite of what Appendix B.2 tests. Re-using it would
mean fighting the framework rather than reproducing the paper's protocol.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import chess

MoveFn = Callable[[List[dict]], str]  # (messages) -> raw assistant text

UCI_RE = re.compile(r"\b([a-h][1-8][a-h][1-8][qrbn]?)\b", re.IGNORECASE)


def extract_uci_move(text: str, board: chess.Board) -> Optional[str]:
    """Pull the first UCI-looking, currently-legal token out of free text.

    Blindfold play gives models no format constraint beyond "reply with your
    move," and reasoning-style models may wrap the answer in prose/<think>
    tags -- scan for legal candidates rather than requiring an exact match.
    """
    legal_uci = {m.uci() for m in board.legal_moves}
    for match in UCI_RE.finditer(text):
        candidate = match.group(1).lower()
        if candidate in legal_uci:
            return candidate
    return None


def format_opening_prompt(color: str, opening_uci_moves: List[str]) -> str:
    """Reproduce Listing 1's setup prompt: numbered move pairs in UCI, ending
    with 'What is your move?'."""
    parts = []
    for i in range(0, len(opening_uci_moves), 2):
        move_no = i // 2 + 1
        white_mv = opening_uci_moves[i]
        black_mv = opening_uci_moves[i + 1] if i + 1 < len(opening_uci_moves) else ""
        parts.append(f"{move_no}. {white_mv} {black_mv}".strip())
    history_str = " ".join(parts)
    return (
        f"You are playing a chess game and you are playing with {color} pieces. "
        f"Current move history is {history_str}. What is your move?"
    )


@dataclass
class PlyRecord:
    ply: int
    mover: str          # "white" or "black"
    worker_id: Optional[str]
    move_uci: Optional[str]
    raw_reply: str
    legal: bool
    centipawn_loss: Optional[int] = None
    is_blunder: bool = False
    is_mistake: bool = False


@dataclass
class GameResult:
    result: str          # "1-0", "0-1", "1/2-1/2", or "aborted"
    termination: str     # "checkmate" | "illegal_move" | "max_plies" | "stalemate" | ...
    plies: List[PlyRecord] = field(default_factory=list)
    final_fen: str = ""


def play_blindfold_vs_engine(
    move_fn: MoveFn,
    llm_color: chess.Color,
    opening_uci_moves: List[str],
    engine_best_move_fn: Callable[[chess.Board], str],
    scorer=None,
    max_plies: int = 120,
    worker_id: Optional[str] = None,
) -> GameResult:
    """Play one blindfold game: the LLM (via move_fn) against an engine baseline
    (e.g. Stockfish at a fixed skill level) that DOES see the board normally --
    only the LLM side is blindfolded, matching the paper's Fugu-vs-Stockfish games.

    move_fn receives the full running message transcript (system-free, just
    user/assistant turns) and returns raw text; engine_best_move_fn receives the
    real chess.Board and returns its move in UCI (the engine is not blindfolded).
    """
    board = chess.Board()
    for mv in opening_uci_moves:
        board.push_uci(mv)

    color_name = "white" if llm_color == chess.WHITE else "black"
    messages: List[dict] = [
        {"role": "user", "content": format_opening_prompt(color_name, opening_uci_moves)}
    ]
    plies: List[PlyRecord] = []
    ply_no = len(opening_uci_moves)

    def llm_turn() -> PlyRecord:
        nonlocal ply_no
        ply_no += 1
        raw = move_fn(messages)
        uci = extract_uci_move(raw, board)
        mover = "white" if board.turn == chess.WHITE else "black"
        rec = PlyRecord(ply=ply_no, mover=mover, worker_id=worker_id,
                         move_uci=uci, raw_reply=raw, legal=uci is not None)
        if scorer is not None and uci is not None:
            ms = scorer.score_move(board, uci)
            rec.centipawn_loss = ms.centipawn_loss
            rec.is_blunder = ms.is_blunder
            rec.is_mistake = ms.is_mistake
        messages.append({"role": "assistant", "content": raw})
        if uci is not None:
            board.push_uci(uci)
        return rec

    def engine_turn() -> str:
        mv = engine_best_move_fn(board)
        board.push_uci(mv)
        messages.append({"role": "user", "content": mv})
        return mv

    # If it's the engine's turn right after the opening (LLM plays the other color),
    # let the engine move first so the LLM's setup prompt is immediately followed
    # by something to react to on its next turn.
    if board.turn != llm_color:
        engine_turn()

    while len(board.move_stack) < max_plies + len(opening_uci_moves) and not board.is_game_over():
        rec = llm_turn()
        plies.append(rec)
        if not rec.legal:
            return GameResult(
                result=("0-1" if llm_color == chess.WHITE else "1-0"),
                termination="illegal_move",
                plies=plies,
                final_fen=board.fen(),
            )
        if board.is_game_over():
            break
        engine_turn()

    if board.is_checkmate():
        result = "0-1" if board.turn == chess.WHITE else "1-0"
        termination = "checkmate"
    elif board.is_stalemate():
        result, termination = "1/2-1/2", "stalemate"
    elif board.is_insufficient_material() or board.is_seventyfive_moves() or board.is_fivefold_repetition():
        result, termination = "1/2-1/2", "draw"
    else:
        result, termination = "*", "max_plies"

    return GameResult(result=result, termination=termination, plies=plies, final_fen=board.fen())

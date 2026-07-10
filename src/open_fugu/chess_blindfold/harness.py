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
from typing import Any, Awaitable, Callable, List, Optional

import chess

BoardFactory = Callable[[], Any]  # defaults to chess.Board; pass GardnerBoard for 5x5

MoveFn = Callable[[List[dict]], str]  # (messages) -> raw assistant text
AsyncMoveFn = Callable[[List[dict]], Awaitable[str]]  # async variant, e.g. an A2A call

# Matches "e2e4"/"e2e4q" as well as the hyphenated "e2-e4" that models very
# commonly produce even when the prompt itself only ever shows concatenated
# UCI -- an earlier version of this regex required strict contiguity and
# silently treated every hyphenated reply as illegal.
UCI_RE = re.compile(r"\b([a-h][1-8])-?([a-h][1-8])([qrbn]?)\b", re.IGNORECASE)


def extract_uci_move(text: str, board: chess.Board) -> Optional[str]:
    """Pull the first UCI-looking, currently-legal token out of free text.

    Blindfold play gives models no format constraint beyond "reply with your
    move," and reasoning-style models may wrap the answer in prose/<think>
    tags -- scan for legal candidates rather than requiring an exact match.
    """
    legal_uci = {m.uci() for m in board.legal_moves}
    for match in UCI_RE.finditer(text):
        candidate = "".join(match.groups()).lower()
        if candidate in legal_uci:
            return candidate

    # Fall back to SAN (e.g. "d4", "Nf3", "exd5") -- models asked for UCI
    # still frequently answer in algebraic notation. Reuse python-chess's own
    # parser (handles disambiguation/checks/promotions) rather than
    # hand-rolling a second regex; try each punctuation-stripped token and
    # take the first that parses to a legal move.
    for token in re.split(r"\s+", text):
        token = token.strip(".,!?()[]{}:;\"'")
        if not token:
            continue
        try:
            move = board.parse_san(token)
        except ValueError:
            continue
        return move.uci()
    return None


# Repeated verbatim on every turn (opening + each opponent-move prompt) --
# without an explicit format constraint, instruct-tuned models default to
# prose ("Given the current move history, I'll play...") that either runs
# past a short max_new_tokens budget or renders the move as "e2-e4" instead
# of the bare UCI the opening history itself was written in.
MOVE_FORMAT_INSTRUCTION = (
    "Reply with ONLY your move in UCI notation (four or five lowercase "
    "letters/digits, e.g. e2e4 or e7e8q), with no other text."
)


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
        f"Current move history is {history_str}. What is your move? {MOVE_FORMAT_INSTRUCTION}"
    )


def format_opponent_move_prompt(mv: str) -> str:
    return f"Opponent played {mv}. What is your move? {MOVE_FORMAT_INSTRUCTION}"


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
    board_factory: BoardFactory = chess.Board,
) -> GameResult:
    """Play one blindfold game: the LLM (via move_fn) against an engine baseline
    (e.g. Stockfish at a fixed skill level) that DOES see the board normally --
    only the LLM side is blindfolded, matching the paper's Fugu-vs-Stockfish games.

    move_fn receives the full running message transcript (system-free, just
    user/assistant turns) and returns raw text; engine_best_move_fn receives the
    board (real chess.Board by default) and returns its move in UCI (the engine
    is not blindfolded). Pass board_factory=GardnerBoard (from
    open_fugu.minichess.board) for the 5x5 track -- everything else about the
    protocol is board-size-agnostic.
    """
    board = board_factory()
    for mv in opening_uci_moves:
        board.push_uci(mv)

    color_name = "white" if llm_color == chess.WHITE else "black"
    opening_prompt = format_opening_prompt(color_name, opening_uci_moves)
    plies: List[PlyRecord] = []
    ply_no = len(opening_uci_moves)

    # If the engine moves immediately after the fixed opening (LLM plays
    # black), fold that first engine move into the SAME initial user turn
    # instead of appending a second, separate user message right after it --
    # two consecutive "user" messages with no assistant turn between them
    # breaks strict chat templates' turn-alternation validation (crashes
    # Mistral's template outright, silently degrades Qwen's).
    if board.turn != llm_color:
        first_engine_move = engine_best_move_fn(board)
        board.push_uci(first_engine_move)
        opening_prompt += " " + format_opponent_move_prompt(first_engine_move)

    messages: List[dict] = [{"role": "user", "content": opening_prompt}]

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
        messages.append({"role": "user", "content": format_opponent_move_prompt(mv)})
        return mv

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


async def play_blindfold_vs_engine_async(
    async_move_fn: AsyncMoveFn,
    llm_color: chess.Color,
    opening_uci_moves: List[str],
    engine_best_move_fn: Callable[[chess.Board], str],
    scorer=None,
    max_plies: int = 120,
    worker_id: Optional[str] = None,
    board_factory: BoardFactory = chess.Board,
) -> GameResult:
    """Async twin of play_blindfold_vs_engine, for when the LLM side is
    reached over the network (e.g. an A2A call to a purple agent) rather than
    an in-process function call. Identical game logic -- see that function's
    docstring; kept as a separate function rather than a shared core with a
    sync/async flag, since threading `await` through the sync call sites would
    otherwise force every direct caller (e.g. the Phase 0.5 floor check) to
    become async too, for no benefit there.
    """
    board = board_factory()
    for mv in opening_uci_moves:
        board.push_uci(mv)

    color_name = "white" if llm_color == chess.WHITE else "black"
    opening_prompt = format_opening_prompt(color_name, opening_uci_moves)
    plies: List[PlyRecord] = []
    ply_no = len(opening_uci_moves)

    if board.turn != llm_color:
        first_engine_move = engine_best_move_fn(board)
        board.push_uci(first_engine_move)
        opening_prompt += " " + format_opponent_move_prompt(first_engine_move)

    messages: List[dict] = [{"role": "user", "content": opening_prompt}]

    async def llm_turn() -> PlyRecord:
        nonlocal ply_no
        ply_no += 1
        raw = await async_move_fn(messages)
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
        messages.append({"role": "user", "content": format_opponent_move_prompt(mv)})
        return mv

    while len(board.move_stack) < max_plies + len(opening_uci_moves) and not board.is_game_over():
        rec = await llm_turn()
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

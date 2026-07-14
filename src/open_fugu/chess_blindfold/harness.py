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
#
# 2026-07-12 revision (prompt-engineering pass suggested by Phase 0.5's
# REVIEW_NEEDED verdict, reports/phase0_5_summary.json): the old wording
# ("Reply with ONLY your move ... with no other text") was stricter than
# extract_uci_move() actually requires -- that function already scans the
# whole reply for the first legal-looking UCI/SAN token, tolerating
# reasoning/prose around it. Telling models (especially reasoning-distill
# ones, which naturally emit a <think> block regardless of instruction) that
# NO other text is allowed didn't change what the code accepts, only added
# an anxiety-inducing constraint models can't actually satisfy. This version
# explicitly permits brief reasoning (paired with local_worker.py's
# scaled_max_new_tokens() giving reasoning models enough budget to use it)
# and adds an explicit reminder to track the position from the move history
# rather than the starting position -- the actual failure mode a floor-check
# rerun should help confirm/rule out.
MOVE_FORMAT_INSTRUCTION = (
    "You may briefly reason about the current position, but you must track "
    "it from the move history above (each piece's CURRENT square after "
    "those moves, not the starting position) -- your move must be strictly "
    "legal in that resulting position. End your reply with your move in UCI "
    "notation (four or five lowercase letters/digits, e.g. e2e4 or e7e8q) "
    "on its own, with nothing after it."
)


def format_opening_prompt(
    color: str, opening_uci_moves: List[str], variant_description: Optional[str] = None
) -> str:
    """Reproduce Listing 1's setup prompt: numbered move pairs in UCI, ending
    with 'What is your move?'.

    `variant_description` overrides the default "you are playing a chess
    game" framing -- pass e.g. `open_fugu.minichess.board.GARDNER_VARIANT_DESCRIPTION`
    for a non-standard board. Without it, a model has zero signal that it
    isn't standard 8x8 chess and reliably hallucinates a standard starting
    position instead (see that constant's own comment for the 2026-07-14
    evidence: 0% legal-move rate, every game dying on move 1, raw replies
    describing a fabricated 8x8 board). Defaults to None so every existing
    full-chess caller is byte-for-byte unaffected.
    """
    parts = []
    for i in range(0, len(opening_uci_moves), 2):
        move_no = i // 2 + 1
        white_mv = opening_uci_moves[i]
        black_mv = opening_uci_moves[i + 1] if i + 1 < len(opening_uci_moves) else ""
        parts.append(f"{move_no}. {white_mv} {black_mv}".strip())
    history_str = " ".join(parts)
    if variant_description:
        intro = f"{variant_description} You are playing with {color} pieces."
    else:
        # Byte-identical to the pre-2026-07-14 wording -- every existing
        # full-chess caller (Phase 0.5/1/3/4/5, train_sft.py's prompt
        # reconstruction from already-collected data) must see the exact
        # same text as before; only a variant_description changes anything.
        intro = f"You are playing a chess game and you are playing with {color} pieces."
    return (
        f"{intro} "
        f"Current move history is {history_str}. What is your move? {MOVE_FORMAT_INSTRUCTION}"
    )


def format_opponent_move_prompt(mv: str) -> str:
    return f"Opponent played {mv}. What is your move? {MOVE_FORMAT_INSTRUCTION}"


# Inverts format_opening_prompt/format_opponent_move_prompt well enough for a
# STATELESS per-query dispatcher (Phase 5's Fugu-router orchestrator, see
# a2a/orchestrator_agent.py) to reconstruct the running move history from
# only the latest turn's raw text, without a chess.Board of its own --
# deliberately regex-based rather than routed through extract_uci_move
# (which needs a board to check legality): the orchestrator never sees the
# real board (blindfold-ness stays server-side, on the green judge), so this
# only ever extracts what the judge's own controlled prompt text already
# told the LLM side, not an arbitrary/untrusted move.
OPENING_COLOR_RE = re.compile(r"playing with (white|black) pieces")
OPENING_HISTORY_RE = re.compile(r"[Cc]urrent move history is (.*?)\.\s*What is your move\?")
OPPONENT_MOVE_RE = re.compile(r"Opponent played ([a-h][1-8]-?[a-h][1-8][qrbn]?)\.", re.IGNORECASE)


def parse_orchestrator_turn(text: str) -> dict:
    """Returns {"color": "white"|"black"|None, "opening_moves": [...],
    "opponent_moves": [...]}.

    `opening_moves` is only non-empty on the very first turn of a game (when
    the "Current move history is ..." preamble is present); `opponent_moves`
    holds every "Opponent played X" move embedded in THIS turn's text --
    normally one, except the very first turn of a game where the LLM plays
    black, where harness.py folds the engine's reply to the fixed opening
    into that same initial message (see play_blindfold_vs_engine's
    docstring), giving two.
    """
    color_match = OPENING_COLOR_RE.search(text)
    color = color_match.group(1) if color_match else None

    opening_moves: List[str] = []
    history_match = OPENING_HISTORY_RE.search(text)
    if history_match:
        opening_moves = [
            "".join(g for g in m.groups() if g).lower()
            for m in UCI_RE.finditer(history_match.group(1))
        ]

    opponent_moves = [m.group(1).replace("-", "").lower() for m in OPPONENT_MOVE_RE.finditer(text)]

    return {"color": color, "opening_moves": opening_moves, "opponent_moves": opponent_moves}


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
    variant_description: Optional[str] = None,
) -> GameResult:
    """Play one blindfold game: the LLM (via move_fn) against an engine baseline
    (e.g. Stockfish at a fixed skill level) that DOES see the board normally --
    only the LLM side is blindfolded, matching the paper's Fugu-vs-Stockfish games.

    move_fn receives the full running message transcript (system-free, just
    user/assistant turns) and returns raw text; engine_best_move_fn receives the
    board (real chess.Board by default) and returns its move in UCI (the engine
    is not blindfolded). Pass board_factory=GardnerBoard (from
    open_fugu.minichess.board) for the 5x5 track, and
    variant_description=GARDNER_VARIANT_DESCRIPTION alongside it -- see
    format_opening_prompt()'s docstring for why the description is required
    (not optional-nice-to-have) for any non-standard board.
    """
    board = board_factory()
    for mv in opening_uci_moves:
        board.push_uci(mv)

    color_name = "white" if llm_color == chess.WHITE else "black"
    opening_prompt = format_opening_prompt(color_name, opening_uci_moves, variant_description)
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
    variant_description: Optional[str] = None,
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
    opening_prompt = format_opening_prompt(color_name, opening_uci_moves, variant_description)
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

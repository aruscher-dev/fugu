"""m4's SFT data collection loop on 5x5 Gardner Minichess -- the minichess
track's twin of open_fugu.data.collect_sft_data, per PLAN.md's m4 row ("reuse
Phase 3's design, cheap here"). Same twin-not-generalization reasoning as
positions.py's module docstring: query every candidate worker n times on each
sampled Gardner position, extract + score the reply via GardnerScorer, and
append one record per (position, sample) to a JSONL file. m5 (SVF + selection
head + SFT training on 5x5) is what turns these into a soft target
distribution, same split as Phase 3/4.

load_done_keys() is pure JSON logic with zero chess.Board/python-chess
dependency (it only ever reads {"position_idx", "sample_idx"} keys already
written to a JSONL file) -- reused directly from
open_fugu.data.collect_sft_data rather than duplicated here.

harness.format_opening_prompt()/extract_uci_move() are also reused directly:
the former is pure move-history-text formatting (board-agnostic), and the
latter already accepts any board_factory-produced object duck-typing
chess.Board's `.legal_moves`/`.parse_san()` (see harness.py's own module
docstring and GardnerBoard's) -- both are exactly the "protocol is board-size-
agnostic" pieces m1/m3's notes already established, unlike the board/scorer
construction itself which stays a twin.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

from open_fugu.chess_blindfold.harness import extract_uci_move, format_opening_prompt
from open_fugu.data.chess_positions import SampledPosition
from open_fugu.data.collect_sft_data import load_done_keys  # noqa: F401 -- re-exported for m4_collect_minichess_sft_data.py
from open_fugu.minichess.board import GardnerBoard
from open_fugu.minichess.engine import GardnerScorer
from open_fugu.models.local_worker import (
    REASONING_WORKER_IDS, LocalWorker, LocalWorkerConfig, scaled_max_new_tokens,
)

DEFAULT_SCORE_DEPTH = 10  # scoring (not self-play generation) depth -- deeper than m2's ENGINE_FLOOR_DEPTH=8 since this only runs once per collected sample, not once per floor-check ply


def _score_reply(position: SampledPosition, sample_idx: int, board: GardnerBoard,
                  raw_reply: str, scorer: GardnerScorer) -> dict:
    move_uci = extract_uci_move(raw_reply, board)
    record = {
        "position_idx": position.position_idx,
        "sample_idx": sample_idx,
        "color_to_move": position.color_to_move,
        "raw_reply": raw_reply[:500],  # truncated -- essential for debugging a bad parse/score, not full transcripts -- same as collect_sft_data.py
        "move_uci": move_uci,
        "legal": move_uci is not None,
    }
    if move_uci is not None:
        ms = scorer.score_move(board, move_uci)
        record["centipawn_loss"] = ms.centipawn_loss
        record["is_blunder"] = ms.is_blunder
        record["is_mistake"] = ms.is_mistake
    else:
        # No parseable move at all -- score it the same as any other
        # unusable reply (GardnerScorer.score_move's own convention for a
        # malformed/illegal move: max loss, blunder, mistake).
        record["centipawn_loss"] = GardnerScorer.MATE_SCORE_CP
        record["is_blunder"] = True
        record["is_mistake"] = True
    return record


def _board_for(position: SampledPosition) -> GardnerBoard:
    board = GardnerBoard()
    for mv in position.opening_uci_moves:
        board.push_uci(mv)
    return board


def query_one_sample(worker: LocalWorker, scorer: GardnerScorer, position: SampledPosition,
                      sample_idx: int, max_new_tokens: int) -> dict:
    board = _board_for(position)
    prompt = format_opening_prompt(position.color_to_move, position.opening_uci_moves)
    raw_reply = worker.generate([{"role": "user", "content": prompt}], max_new_tokens=max_new_tokens)
    return _score_reply(position, sample_idx, board, raw_reply, scorer)


def query_batch(worker: LocalWorker, scorer: GardnerScorer,
                 items: List[Tuple[SampledPosition, int]], max_new_tokens: int) -> List[dict]:
    """Batched twin of query_one_sample() -- see collect_sft_data.query_batch's
    docstring for why this is safe/faster, not just equivalent (every item
    here is an independent single-turn prompt, no game-state dependency
    forcing sequential order)."""
    boards: List[GardnerBoard] = []
    batch_messages: List[List[dict]] = []
    for position, _sample_idx in items:
        boards.append(_board_for(position))
        prompt = format_opening_prompt(position.color_to_move, position.opening_uci_moves)
        batch_messages.append([{"role": "user", "content": prompt}])

    raw_replies = worker.generate_batch(batch_messages, max_new_tokens=max_new_tokens)
    return [
        _score_reply(position, sample_idx, board, raw_reply, scorer)
        for (position, sample_idx), board, raw_reply in zip(items, boards, raw_replies)
    ]


def collect_for_worker(worker_short_id: str, model_id: str, positions: List[SampledPosition],
                        n_samples: int, out_path: Path, max_new_tokens: int = 256,
                        temperature: float = 0.7, gardner_depth: int = DEFAULT_SCORE_DEPTH,
                        batch_size: int = 32) -> None:
    """Mirrors collect_sft_data.collect_for_worker exactly (same batching/
    resume/gen-budget discipline) -- see that function's docstring for the
    batch-size-measurement reasoning behind effective_batch_size."""
    done = load_done_keys(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.parent.chmod(0o700)

    pending = [(p, s) for p in positions for s in range(n_samples) if (p.position_idx, s) not in done]
    if not pending:
        return

    gen_budget = scaled_max_new_tokens(worker_short_id, max_new_tokens)
    effective_batch_size = 1 if worker_short_id in REASONING_WORKER_IDS else batch_size

    worker = LocalWorker(LocalWorkerConfig(model_id=model_id, max_new_tokens=gen_budget,
                                            temperature=temperature))
    scorer = GardnerScorer(depth=gardner_depth)
    try:
        with out_path.open("a") as f:
            for i in range(0, len(pending), effective_batch_size):
                chunk = pending[i:i + effective_batch_size]
                records = query_batch(worker, scorer, chunk, gen_budget)
                for record in records:
                    record["worker"] = worker_short_id
                    f.write(json.dumps(record) + "\n")
                f.flush()
    finally:
        scorer.close()
        worker.unload()
    out_path.chmod(0o600)

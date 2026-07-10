"""Phase 3 SFT data collection loop (per PLAN.md's training recipe step 1):
for one candidate worker, query every sampled position n times, extract +
score the reply via StockfishScorer, and append one record per (position,
sample) to a JSONL file. Positions are pure move-history prefixes (see
chess_positions.py) -- this reuses harness.py's own blindfold prompt format
for a single-move query instead of playing out a whole game, so a worker's
reward here reflects "how good is this one move given this exact history",
matching what Phase 4 needs to build a per-position soft target distribution
across workers.

Deliberately stops at "collect scored raw samples" -- Phase 4 is what turns
these into r̄ per worker/position and a softmax-τ soft target (τ is a
training hyperparameter, not this phase's business).

Records are appended and flushed after every single generation, so a
crashed/killed run (this phase is meant to span many cron cycles, per
PLAN.md's 2-4 day / ~10-20 GPU-hour estimate) loses at most one in-flight
sample; load_done_keys() lets a re-run skip whatever (position_idx,
sample_idx) pairs are already present.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Set, Tuple

import chess

from open_fugu.chess_blindfold.harness import extract_uci_move, format_opening_prompt
from open_fugu.data.chess_positions import SampledPosition
from open_fugu.models.local_worker import LocalWorker, LocalWorkerConfig
from open_fugu.reward.stockfish_scorer import StockfishScorer


def load_done_keys(path: Path) -> Set[Tuple[int, int]]:
    if not path.exists():
        return set()
    done = set()
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            done.add((rec["position_idx"], rec["sample_idx"]))
    return done


def query_one_sample(worker: LocalWorker, scorer: StockfishScorer, position: SampledPosition,
                      sample_idx: int, max_new_tokens: int) -> dict:
    board = chess.Board()
    for mv in position.opening_uci_moves:
        board.push_uci(mv)

    prompt = format_opening_prompt(position.color_to_move, position.opening_uci_moves)
    raw_reply = worker.generate([{"role": "user", "content": prompt}], max_new_tokens=max_new_tokens)
    move_uci = extract_uci_move(raw_reply, board)

    record = {
        "position_idx": position.position_idx,
        "sample_idx": sample_idx,
        "color_to_move": position.color_to_move,
        "raw_reply": raw_reply[:500],  # truncated -- essential for debugging a bad parse/score, not full transcripts
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
        # unusable reply (StockfishScorer.score_move's own convention for a
        # malformed/illegal move: max loss, blunder, mistake).
        record["centipawn_loss"] = StockfishScorer.MATE_SCORE_CP
        record["is_blunder"] = True
        record["is_mistake"] = True
    return record


def collect_for_worker(worker_short_id: str, model_id: str, positions: List[SampledPosition],
                        n_samples: int, out_path: Path, max_new_tokens: int = 256,
                        temperature: float = 0.7, stockfish_depth: int = 12) -> None:
    done = load_done_keys(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.parent.chmod(0o700)

    pending = [(p, s) for p in positions for s in range(n_samples) if (p.position_idx, s) not in done]
    if not pending:
        return

    worker = LocalWorker(LocalWorkerConfig(model_id=model_id, max_new_tokens=max_new_tokens,
                                            temperature=temperature))
    scorer = StockfishScorer(depth=stockfish_depth)
    try:
        with out_path.open("a") as f:
            for position, sample_idx in pending:
                record = query_one_sample(worker, scorer, position, sample_idx, max_new_tokens)
                record["worker"] = worker_short_id
                f.write(json.dumps(record) + "\n")
                f.flush()
    finally:
        scorer.close()
        worker.unload()
    out_path.chmod(0o600)

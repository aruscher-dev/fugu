"""Phase 4 SFT training (PLAN.md training recipe step 1, continued from
Phase 3): turn Phase 3's raw per-(worker, position, sample) records into a
soft target distribution over workers per position (mean reward ->
softmax-tau), then train the orchestrator backbone's selection head + SVF
`z` vectors (see models/worker_backend.py) against it via a plain AdamW loop
minimizing cross-entropy vs. that soft target -- PLAN.md's own description
("softmax-tau into a soft target distribution, train head+SVF-z via KL
divergence (plain AdamW custom loop)"); peft has no SVF adapter to lean on
here, same reasoning as models/svf.py.

Split into two halves on purpose:
  - build_soft_targets() and its helpers: pure data munging, no torch/GPU
    dependency at all -- testable in a plain sandbox with just the stdlib
    (this is what the cloud dev routine that wrote this file could actually
    verify).
  - train(): needs an OrchestratorBackbone instance (real GPU model) --
    only imports torch inside the function body so importing this module
    elsewhere never requires torch to be installed.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


@dataclass
class SoftTarget:
    position_idx: int
    opening_uci_moves: List[str]
    worker_ids: List[str]   # fixed order, matches `probs`
    probs: List[float]      # softmax-tau over mean reward, same order as worker_ids


def load_jsonl(path: Path) -> List[dict]:
    records = []
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def load_positions(positions_path: Path) -> Dict[int, List[str]]:
    return {rec["position_idx"]: rec["opening_uci_moves"] for rec in load_jsonl(positions_path)}


def mean_reward_per_position(records: List[dict]) -> Dict[int, float]:
    """reward = -centipawn_loss (higher is better). An illegal/unparseable
    move already carries StockfishScorer.MATE_SCORE_CP as its
    centipawn_loss, per collect_sft_data.py's convention -- it naturally
    gets the worst reward here without any special-casing."""
    losses_by_position: Dict[int, List[float]] = defaultdict(list)
    for rec in records:
        losses_by_position[rec["position_idx"]].append(float(rec["centipawn_loss"]))
    return {idx: -(sum(losses) / len(losses)) for idx, losses in losses_by_position.items()}


def softmax(xs: List[float]) -> List[float]:
    m = max(xs)
    exps = [math.exp(x - m) for x in xs]
    total = sum(exps)
    return [e / total for e in exps]


def build_soft_targets(positions: Dict[int, List[str]], worker_records: Dict[str, List[dict]],
                        tau: float = 1.0, reward_scale_cp: float = 100.0) -> List[SoftTarget]:
    """One SoftTarget per position that every worker in worker_records has
    at least one scored sample for -- positions only partially covered by
    the swarm (e.g. one worker's collection run got killed mid-position) are
    dropped rather than guessed at, so every training example reflects a
    genuine head-to-head comparison across the full worker pool.

    `reward_scale_cp` converts raw centipawn rewards to a pawns-ish unit
    before dividing by `tau`, so `tau` stays in a human-friendly range
    (~0.1-10) independent of Stockfish's centipawn scale.
    """
    worker_ids = sorted(worker_records)  # fixed, deterministic order
    per_worker_mean = {w: mean_reward_per_position(recs) for w, recs in worker_records.items()}

    targets: List[SoftTarget] = []
    for position_idx, opening_uci_moves in positions.items():
        if any(position_idx not in per_worker_mean[w] for w in worker_ids):
            continue
        rewards = [per_worker_mean[w][position_idx] / reward_scale_cp for w in worker_ids]
        probs = softmax([r / tau for r in rewards])
        targets.append(SoftTarget(position_idx=position_idx, opening_uci_moves=opening_uci_moves,
                                   worker_ids=worker_ids, probs=probs))
    return targets


def train(targets: List[SoftTarget], backbone, epochs: int = 3, lr: float = 1e-3,
          log_every: int = 50) -> List[float]:
    """Plain AdamW loop over backbone.trainable_parameters() (the selection
    head + every SVFLinear's z, per PLAN.md -- everything else in the
    backbone stays frozen). One step per (position, epoch); loss is
    cross-entropy of the backbone's predicted worker distribution against
    the position's precomputed soft target -- equivalent to KL divergence up
    to the target distribution's own entropy, which is a constant w.r.t. the
    trained parameters and so doesn't affect the gradient.

    Returns the full per-step loss history (not just epoch means) so the
    caller can report both a final loss and a short trailing-window mean --
    useful signal for a human reviewing whether training actually converged
    at all, not just that it ran."""
    import torch

    from open_fugu.chess_blindfold.harness import format_opening_prompt

    optimizer = torch.optim.AdamW(backbone.trainable_parameters(), lr=lr)
    losses: List[float] = []
    for epoch in range(epochs):
        for i, target in enumerate(targets):
            color = "white" if len(target.opening_uci_moves) % 2 == 0 else "black"
            prompt = format_opening_prompt(color, target.opening_uci_moves)

            logits = backbone(prompt)
            log_probs = torch.log_softmax(logits, dim=-1)
            target_probs = torch.tensor(target.probs, dtype=log_probs.dtype, device=log_probs.device)
            loss = -(target_probs * log_probs).sum()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            loss_value = loss.item()
            losses.append(loss_value)
            if (i + 1) % log_every == 0:
                print(f"  epoch {epoch} step {i + 1}/{len(targets)} loss={loss_value:.4f}", flush=True)
    return losses

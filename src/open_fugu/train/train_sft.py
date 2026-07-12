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
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


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


def split_train_val(targets: List[SoftTarget], val_frac: float = 0.15,
                     seed: int = 0) -> Tuple[List[SoftTarget], List[SoftTarget]]:
    """Deterministic held-out split, done once before training starts (not
    re-shuffled per epoch like the training order is) so val_loss is
    comparable across epochs. Pure-logic, no torch dependency -- see this
    module's docstring for why that split matters for cloud-sandbox
    testability.

    2026-07-12 deep-review finding this fixes: the original train() reported
    loss on the same examples it fit, in the same order every epoch, which
    made `final_loss`/`mean_loss_last_50` look like convergence metrics when
    they weren't -- there was no way to tell "fit the training data" apart
    from "learned a routing signal that generalizes.\""""
    if len(targets) < 2:
        return targets, []
    order = list(range(len(targets)))
    random.Random(seed).shuffle(order)
    n_val = max(1, round(len(targets) * val_frac))
    val_idx = set(order[:n_val])
    train_targets = [t for i, t in enumerate(targets) if i not in val_idx]
    val_targets = [t for i, t in enumerate(targets) if i in val_idx]
    return train_targets, val_targets


def _cross_entropy(target: SoftTarget, backbone) -> "torch.Tensor":  # noqa: F821 -- torch only imported by callers
    from open_fugu.chess_blindfold.harness import format_opening_prompt
    import torch

    color = "white" if len(target.opening_uci_moves) % 2 == 0 else "black"
    prompt = format_opening_prompt(color, target.opening_uci_moves)
    logits = backbone(prompt)
    log_probs = torch.log_softmax(logits, dim=-1)
    target_probs = torch.tensor(target.probs, dtype=log_probs.dtype, device=log_probs.device)
    return -(target_probs * log_probs).sum()


def evaluate(targets: List[SoftTarget], backbone) -> float:
    """Mean cross-entropy loss over `targets` with gradients disabled -- no
    optimizer step, no effect on training. Used for the held-out validation
    split; also callable standalone for a pre-training baseline check."""
    import torch

    if not targets:
        return float("nan")
    with torch.no_grad():
        total = sum(_cross_entropy(t, backbone).item() for t in targets)
    return total / len(targets)


def train(train_targets: List[SoftTarget], val_targets: List[SoftTarget], backbone,
          epochs: int = 3, lr: float = 1e-3, log_every: int = 50,
          shuffle_seed: int = 0) -> Tuple[List[float], List[float]]:
    """Plain AdamW loop over backbone.trainable_parameters() (the selection
    head + every SVFLinear's z, per PLAN.md -- everything else in the
    backbone stays frozen). One step per (position, epoch); loss is
    cross-entropy of the backbone's predicted worker distribution against
    the position's precomputed soft target -- equivalent to KL divergence up
    to the target distribution's own entropy, which is a constant w.r.t. the
    trained parameters and so doesn't affect the gradient.

    Re-shuffles `train_targets`' order every epoch (seeded, reproducible) --
    the original version iterated in the same fixed order every epoch, which
    the 2026-07-12 deep-review flagged as a real confound: positions late in
    that fixed order got systematically fresher gradient updates each epoch
    than positions early in it, not just harder/easier positions. Evaluates
    `val_targets` (held out by split_train_val(), never trained on) after
    every epoch under no_grad() -- this is the actual generalization signal
    that review found missing; `final_loss`/`mean_loss_last_50` alone can't
    distinguish "learned a real routing signal" from "memorized this exact
    400-position training set."

    Returns (train_losses, val_losses_per_epoch) -- train_losses is the full
    per-step history (not just epoch means), val_losses_per_epoch has one
    entry per epoch."""
    import torch

    optimizer = torch.optim.AdamW(backbone.trainable_parameters(), lr=lr)
    losses: List[float] = []
    val_losses: List[float] = []
    rng = random.Random(shuffle_seed)
    for epoch in range(epochs):
        order = list(range(len(train_targets)))
        rng.shuffle(order)
        for step, idx in enumerate(order):
            loss = _cross_entropy(train_targets[idx], backbone)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            loss_value = loss.item()
            losses.append(loss_value)
            if (step + 1) % log_every == 0:
                print(f"  epoch {epoch} step {step + 1}/{len(order)} loss={loss_value:.4f}", flush=True)

        val_loss = evaluate(val_targets, backbone)
        val_losses.append(val_loss)
        print(f"  epoch {epoch} done -- held-out val_loss={val_loss:.4f} "
              f"(n_val={len(val_targets)})", flush=True)
    return losses, val_losses

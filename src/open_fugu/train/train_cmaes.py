"""sep-CMA-ES training loop (PLAN.md training recipe step 2, "explicitly a
stretch goal"): evolve a flat parameter vector directly against end-to-end
task reward (full game rollouts), rather than SFT's supervised KL loss
against a precomputed soft target (train_sft.py). PLAN.md: "Validate the
loop first on gtbench's cheap kuhn_poker before spending chess GPU-hours on
it" -- this module is the reusable trainer half; scripts/phase7_cmaes_kuhn_pilot.py
is the kuhn_poker-specific fitness function + worker/backbone wiring.

Split like train_sft.py, deliberately: run_cmaes()/flatten_params()/
unflatten_params() are pure numpy, no torch/GPU/gtbench dependency at all --
testable in a plain sandbox with just `cma` + numpy installed (this is what
the cloud dev routine that wrote this file could actually verify: a toy
fitness_fn like a negated sphere function converges the same way a real
game-rollout fitness_fn would, CMA-ES itself doesn't know the difference).
Only the fitness_fn a caller supplies (real game rollouts through the
orchestrator + workers) needs GPU; this module never imports torch itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np


def flatten_params(weight: np.ndarray, bias: np.ndarray) -> np.ndarray:
    """`weight`/`bias` are a selection head's `nn.Linear` parameters
    (shapes `[n_workers, hidden_size]` / `[n_workers]`) -- flattens into the
    one vector CMA-ES actually evolves. Order (weight then bias) must match
    unflatten_params()."""
    return np.concatenate([np.asarray(weight).reshape(-1), np.asarray(bias).reshape(-1)])


def unflatten_params(flat: np.ndarray, weight_shape: Sequence[int],
                      bias_shape: Sequence[int]) -> Tuple[np.ndarray, np.ndarray]:
    flat = np.asarray(flat)
    n_weight = int(np.prod(weight_shape))
    n_bias = int(np.prod(bias_shape))
    expected = n_weight + n_bias
    if flat.shape[0] != expected:
        raise ValueError(
            f"flat param vector has {flat.shape[0]} entries, expected {expected} "
            f"(weight_shape={tuple(weight_shape)} + bias_shape={tuple(bias_shape)})"
        )
    weight = flat[:n_weight].reshape(weight_shape)
    bias = flat[n_weight:n_weight + n_bias].reshape(bias_shape)
    return weight, bias


@dataclass
class CMAESResult:
    best_params: np.ndarray
    best_fitness: float
    history_best: List[float] = field(default_factory=list)   # best-in-generation fitness
    history_mean: List[float] = field(default_factory=list)   # mean-in-generation fitness


def run_cmaes(fitness_fn: Callable[[np.ndarray], float], x0: np.ndarray, sigma0: float = 0.5,
              n_generations: int = 20, popsize: Optional[int] = None, seed: Optional[int] = None,
              maximize: bool = True, log_every: int = 1) -> CMAESResult:
    """Runs sep-CMA-ES (diagonal covariance -- the "sep" in PLAN.md's
    "sep-CMA-ES", scales ~O(n log n)/generation instead of full-covariance
    CMA-ES's O(n^2), needed once n climbs into the selection-head-sized
    thousands-of-parameters range this project's phases use) over
    `fitness_fn`, which must accept one flat parameter vector (same shape as
    `x0`) and return a scalar.

    The `cma` package's own `CMAEvolutionStrategy` always MINIMIZES;
    `maximize=True` (default -- `fitness_fn` returns a reward, higher is
    better, matching every other reward convention in this project e.g.
    train_sft.py's `mean_reward_per_position`) negates internally before
    calling `es.tell()`. Pass `maximize=False` if `fitness_fn` already
    returns a cost to minimize.

    Does not evaluate lazily/in parallel -- `fitness_fn` calls within one
    generation are sequential (`es.ask()` returns `popsize` candidates, each
    scored one at a time). For this project's real fitness functions (LLM
    generation + game rollouts), that IS the expensive part; the caller
    controls total cost via `popsize` * `n_generations` * (rollouts per
    fitness_fn call), not this function.
    """
    import cma

    opts = {"CMA_diagonal": True, "verbose": -9}
    if popsize is not None:
        opts["popsize"] = popsize
    if seed is not None:
        opts["seed"] = seed
    es = cma.CMAEvolutionStrategy(np.asarray(x0, dtype=float).tolist(), sigma0, opts)

    sign = -1.0 if maximize else 1.0
    result = CMAESResult(best_params=np.asarray(x0, dtype=float), best_fitness=float("-inf") if maximize else float("inf"))

    for generation in range(n_generations):
        solutions = es.ask()
        fitnesses = [float(fitness_fn(np.asarray(s, dtype=float))) for s in solutions]
        es.tell(solutions, [sign * f for f in fitnesses])

        best_in_gen = max(fitnesses) if maximize else min(fitnesses)
        mean_in_gen = sum(fitnesses) / len(fitnesses)
        result.history_best.append(best_in_gen)
        result.history_mean.append(mean_in_gen)

        is_new_best = (best_in_gen > result.best_fitness) if maximize else (best_in_gen < result.best_fitness)
        if is_new_best:
            result.best_fitness = best_in_gen
            result.best_params = np.asarray(solutions[fitnesses.index(best_in_gen)], dtype=float)

        if log_every and (generation % log_every == 0 or generation == n_generations - 1):
            print(f"  generation {generation}/{n_generations}: best_in_gen={best_in_gen:.4f} "
                  f"mean_in_gen={mean_in_gen:.4f} best_so_far={result.best_fitness:.4f}", flush=True)

    return result

#!/usr/bin/env python3
"""Phase 7 (stretch) -- sep-CMA-ES pilot on `kuhn_poker` (PLAN.md training
recipe step 2: "Validate the loop first on gtbench's cheap kuhn_poker before
spending chess GPU-hours on it"). Milestone table: "CMA-ES pilot on
kuhn_poker | 2-5 days".

What this validates, concretely: can `open_fugu.train.train_cmaes.run_cmaes`
(sep-CMA-ES, `cma` package) directly optimize the Fugu orchestrator's
selection-head weights against real end-to-end game reward (win/loss/draw
over full `gtbench` kuhn_poker matches, played through the SAME
`OrchestratorBackbone`/per-query-routing design Phase 4/5 use for chess --
see `gtbench_ext/orchestrator_router_model.py`) -- i.e. does the whole
mechanism (flatten params -> CMA-ES candidate -> load into
`selection_head` -> play real games -> scalar reward -> `cma.tell()`) run
end-to-end without silently breaking, BEFORE Phase 8 spends real chess
GPU-hours evolving against a much more expensive rollout.

Explicitly NOT a chess-quality or poker-strength claim -- see PLAN.md's
Verification section: "Any CMA-ES results (stretch phases) are explicitly
reported as scoped proof-of-concept." The router is evolved against a fixed
`RandomAgent` baseline (gtbench's own, no LLM cost) purely because it's a
cheap, always-legal opponent to validate the training loop against, not
because beating random play is a meaningful benchmark.

Scoping decision, worth flagging explicitly: CMA-ES here evolves ONLY the
selection head's weight+bias (a few thousand parameters for the default
2-worker pool), not the SVF `z` vectors SFT's stage 1 also trains (PLAN.md:
"train head+SVF-z"). `apply_svf` is still applied to the backbone (for
architecture parity with Phase 4/5's backbone, `z` stays frozen at its
default all-ones/no-op value) but `z` is not part of the flat parameter
vector CMA-ES evolves -- keeps this pilot's parameter count (and therefore
its GPU-hour budget) small while still validating the exact same
flatten/unflatten/load-into-backbone mechanics Phase 8 would reuse to also
evolve `z`.

Requires (installed idempotently by scripts/phase7_setup_gtbench.sh, called
at the top of main() below): vendor/gtbench (jinhaoduan/GTBench clone),
`pyspiel` (open_spiel), `python-box`. Also needs the GPU (2 local worker
LLMs + the orchestrator backbone, same as every other GPU phase) -- cannot
run in the cloud dev sandbox that wrote this file; see reports/phase7_summary.json's
`gpu_spend_approved`-gated advancer in scripts/orchestrate.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from disk_guard import check_disk_budget  # noqa: E402

VENDOR_GTBENCH = PROJECT_DIR / "vendor" / "gtbench"
REPORT_PATH = PROJECT_DIR / "reports" / "phase7_summary.json"
CHECKPOINT_DIR = PROJECT_DIR / "checkpoints" / "phase7_cmaes_kuhn"
LOG_DIR = PROJECT_DIR / "logs"

DEFAULT_WORKERS = ["qwen2.5-7b", "mistral-7b"]  # small pool -- this is a mechanics pilot, not a quality run
ROUTER_AGENT_NAME = "FuguRouter"
BASELINE_AGENT_NAME = "RandomBaseline"


def _run_setup() -> None:
    result = subprocess.run(["bash", str(PROJECT_DIR / "scripts" / "phase7_setup_gtbench.sh")],
                             capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError("scripts/phase7_setup_gtbench.sh failed -- see output above")


def _import_gtbench():
    """Wires vendor/gtbench onto sys.path and neutralizes its unconditional
    `import langchain` (see gtbench_ext/_langchain_stub's docstring for
    exactly why) before touching any gamingbench module. Returns the handful
    of gtbench classes this pilot needs."""
    from open_fugu.gtbench_ext._langchain_stub import ensure_importable
    ensure_importable()

    gtbench_path = str(VENDOR_GTBENCH)
    if gtbench_path not in sys.path:
        sys.path.insert(0, gtbench_path)

    from gamingbench.utils.utils import LLMBenchLogger
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.chmod(0o700)
    # First construction anywhere in this process wins (LLMBenchLogger is a
    # singleton, see gamingbench/utils/utils.py) -- must happen before any
    # KuhnPoker()/agent construction, which each call LLMBenchLogger(None)
    # internally and would otherwise crash trying to open a `None` log path.
    LLMBenchLogger(str(LOG_DIR / "phase7_gtbench.log"))

    from gamingbench.agents.prompt_agent import PromptAgent
    from gamingbench.agents.random_agent import RandomAgent
    from gamingbench.games.kuhn_poker import KuhnPoker
    from gamingbench.utils.history_tracker import HistoryTracker

    return PromptAgent, RandomAgent, KuhnPoker, HistoryTracker


def build_agents_and_models(worker_short_ids: list, backbone, PromptAgent, RandomAgent,
                             max_tokens: int, temperature: float):
    """Router side: PromptAgent driven by an OrchestratorRouterModel (real
    per-query dispatch over `worker_short_ids`, routed by `backbone`).
    Baseline side: gtbench's own RandomAgent (no LLM cost, always legal --
    a cheap fixed opponent to validate the training loop against, not a
    strength claim, see module docstring)."""
    import types

    from open_fugu.gtbench_ext.local_transformers_model import LocalTransformersModel
    from open_fugu.gtbench_ext.orchestrator_router_model import OrchestratorRouterModel

    worker_models = {}
    for short_id in worker_short_ids:
        cfg = types.SimpleNamespace(model_id=short_id, max_tokens=max_tokens, timeout=60,
                                     temperature=temperature, nick_name=short_id)
        worker_models[short_id] = LocalTransformersModel(cfg)

    router_cfg = types.SimpleNamespace(max_tokens=max_tokens, timeout=60, temperature=temperature,
                                        nick_name="fugu-router", worker_models=worker_models, backbone=backbone)
    router_model = OrchestratorRouterModel(router_cfg)

    router_agent_cfg = types.SimpleNamespace(agent_name=ROUTER_AGENT_NAME, num_generations=1, majority_vote=False)
    router_agent = PromptAgent(router_agent_cfg)
    router_agent.set_model(router_model)

    baseline_model = types.SimpleNamespace(nick_name="random-baseline")
    baseline_agent_cfg = types.SimpleNamespace(agent_name=BASELINE_AGENT_NAME, num_generations=1, majority_vote=False)
    baseline_agent = RandomAgent(baseline_agent_cfg)
    baseline_agent.set_model(baseline_model)

    return router_agent, router_model, baseline_agent, baseline_model


def play_one_match(game, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
                    router_seat: int) -> float:
    """One kuhn_poker match, router in `router_seat` (0 or 1). Returns this
    match's reward from the ROUTER's perspective: +1 win, -1 loss, 0 draw,
    -1 if the match aborted (`status != "Normal"` -- can only be the
    router's fault here, since RandomAgent always picks a legal move from
    `observations['legal_moves']` by construction)."""
    game.reset()
    tracker = HistoryTracker()
    if router_seat == 0:
        game.play([router_agent, baseline_agent], [router_model, baseline_model], tracker)
    else:
        game.play([baseline_agent, router_agent], [baseline_model, router_model], tracker)

    match = tracker.matches[-1]
    if match.status != "Normal":
        return -1.0
    router_full_name = f"{router_agent.agent_name}_{router_model.nick_name}"
    if match.winner == router_full_name:
        return 1.0
    if match.winner == "":
        return 0.0
    return -1.0


def make_fitness_fn(game, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
                     weight_shape, bias_shape, n_games_per_eval: int):
    import torch

    from open_fugu.train.train_cmaes import unflatten_params

    def fitness_fn(flat_params):
        weight, bias = unflatten_params(flat_params, weight_shape, bias_shape)
        head = router_model.backbone.selection_head
        with torch.no_grad():
            head.weight.copy_(torch.as_tensor(weight, dtype=head.weight.dtype, device=head.weight.device))
            head.bias.copy_(torch.as_tensor(bias, dtype=head.bias.dtype, device=head.bias.device))

        rewards = [
            play_one_match(game, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
                            router_seat=i % 2)
            for i in range(n_games_per_eval)
        ]
        return sum(rewards) / len(rewards)

    return fitness_fn


def final_eval(game, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
               n_games: int) -> dict:
    outcomes = [
        play_one_match(game, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
                        router_seat=i % 2)
        for i in range(n_games)
    ]
    return {
        "n_games": n_games,
        "win_rate": sum(1 for r in outcomes if r == 1.0) / n_games,
        "loss_or_illegal_rate": sum(1 for r in outcomes if r == -1.0) / n_games,
        "draw_rate": sum(1 for r in outcomes if r == 0.0) / n_games,
        "mean_reward": sum(outcomes) / n_games,
    }


def write_summary(args, n_params: int, cma_result, held_out: dict, checkpoint_path: Path) -> dict:
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "game": "kuhn_poker",
        "workers": args.workers,
        "backbone_model_id": args.backbone_model_id,
        "n_evolved_params": n_params,
        "cmaes": {
            "n_generations": args.n_generations,
            "popsize": args.popsize,
            "n_games_per_eval": args.n_games_per_eval,
            "sigma0": args.sigma0,
            "best_fitness_mean_reward": cma_result.best_fitness,
            "history_best": cma_result.history_best,
            "history_mean": cma_result.history_mean,
        },
        "held_out_eval_vs_random_baseline": held_out,
        "checkpoint_path": str(checkpoint_path),
        "verdict": "COMPLETE",
        "note": ("Scoped proof-of-concept only (PLAN.md's Verification section: 'Any CMA-ES "
                 "results (stretch phases) are explicitly reported as scoped proof-of-concept') "
                 "-- validates that sep-CMA-ES (open_fugu.train.train_cmaes.run_cmaes) can "
                 "optimize the Fugu orchestrator's selection head against real end-to-end "
                 "kuhn_poker rewards end-to-end, not a claim about poker strength or about "
                 "chess (Phase 8's own, much more expensive, still-not-yet-written scope). "
                 "Opponent is gtbench's own RandomAgent (no LLM, always legal) -- chosen as a "
                 "cheap fixed baseline to validate the training loop against, not a benchmark. "
                 "Evolved parameters are the selection head's weight+bias ONLY -- SVF's `z` "
                 "vectors are frozen at their default no-op value here (see this script's module "
                 "docstring for why) -- Phase 8 would need to widen the flat parameter vector to "
                 "include them if full parity with SFT's trainable_parameters() is wanted."),
    }
    REPORTS_DIR = PROJECT_DIR / "reports"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.chmod(0o700)
    REPORT_PATH.write_text(json.dumps(summary, indent=2))
    REPORT_PATH.chmod(0o600)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", nargs="*", default=DEFAULT_WORKERS)
    ap.add_argument("--backbone-model-id", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--svf-n-last-layers", type=int, default=3)
    ap.add_argument("--worker-max-tokens", type=int, default=64)  # <Pass>/<Bet> only -- kuhn_poker's whole action space
    ap.add_argument("--worker-temperature", type=float, default=0.7)
    ap.add_argument("--n-generations", type=int, default=8)
    ap.add_argument("--popsize", type=int, default=6)
    ap.add_argument("--n-games-per-eval", type=int, default=6)
    ap.add_argument("--n-final-eval-games", type=int, default=40)
    ap.add_argument("--sigma0", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args()

    if REPORT_PATH.exists():
        existing = json.loads(REPORT_PATH.read_text())
        if existing.get("verdict") == "COMPLETE":
            print(f"{REPORT_PATH} already COMPLETE, nothing to do (delete it to force a re-run).")
            return

    check_disk_budget()
    _run_setup()
    PromptAgent, RandomAgent, KuhnPoker, HistoryTracker = _import_gtbench()

    import torch

    from open_fugu.models.worker_backend import OrchestratorBackbone, OrchestratorBackboneConfig
    from open_fugu.train.train_cmaes import flatten_params, run_cmaes, unflatten_params

    backbone_config = OrchestratorBackboneConfig(
        worker_ids=args.workers,
        backbone_model_id=args.backbone_model_id,
        svf_n_last_layers=args.svf_n_last_layers,
        device=args.device,
    )
    backbone = OrchestratorBackbone(backbone_config)
    backbone.eval()

    router_agent, router_model, baseline_agent, baseline_model = build_agents_and_models(
        args.workers, backbone, PromptAgent, RandomAgent, args.worker_max_tokens, args.worker_temperature,
    )

    game = KuhnPoker()

    weight_shape = tuple(backbone.selection_head.weight.shape)
    bias_shape = tuple(backbone.selection_head.bias.shape)
    x0 = flatten_params(
        backbone.selection_head.weight.detach().cpu().numpy(),
        backbone.selection_head.bias.detach().cpu().numpy(),
    )
    print(f"Evolving {x0.shape[0]} selection-head parameters "
          f"(weight {weight_shape} + bias {bias_shape}) over {len(args.workers)} workers", flush=True)

    fitness_fn = make_fitness_fn(game, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
                                  weight_shape, bias_shape, args.n_games_per_eval)

    result = run_cmaes(fitness_fn, x0, sigma0=args.sigma0, n_generations=args.n_generations,
                        popsize=args.popsize, seed=args.seed, maximize=True)

    best_weight, best_bias = unflatten_params(result.best_params, weight_shape, bias_shape)
    with torch.no_grad():
        backbone.selection_head.weight.copy_(
            torch.as_tensor(best_weight, dtype=backbone.selection_head.weight.dtype,
                             device=backbone.selection_head.weight.device))
        backbone.selection_head.bias.copy_(
            torch.as_tensor(best_bias, dtype=backbone.selection_head.bias.dtype,
                             device=backbone.selection_head.bias.device))

    held_out = final_eval(game, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
                           args.n_final_eval_games)
    print(f"Held-out eval vs. RandomAgent: {held_out}", flush=True)

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_DIR.chmod(0o700)
    checkpoint_path = CHECKPOINT_DIR / "selection_head.pt"
    torch.save({
        "selection_head": backbone.selection_head.state_dict(),
        "worker_ids": args.workers,
        "backbone_model_id": args.backbone_model_id,
        "svf_n_last_layers": args.svf_n_last_layers,
        "game": "kuhn_poker",
    }, checkpoint_path)
    checkpoint_path.chmod(0o600)

    summary = write_summary(args, x0.shape[0], result, held_out, checkpoint_path)
    print(f"\nVerdict: {summary['verdict']} (win_rate_vs_random={held_out['win_rate']})")


if __name__ == "__main__":
    main()

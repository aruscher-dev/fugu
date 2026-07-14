#!/usr/bin/env python3
"""Phase 9 (stretch) -- gtbench extension (PLAN.md phase table: "gtbench
extension (connect_four/breakthrough + kuhn_poker) | 2-4 days").

Phase 7 validated the whole sep-CMA-ES mechanism (flatten params -> CMA-ES
candidate -> load into the Fugu orchestrator's selection_head -> play real
end-to-end games -> scalar reward -> cma.tell()) against exactly one cheap
game, `kuhn_poker`, before Phase 8 spent real chess GPU-hours on the same
loop. Phase 9 is the breadth check that pilot's own module docstring implied
but didn't itself answer: does that mechanism generalize past poker, to
game types with meaningfully different action/observation shapes -- a
column-pick game (`connect_four`, ~4200 possible board states before either
side must react, small discrete action space) and a coordinate-move game
(`breakthrough`, on a deliberately small 3-column board, see
`gtbench_ext/game_registry.py`'s docstring for why)? Per PLAN.md's
Verification section ("Any CMA-ES results (stretch phases) are explicitly
reported as scoped proof-of-concept"), this is explicitly NOT a strength
claim for any of the three games, same disclaimer Phase 7/8 both carry.

Deliberately smaller per-game CMA-ES budget than Phase 7's kuhn_poker-only
pilot (PLAN.md's own estimate for this phase, "2-4 days" for THREE games
combined, is less than Phase 7's "2-5 days" for kuhn_poker ALONE) -- Phase 7
already spent its budget proving the mechanism works at all; this phase
spends its smaller budget proving it *generalizes*, not re-proving it
converges well on any one game. `write_summary()`'s per-game `note` says so
explicitly.

Reuses `open_fugu.train.train_cmaes.run_cmaes` verbatim (same as Phase 8),
`gtbench_ext.local_transformers_model.LocalTransformersModel` /
`gtbench_ext.orchestrator_router_model.OrchestratorRouterModel` verbatim
(both already game-agnostic -- confirmed by reading `gamingbench.agents.
random_agent.RandomAgent`/`prompt_agent.PromptAgent` and `gamingbench.
prompts.*`'s `env_name`-keyed dispatch directly: neither the fixed
RandomAgent baseline nor the router's own PromptAgent wrapper needed a
single line changed to work with connect_four/breakthrough, only the
per-game `GameSpec` in `gtbench_ext/game_registry.py` differs). Each
requested game gets its own **fresh** `OrchestratorBackbone`/selection head
(same "own fresh backbone per pilot" choice Phase 7 made) -- this phase
reports whether the mechanism generalizes across games, not whether
cross-game weight transfer helps (a fair question, left as future work).

Idempotent per-game, not just per-script (see `write_summary`/`main`):
`reports/phase9_summary.json`'s `games` dict is checked before each
requested game's own CMA-ES run starts, and re-persisted after every game
finishes -- so a crash-and-cron-relaunch partway through `--games
kuhn_poker connect_four breakthrough` resumes at the first not-yet-COMPLETE
game rather than re-running already-finished ones (Phase 7/8's own
single-game scripts can't meaningfully resume *mid*-CMA-ES-run either, for
the same reason documented there: the ask/tell state lives only in that one
process -- this phase's resumability is at the *per-game* granularity, not
finer).

Requires (installed idempotently by scripts/phase7_setup_gtbench.sh, called
at the top of main() below -- shared with Phase 7, not duplicated):
vendor/gtbench (jinhaoduan/GTBench clone), `pyspiel` (open_spiel),
`python-box`. Also needs the GPU (2 local worker LLMs + a fresh orchestrator
backbone per game) -- cannot run in the cloud dev sandbox that wrote this
file; see reports/phase9_summary.json's `gpu_spend_approved`-gated advancer
in scripts/orchestrate.py.
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
REPORT_PATH = PROJECT_DIR / "reports" / "phase9_summary.json"
CHECKPOINT_DIR = PROJECT_DIR / "checkpoints" / "phase9_cmaes_gtbench"
LOG_DIR = PROJECT_DIR / "logs"

DEFAULT_WORKERS = ["qwen2.5-7b", "mistral-7b"]  # same small pool Phase 7 used -- mechanics/generalization pilot, not a quality run
DEFAULT_GAMES = ["kuhn_poker", "connect_four", "breakthrough"]  # PLAN.md phase table's own ordering
ROUTER_AGENT_NAME = "FuguRouter"
BASELINE_AGENT_NAME = "RandomBaseline"


def _run_setup() -> None:
    # Shared with Phase 7 -- clones vendor/gtbench + installs pyspiel/python-box
    # idempotently, verifies kuhn_poker imports. Deliberately not duplicated
    # into a phase9-specific setup script.
    result = subprocess.run(["bash", str(PROJECT_DIR / "scripts" / "phase7_setup_gtbench.sh")],
                             capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError("scripts/phase7_setup_gtbench.sh failed -- see output above")

    # Phase 7's own setup script only verifies kuhn_poker (it predates this
    # phase). connect_four/breakthrough are part of the same vendor/gtbench
    # clone and the same pyspiel install (no extra system deps -- confirmed
    # in this session's sandbox), so this is a pure verification step, not
    # an additional install.
    # Same LLMBenchLogger-singleton-ordering + langchain-stub wiring
    # _import_gtbench()/phase7_setup_gtbench.sh's own verification need (see
    # that script's header comment) -- found the hard way (2026-07-14) when
    # this exact snippet crashed the same way phase7's setup verification
    # originally did, before ANY game construction here also claimed the
    # LLMBenchLogger singleton with a real path first.
    gtbench_path = str(VENDOR_GTBENCH)
    src_path = str(PROJECT_DIR / "src")
    result = subprocess.run(
        ["/Data/.venv/bin/python3", "-c", f"""
import sys
sys.path.insert(0, {src_path!r})
sys.path.insert(0, {gtbench_path!r})
from open_fugu.gtbench_ext._langchain_stub import ensure_importable
ensure_importable()
from gamingbench.utils.utils import LLMBenchLogger
LLMBenchLogger({str(LOG_DIR / "phase9_setup_verify.log")!r})
from gamingbench.games.connect_four import ConnectFour
from gamingbench.games.breakthrough import Breakthrough
c = ConnectFour()
assert not c.env.is_terminal()
b = Breakthrough()
assert not b.env.is_terminal()
assert b.game.num_distinct_actions() == 288, "expected the small 3-column board, see game_registry.py"
print('[phase9-setup] gamingbench connect_four/breakthrough OK')
"""],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError("connect_four/breakthrough import verification failed -- see output above")


def _import_gtbench():
    """Same wiring as Phase 7's `_import_gtbench()` (langchain stub +
    sys.path + LLMBenchLogger singleton), returning the extra bits Phase 9
    needs on top: `open_fugu.gtbench_ext.game_registry.GAME_SPECS` (imported
    only after vendor/gtbench is on sys.path, since its `GameSpec.make`
    closures import `gamingbench.games.*` lazily -- see that module's
    docstring)."""
    from open_fugu.gtbench_ext._langchain_stub import ensure_importable
    ensure_importable()

    gtbench_path = str(VENDOR_GTBENCH)
    if gtbench_path not in sys.path:
        sys.path.insert(0, gtbench_path)

    from gamingbench.utils.utils import LLMBenchLogger
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.chmod(0o700)
    LLMBenchLogger(str(LOG_DIR / "phase9_gtbench.log"))

    from gamingbench.agents.prompt_agent import PromptAgent
    from gamingbench.agents.random_agent import RandomAgent
    from gamingbench.utils.history_tracker import HistoryTracker

    from open_fugu.gtbench_ext.game_registry import GAME_SPECS

    return PromptAgent, RandomAgent, HistoryTracker, GAME_SPECS


def build_agents_and_models(worker_short_ids: list, backbone, PromptAgent, RandomAgent,
                             max_tokens: int, temperature: float):
    """Identical to Phase 7's `build_agents_and_models` (game-agnostic --
    neither `LocalTransformersModel`/`OrchestratorRouterModel` nor
    `PromptAgent`/`RandomAgent` know which game they're playing, that's
    entirely driven by the `observations['env_name']` dict `game.play()`
    hands them each turn)."""
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


def play_one_match(game_spec, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
                    router_seat: int) -> float:
    """Same reward convention as Phase 7's `play_one_match` (+1 win / -1
    loss-or-abnormal / 0 draw, router's perspective), but builds a **fresh**
    `game_spec.make()` instance for this one match instead of taking an
    already-constructed `game` and calling `.reset()` on it -- see
    `gtbench_ext/game_registry.py`'s module docstring for the real upstream
    bugs (crash for connect_four, silent wrong-board-size for breakthrough)
    this sidesteps."""
    game = game_spec.make()
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


def make_fitness_fn(game_spec, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
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
            play_one_match(game_spec, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
                            router_seat=i % 2)
            for i in range(n_games_per_eval)
        ]
        return sum(rewards) / len(rewards)

    return fitness_fn


def final_eval(game_spec, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
               n_games: int) -> dict:
    outcomes = [
        play_one_match(game_spec, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
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


def run_one_game(game_key: str, args, PromptAgent, RandomAgent, HistoryTracker, GAME_SPECS) -> dict:
    import torch

    from open_fugu.models.worker_backend import OrchestratorBackbone, OrchestratorBackboneConfig
    from open_fugu.train.train_cmaes import flatten_params, run_cmaes, unflatten_params

    game_spec = GAME_SPECS[game_key]

    # Fresh backbone per game -- this phase reports whether the CMA-ES
    # mechanism generalizes across game types, not whether cross-game weight
    # transfer helps (a fair follow-up question, left as future work; see
    # module docstring).
    backbone_config = OrchestratorBackboneConfig(
        worker_ids=args.workers,
        backbone_model_id=args.backbone_model_id,
        svf_n_last_layers=args.svf_n_last_layers,
        device=args.device,
    )
    backbone = OrchestratorBackbone(backbone_config)
    backbone.eval()

    router_agent, router_model, baseline_agent, baseline_model = build_agents_and_models(
        args.workers, backbone, PromptAgent, RandomAgent, game_spec.worker_max_tokens, args.worker_temperature,
    )

    weight_shape = tuple(backbone.selection_head.weight.shape)
    bias_shape = tuple(backbone.selection_head.bias.shape)
    x0 = flatten_params(
        backbone.selection_head.weight.detach().cpu().numpy(),
        backbone.selection_head.bias.detach().cpu().numpy(),
    )
    print(f"[{game_key}] evolving {x0.shape[0]} selection-head parameters "
          f"(weight {weight_shape} + bias {bias_shape}) over {len(args.workers)} workers", flush=True)

    fitness_fn = make_fitness_fn(game_spec, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
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

    held_out = final_eval(game_spec, HistoryTracker, router_agent, router_model, baseline_agent, baseline_model,
                           args.n_final_eval_games)
    print(f"[{game_key}] held-out eval vs. RandomAgent: {held_out}", flush=True)

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_DIR.chmod(0o700)
    checkpoint_path = CHECKPOINT_DIR / f"{game_key}_selection_head.pt"
    torch.save({
        "selection_head": backbone.selection_head.state_dict(),
        "worker_ids": args.workers,
        "backbone_model_id": args.backbone_model_id,
        "svf_n_last_layers": args.svf_n_last_layers,
        "game": game_key,
    }, checkpoint_path)
    checkpoint_path.chmod(0o600)

    return {
        "game": game_key,
        "n_evolved_params": int(x0.shape[0]),
        "cmaes": {
            "n_generations": args.n_generations,
            "popsize": args.popsize,
            "n_games_per_eval": args.n_games_per_eval,
            "sigma0": args.sigma0,
            "best_fitness_mean_reward": result.best_fitness,
            "history_best": result.history_best,
            "history_mean": result.history_mean,
        },
        "held_out_eval_vs_random_baseline": held_out,
        "checkpoint_path": str(checkpoint_path),
        "verdict": "COMPLETE",
        "note": (f"Scoped proof-of-concept only (PLAN.md's Verification section: 'Any CMA-ES results "
                 f"(stretch phases) are explicitly reported as scoped proof-of-concept') -- validates "
                 f"that sep-CMA-ES generalizes to {game_key} using the same mechanism Phase 7 already "
                 f"validated on kuhn_poker, not a {game_key}-strength claim. Deliberately a smaller "
                 f"CMA-ES budget than Phase 7's kuhn_poker-only pilot (this phase spreads its budget "
                 f"across 3 games to check generalization, not to re-prove convergence on any one). "
                 f"Opponent is gtbench's own RandomAgent (no LLM, always legal) -- a cheap fixed "
                 f"baseline to validate the training loop against, not a benchmark. Evolved parameters "
                 f"are the selection head's weight+bias ONLY, same scoping as Phase 7/8."),
    }


def load_or_init_summary(args) -> dict:
    if REPORT_PATH.exists():
        summary = json.loads(REPORT_PATH.read_text())
        summary.setdefault("games", {})
        return summary
    return {"games_requested": args.games, "games": {}}


def write_summary(summary: dict, args) -> dict:
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    summary["games_requested"] = args.games
    summary["workers"] = args.workers
    summary["backbone_model_id"] = args.backbone_model_id
    all_complete = all(summary["games"].get(g, {}).get("verdict") == "COMPLETE" for g in args.games)
    summary["verdict"] = "COMPLETE" if all_complete else "PARTIAL"
    summary["note"] = (
        "Multi-game extension of Phase 7's kuhn_poker-only sep-CMA-ES pilot (PLAN.md phase table: "
        "'gtbench extension (connect_four/breakthrough + kuhn_poker)') -- each game's own entry under "
        "'games' carries its own scoped-proof-of-concept disclaimer. 'verdict' is COMPLETE only once "
        "every game in 'games_requested' has its own verdict COMPLETE."
    )
    REPORTS_DIR = PROJECT_DIR / "reports"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.chmod(0o700)
    REPORT_PATH.write_text(json.dumps(summary, indent=2))
    REPORT_PATH.chmod(0o600)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", nargs="*", default=DEFAULT_GAMES, choices=list(DEFAULT_GAMES))
    ap.add_argument("--workers", nargs="*", default=DEFAULT_WORKERS)
    ap.add_argument("--backbone-model-id", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--svf-n-last-layers", type=int, default=3)
    ap.add_argument("--worker-temperature", type=float, default=0.7)
    ap.add_argument("--n-generations", type=int, default=5)
    ap.add_argument("--popsize", type=int, default=4)
    ap.add_argument("--n-games-per-eval", type=int, default=4)
    ap.add_argument("--n-final-eval-games", type=int, default=20)
    ap.add_argument("--sigma0", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args()

    summary = load_or_init_summary(args)
    remaining = [g for g in args.games if summary["games"].get(g, {}).get("verdict") != "COMPLETE"]
    if not remaining:
        print(f"{REPORT_PATH} already COMPLETE for every requested game, nothing to do "
              f"(delete it, or a game's own entry, to force a re-run).")
        return

    check_disk_budget()
    _run_setup()
    PromptAgent, RandomAgent, HistoryTracker, GAME_SPECS = _import_gtbench()

    for game_key in remaining:
        game_result = run_one_game(game_key, args, PromptAgent, RandomAgent, HistoryTracker, GAME_SPECS)
        summary["games"][game_key] = game_result
        summary = write_summary(summary, args)  # persist after every game -- see module docstring on resumability
        print(f"[{game_key}] done, win_rate_vs_random={game_result['held_out_eval_vs_random_baseline']['win_rate']}")

    print(f"\nOverall verdict: {summary['verdict']}")


if __name__ == "__main__":
    main()

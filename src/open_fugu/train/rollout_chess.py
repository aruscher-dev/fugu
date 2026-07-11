"""Phase 8 (stretch) -- in-process blindfold-chess rollout machinery for
sep-CMA-ES (PLAN.md training recipe step 2: "full blindfold games as
rollouts, reward blending win/loss/draw with graded -ACPL"). Reused by
scripts/phase8_cmaes_chess_pilot.py's fitness function, the same way
Phase 7's gtbench_ext/orchestrator_router_model.py provided the per-query
dispatch mechanics for kuhn_poker.

Deliberately in-process (direct open_fugu.models.local_worker.LocalWorker
calls), not A2A: CMA-ES needs many rollouts per generation (popsize x
n_games_per_eval), and Phase 5's A2A path pays a whole
worker-agent-plus-orchestrator-plus-green-judge subprocess-and-HTTP-readiness
cost per condition (see eval/run_eval_matches.py) -- fine for one 25-game
condition, far too slow to pay per CMA-ES candidate. Phase 7's own
gtbench_ext/orchestrator_router_model.py made the identical choice for the
same reason.

Split like every earlier phase's train/*.py: the pure half below (reward
blending, move-history reconstruction) has no torch/chess import and is
exactly what this sandbox (no GPU, no downloaded models) can actually unit
test -- the GPU half (make_dispatch_move_fn, which calls a real
OrchestratorBackbone.forward() and LocalWorker.generate()) needs both and
can only be exercised by the GPU host.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from open_fugu.chess_blindfold.harness import (
    UCI_RE,
    GameResult,
    format_opening_prompt,
    parse_orchestrator_turn,
)
from open_fugu.eval.aggregate_metrics import game_outcome

OUTCOME_REWARD = {"win": 1.0, "draw": 0.0, "loss": -1.0, "unresolved": 0.0}


def mean_centipawn_loss(plies) -> Optional[float]:
    """Mean centipawn loss over the LLM's own legal, scored plies (harness.py's
    GameResult.plies only ever holds the LLM side's moves -- the opponent
    engine's replies are threaded into the message transcript but never
    appended there, see play_blindfold_vs_engine). None if the LLM never
    played a single legal, scored move (e.g. illegal on move 1)."""
    losses = [p.centipawn_loss for p in plies if p.legal and p.centipawn_loss is not None]
    return (sum(losses) / len(losses)) if losses else None


def blend_reward(outcome: str, mean_cpl: Optional[float], acpl_scale: float = 100.0,
                  outcome_weight: float = 0.7) -> float:
    """PLAN.md: "reward blending win/loss/draw with graded -ACPL". Truncated
    games (Phase 8's whole point -- short max_plies to keep CMA-ES rollouts
    affordable) rarely reach a decisive result: most games end "unresolved"
    (the ply cap, harness.py's "max_plies" termination) with outcome reward
    0, which alone would give CMA-ES almost no gradient to climb. Blending in
    graded -ACPL (bounded to [-1, 0] via acpl_scale, so one blundered queen
    doesn't dwarf everything else) gives a continuous signal even when no
    game in a generation's whole rollout batch ever finishes decisively.

    mean_cpl is None only when the LLM never played a single legal, scored
    move (see mean_centipawn_loss) -- in that case the outcome component
    alone (typically "loss", from an immediate illegal-move termination)
    is the whole reward, since there is no ACPL to blend in.
    """
    outcome_component = OUTCOME_REWARD[outcome]
    if mean_cpl is None:
        return outcome_component
    acpl_component = max(-1.0, -mean_cpl / acpl_scale)
    return outcome_weight * outcome_component + (1 - outcome_weight) * acpl_component


class RoutingHistoryTracker:
    """Reconstructs the Fugu backbone's routing prompt from the FULL message
    transcript harness.py's move_fn callback already receives on every call
    (unlike a2a.orchestrator_agent.FuguSelectionDispatch, which only ever
    sees one turn's delta text over the A2A wire and must accumulate state
    across separate per-context calls) -- this in-process rollout has the
    whole transcript on hand already, so state is rebuilt fresh each call
    instead of persisted in a ctx_id-keyed dict.

    Deliberately a separate, small implementation rather than importing
    FuguSelectionDispatch's logic directly: that class is tightly coupled to
    A2A's ToolProvider/async event-queue plumbing this synchronous CMA-ES
    rollout has no use for, and untangling that coupling is out of scope for
    a phase this sandbox cannot GPU-test end-to-end. Uses the exact same
    parse_orchestrator_turn/format_opening_prompt/UCI_RE building blocks
    FuguSelectionDispatch uses, so the routing PROMPT a candidate is
    evaluated against matches production exactly, even though the
    bookkeeping around it differs.
    """

    def build_routing_prompt(self, messages: List[dict]) -> str:
        color: Optional[str] = None
        moves: List[str] = []
        for msg in messages:
            if msg["role"] == "user":
                parsed = parse_orchestrator_turn(msg["content"])
                if parsed["color"] and color is None:
                    color = parsed["color"]
                moves.extend(parsed["opening_moves"])
                moves.extend(parsed["opponent_moves"])
            elif msg["role"] == "assistant":
                match = UCI_RE.search(msg["content"])
                if match:
                    moves.append("".join(g for g in match.groups() if g).lower())
        return format_opening_prompt(color or "white", moves)


def make_dispatch_move_fn(backbone, worker_pool: Dict[str, "object"], max_new_tokens: int = 200):
    """Returns a synchronous harness.MoveFn performing real per-query Fugu
    routing in-process: reconstructs the routing prompt (RoutingHistoryTracker,
    above), runs backbone.forward() under torch.no_grad(), argmaxes to pick
    one worker from worker_pool (short_id -> already-loaded
    models.local_worker.LocalWorker, keyed the same way
    backbone.config.worker_ids orders the selection head's output), and asks
    that worker to generate a reply to a FRESH single-turn message built from
    the reconstructed prompt -- same "stateless, full-history-every-time"
    design a2a.orchestrator_agent.FuguSelectionDispatch uses for real A2A
    dispatch, so a CMA-ES-evolved selection head is evaluated against the
    same dispatch mechanics that would actually run in production.
    """
    import torch

    tracker = RoutingHistoryTracker()

    def move_fn(messages: List[dict]) -> str:
        prompt = tracker.build_routing_prompt(messages)
        with torch.no_grad():
            logits = backbone.forward(prompt)
        short_id = backbone.config.worker_ids[int(torch.argmax(logits).item())]
        worker = worker_pool[short_id]
        return worker.generate([{"role": "user", "content": prompt}], max_new_tokens=max_new_tokens)

    return move_fn


@dataclass
class RolloutOutcome:
    game_result: GameResult
    outcome: str                       # "win" | "draw" | "loss" | "unresolved"
    mean_centipawn_loss: Optional[float]
    reward: float


def play_one_rollout(move_fn, llm_color: bool, opening_uci_moves: List[str], scorer, max_plies: int,
                      worker_id_label: str = "fugu_router") -> RolloutOutcome:
    """One truncated blindfold game (harness.play_blindfold_vs_engine) against
    `scorer`'s engine (both the opponent's moves and the LLM's own
    centipawn-loss grading), reward-blended per blend_reward()."""
    from open_fugu.chess_blindfold.harness import play_blindfold_vs_engine

    result = play_blindfold_vs_engine(
        move_fn=move_fn,
        llm_color=llm_color,
        opening_uci_moves=opening_uci_moves,
        engine_best_move_fn=scorer.best_move,
        scorer=scorer,
        max_plies=max_plies,
        worker_id=worker_id_label,
    )
    llm_color_str = "white" if llm_color else "black"
    outcome = game_outcome({"result": result.result, "llm_color": llm_color_str})
    mean_cpl = mean_centipawn_loss(result.plies)
    reward = blend_reward(outcome, mean_cpl)
    return RolloutOutcome(game_result=result, outcome=outcome, mean_centipawn_loss=mean_cpl, reward=reward)

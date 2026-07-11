#!/usr/bin/env python3
"""The Fugu orchestrator as an A2A purple agent: the green judge only ever
talks to THIS agent (never directly to a worker), matching Fugu's paper
architecture where a backbone+selection-head dispatches each query to one
worker in the swarm. This agent, in turn, is an A2A *client* to the worker
agents (worker_agent.py) -- i.e. inter-agent dispatch happens over A2A too,
not an in-process function call.

Two dispatch strategies, selected via --router:

- `random` (default, Phase 1 scope): picks ONE worker at random per game (per
  A2A context_id) and sticks with it for that whole game, rather than the
  paper's real per-query learned routing. Reason: worker agents keep their
  own conversation history server-side, keyed by context_id -- switching
  workers mid-game would silently drop context (the new worker never saw
  earlier moves), unless the orchestrator forwards the FULL transcript on
  every call instead of relying on A2A context threading.

- `fugu` (Phase 5+): real per-query routing using Phase 4's trained
  OrchestratorBackbone (selection head + SVF-adapted backbone) checkpoint.
  This is the "forward the full transcript every call" redesign the random
  strategy's docstring above defers -- see FuguSelectionDispatch below.
  Stateless by construction: every worker call reconstructs the whole move
  history as a fresh format_opening_prompt-style message and opens a brand
  new A2A context on the worker side (new_conversation=True), so switching
  workers between queries is safe.

Run standalone:
  python -m open_fugu.a2a.orchestrator_agent --workers qwen2.5-7b=http://127.0.0.1:9101 --port 9200
  python -m open_fugu.a2a.orchestrator_agent --workers qwen2.5-7b=http://127.0.0.1:9101,mistral-7b=http://127.0.0.1:9102 \
      --router fugu --checkpoint checkpoints/phase4_sft/backbone_head_svf.pt --port 9200
"""
from __future__ import annotations

import argparse
import logging
import random
from typing import Optional

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

from open_fugu.agentbeats.tool_provider import ToolProvider
from open_fugu.chess_blindfold.harness import UCI_RE, format_opening_prompt, parse_orchestrator_turn

logger = logging.getLogger(__name__)


def prepare_agent_card(url: str) -> AgentCard:
    skill = AgentSkill(
        id="blindfold_chess_move",
        name="Blindfold chess move (Open-Fugu orchestrator)",
        description=(
            "Same interface as a single worker agent -- a running blindfold-chess "
            "conversation in, a move out -- but internally dispatches each game to "
            "a worker in the swarm (random-routing or a trained selection head, "
            "see --router)."
        ),
        tags=["chess", "blindfold", "game", "orchestrator", "fugu"],
        examples=[],
    )
    return AgentCard(
        name="open_fugu_orchestrator",
        description="Open-Fugu orchestrator agent -- dispatches to a swarm of worker agents over A2A.",
        url=url,
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(),
        skills=[skill],
    )


class RandomStickyDispatch:
    """Phase 1 scope: pick one worker at random per game (per context_id) and
    relay every subsequent turn's text verbatim to that same worker -- the
    worker's own A2A-context-keyed conversation memory (see
    worker_agent.WorkerAgentExecutor) does the rest, exactly like a solo
    worker would see the game."""

    def __init__(self, worker_urls: dict[str, str], seed: Optional[int] = None):
        self.worker_urls = worker_urls
        self.rng = random.Random(seed)
        self.ctx_id_to_worker: dict[str, str] = {}
        self._seen_contexts: set[str] = set()

    def _pick_worker_for(self, ctx_id: str) -> str:
        if ctx_id not in self.ctx_id_to_worker:
            short_id = self.rng.choice(list(self.worker_urls))
            self.ctx_id_to_worker[ctx_id] = short_id
            logger.info(f"[orchestrator/random] new game (context {ctx_id}) routed to worker '{short_id}'")
        return self.ctx_id_to_worker[ctx_id]

    async def dispatch(self, ctx_id: str, user_input: str, tool_provider: ToolProvider) -> str:
        short_id = self._pick_worker_for(ctx_id)
        worker_url = self.worker_urls[short_id]

        # First call for this context_id -> fresh conversation on the
        # worker's side too; subsequent calls continue it (ToolProvider
        # tracks the worker-side context_id per URL -- see its docstring for
        # the known single-game-at-a-time limitation).
        new_conversation = ctx_id not in self._seen_contexts
        self._seen_contexts.add(ctx_id)

        return await tool_provider.talk_to_agent(user_input, worker_url, new_conversation=new_conversation)


class FuguSelectionDispatch:
    """Real per-query routing (PLAN.md's paper-matching design), using Phase
    4's trained OrchestratorBackbone checkpoint (selection head + SVF `z`
    vectors) to pick a worker fresh on every single query instead of once
    per game.

    Stateless by construction: rather than relaying each turn's raw delta
    text ("Opponent played X...") the way RandomStickyDispatch does, this
    reconstructs the FULL move history from the accumulated
    parse_orchestrator_turn() output and re-sends it as a brand-new
    format_opening_prompt-style message on a brand-new worker-side A2A
    context (new_conversation=True) every time -- sidesteps the
    "switching workers mid-game silently drops context" problem entirely, at
    the cost of resending the whole history every turn (fine at these game
    lengths -- max_plies is capped in the low hundreds).

    Never touches a real chess.Board (blindfold-ness/legality checking stays
    entirely server-side on the green judge, same as every other component
    here) -- move-history reconstruction is regex-based, not
    board-validated; see harness.parse_orchestrator_turn's docstring.
    """

    def __init__(self, worker_urls: dict[str, str], checkpoint_path: str, device: str = "cuda:0"):
        import torch

        from open_fugu.models.worker_backend import OrchestratorBackbone, OrchestratorBackboneConfig

        state = torch.load(checkpoint_path, map_location=device)
        checkpoint_worker_ids = state["worker_ids"]
        missing = [w for w in checkpoint_worker_ids if w not in worker_urls]
        if missing:
            raise ValueError(
                f"Checkpoint at {checkpoint_path} expects worker(s) {missing} but --workers only "
                f"provided {list(worker_urls)} -- the selection head's output order/size is fixed "
                f"at training time (see worker_backend.OrchestratorBackboneConfig.worker_ids)."
            )

        self.worker_urls = worker_urls
        self.worker_ids = checkpoint_worker_ids
        config = OrchestratorBackboneConfig(
            worker_ids=checkpoint_worker_ids,
            backbone_model_id=state["backbone_model_id"],
            svf_n_last_layers=state["svf_n_last_layers"],
            device=device,
        )
        self.backbone = OrchestratorBackbone(config)
        self.backbone.selection_head.load_state_dict(state["selection_head"])
        for module, z in zip(self.backbone.svf_linears, state["svf_z"]):
            module.z.data = z.to(device=module.z.device, dtype=module.z.dtype)
        self.backbone.eval()

        self.ctx_state: dict[str, dict] = {}  # ctx_id -> {"color": str|None, "moves": [uci, ...]}

    async def dispatch(self, ctx_id: str, user_input: str, tool_provider: ToolProvider) -> str:
        import torch

        state = self.ctx_state.setdefault(ctx_id, {"color": None, "moves": []})
        parsed = parse_orchestrator_turn(user_input)
        if parsed["color"] and state["color"] is None:
            state["color"] = parsed["color"]
        state["moves"].extend(parsed["opening_moves"])
        state["moves"].extend(parsed["opponent_moves"])

        prompt = format_opening_prompt(state["color"] or "white", state["moves"])
        with torch.no_grad():
            logits = self.backbone.forward(prompt)
        short_id = self.worker_ids[int(torch.argmax(logits).item())]
        logger.info(f"[orchestrator/fugu] context {ctx_id}: ply {len(state['moves'])} "
                    f"routed to '{short_id}' (logits={[round(x, 3) for x in logits.tolist()]})")

        reply = await tool_provider.talk_to_agent(prompt, self.worker_urls[short_id], new_conversation=True)

        # Best-effort continuity tracking only (no board to validate legality
        # against here) -- if this mis-extracts, the green judge's own
        # board-backed extract_uci_move still catches an actually-illegal
        # move on the next turn; this only affects what WE think the history
        # is for future routing prompts.
        move_match = UCI_RE.search(reply)
        if move_match:
            state["moves"].append("".join(g for g in move_match.groups() if g).lower())
        return reply


class OrchestratorAgentExecutor(AgentExecutor):
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.tool_provider = ToolProvider()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        ctx_id = context.context_id or "default"

        reply = await self.dispatch.dispatch(ctx_id, user_input, self.tool_provider)

        await event_queue.enqueue_event(
            new_agent_text_message(reply, context_id=context.context_id)
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


def main():
    parser = argparse.ArgumentParser(description="Run the Open-Fugu orchestrator agent (purple, A2A)")
    parser.add_argument("--workers", required=True,
                         help="Comma-separated short_id=url pairs, e.g. qwen2.5-7b=http://127.0.0.1:9101,mistral-7b=http://127.0.0.1:9102")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9200)
    parser.add_argument("--card-url", default="")
    parser.add_argument("--router", choices=["random", "fugu"], default="random",
                         help="'random' (Phase 1, sticky-per-game) or 'fugu' (Phase 5+, per-query, "
                              "needs --checkpoint from Phase 4's SFT training)")
    parser.add_argument("--checkpoint", default="",
                         help="Path to Phase 4's checkpoint (checkpoints/phase4_sft/backbone_head_svf.pt) "
                              "-- required when --router fugu")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--seed", type=int, default=None, help="Only used by --router random")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING)

    worker_urls = {}
    for pair in args.workers.split(","):
        short_id, url = pair.split("=", 1)
        worker_urls[short_id.strip()] = url.strip()

    if args.router == "fugu":
        if not args.checkpoint:
            parser.error("--router fugu requires --checkpoint")
        dispatch = FuguSelectionDispatch(worker_urls, args.checkpoint, device=args.device)
    else:
        dispatch = RandomStickyDispatch(worker_urls, seed=args.seed)

    card_url = args.card_url or f"http://{args.host}:{args.port}"
    card = prepare_agent_card(card_url)
    executor = OrchestratorAgentExecutor(dispatch)

    request_handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore())
    app = A2AStarletteApplication(agent_card=card, http_handler=request_handler)

    logger.info(f"Serving orchestrator agent on {args.host}:{args.port}, "
                f"router={args.router}, workers={worker_urls}")
    uvicorn.run(app.build(), host=args.host, port=args.port, timeout_keep_alive=300)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""The Fugu orchestrator as an A2A purple agent: the green judge only ever
talks to THIS agent (never directly to a worker), matching Fugu's paper
architecture where a backbone+selection-head dispatches each query to one
worker in the swarm. This agent, in turn, is an A2A *client* to the worker
agents (worker_agent.py) -- i.e. inter-agent dispatch happens over A2A too,
not an in-process function call.

Phase 1 scope (per PLAN.md): "random-routing dummy orchestrator first" --
this picks ONE worker at random per game (per A2A context_id) and sticks with
it for that whole game, rather than the paper's real per-query learned
routing. Reason: worker agents keep their own conversation history
server-side, keyed by context_id: switching workers mid-game would silently
drop context (the new worker never saw earlier moves), unless the orchestrator
starts forwarding the FULL transcript on every call instead of relying on A2A
context threading -- that's real, deferred to Phase 4 alongside the actual
learned selection head (SVF + head), which is when per-query routing
decisions start meaning something.

Run standalone: python -m open_fugu.a2a.orchestrator_agent --workers qwen2.5-7b,mistral-7b --port 9200
"""
from __future__ import annotations

import argparse
import logging
import random

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

from open_fugu.agentbeats.tool_provider import ToolProvider

logger = logging.getLogger(__name__)


def prepare_agent_card(url: str) -> AgentCard:
    skill = AgentSkill(
        id="blindfold_chess_move",
        name="Blindfold chess move (Open-Fugu orchestrator)",
        description=(
            "Same interface as a single worker agent -- a running blindfold-chess "
            "conversation in, a move out -- but internally dispatches each game to "
            "a worker in the swarm (Phase 1: random routing; Phase 4+: learned "
            "selection head)."
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


class OrchestratorAgentExecutor(AgentExecutor):
    def __init__(self, worker_urls: dict[str, str], seed: int | None = None):
        self.worker_urls = worker_urls  # short_id -> url
        self.tool_provider = ToolProvider()
        self.ctx_id_to_worker: dict[str, str] = {}
        self.rng = random.Random(seed)

    def _pick_worker_for(self, ctx_id: str) -> str:
        if ctx_id not in self.ctx_id_to_worker:
            short_id = self.rng.choice(list(self.worker_urls))
            self.ctx_id_to_worker[ctx_id] = short_id
            logger.info(f"[orchestrator] new game (context {ctx_id}) routed to worker '{short_id}'")
        return self.ctx_id_to_worker[ctx_id]

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        ctx_id = context.context_id or "default"
        short_id = self._pick_worker_for(ctx_id)
        worker_url = self.worker_urls[short_id]

        # First call for this context_id -> fresh conversation on the
        # worker's side too; subsequent calls continue it (ToolProvider
        # tracks the worker-side context_id per URL -- see its docstring for
        # the known single-game-at-a-time limitation).
        new_conversation = ctx_id not in getattr(self, "_seen_contexts", set())
        if not hasattr(self, "_seen_contexts"):
            self._seen_contexts = set()
        self._seen_contexts.add(ctx_id)

        reply = await self.tool_provider.talk_to_agent(
            user_input, worker_url, new_conversation=new_conversation
        )

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
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING)

    worker_urls = {}
    for pair in args.workers.split(","):
        short_id, url = pair.split("=", 1)
        worker_urls[short_id.strip()] = url.strip()

    card_url = args.card_url or f"http://{args.host}:{args.port}"
    card = prepare_agent_card(card_url)
    executor = OrchestratorAgentExecutor(worker_urls, seed=args.seed)

    request_handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore())
    app = A2AStarletteApplication(agent_card=card, http_handler=request_handler)

    logger.info(f"Serving orchestrator agent on {args.host}:{args.port}, workers={worker_urls}")
    uvicorn.run(app.build(), host=args.host, port=args.port, timeout_keep_alive=300)


if __name__ == "__main__":
    main()

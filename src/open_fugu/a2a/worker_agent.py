#!/usr/bin/env python3
"""A2A purple agent wrapping one open-weight worker LLM (LocalWorker) as a
standalone AgentBeats-style server. Each candidate worker in the swarm
(Qwen2.5-7B, Mistral-7B, etc.) runs as its own process/port; the Fugu
orchestrator agent (orchestrator_agent.py) is the one that talks to these
over A2A, not the green judge directly -- the judge only ever talks to the
orchestrator (see PLAN.md's architecture section).

Run standalone: python -m open_fugu.a2a.worker_agent --worker qwen2.5-7b --port 9101
"""
from __future__ import annotations

import argparse
import logging

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

from open_fugu.models.local_worker import (
    CANDIDATE_WORKERS, LocalWorker, LocalWorkerConfig, scaled_max_new_tokens,
)

logger = logging.getLogger(__name__)


def prepare_agent_card(short_id: str, url: str) -> AgentCard:
    skill = AgentSkill(
        id="blindfold_chess_move",
        name="Blindfold chess move",
        description=(
            "Given a running blindfold-chess conversation (fixed opening, then only "
            "the opponent's last UCI move each turn -- no board/FEN ever shown), "
            "reply with the next move."
        ),
        tags=["chess", "blindfold", "game"],
        examples=[
            "You are playing a chess game and you are playing with white pieces. "
            "Current move history is 1. e2e4 e7e5 2. g1f3 b8c6 3. f1b5 a7a6. What is your move?"
        ],
    )
    return AgentCard(
        name=f"open_fugu_worker_{short_id}",
        description=f"Open-Fugu worker agent ({short_id}) -- a candidate LLM in the swarm.",
        url=url,
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(),
        skills=[skill],
    )


class WorkerAgentExecutor(AgentExecutor):
    """Maintains one running message transcript per A2A context_id, so a
    game's multi-turn conversation (opening prompt, then alternating
    opponent-move / our-move turns) accumulates correctly across separate
    A2A messages -- mirrors what harness.py does in-process, just keyed by
    context_id instead of a local list.
    """

    def __init__(self, worker: LocalWorker, max_new_tokens: int = 32, temperature: float = 0.4):
        self.worker = worker
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.ctx_id_to_messages: dict[str, list[dict]] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        ctx_id = context.context_id or "default"
        messages = self.ctx_id_to_messages.setdefault(ctx_id, [])
        messages.append({"role": "user", "content": user_input})

        reply = self.worker.generate(messages, max_new_tokens=self.max_new_tokens)
        messages.append({"role": "assistant", "content": reply})

        await event_queue.enqueue_event(
            new_agent_text_message(reply, context_id=context.context_id)
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


def main():
    parser = argparse.ArgumentParser(description="Run an Open-Fugu worker agent (purple, A2A)")
    parser.add_argument("--worker", required=True, choices=list(CANDIDATE_WORKERS),
                         help="Short id from CANDIDATE_WORKERS")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--card-url", default="", help="Override the URL advertised in the agent card")
    parser.add_argument("--max-new-tokens", type=int, default=32)
    parser.add_argument("--temperature", type=float, default=0.4)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING)

    card_url = args.card_url or f"http://{args.host}:{args.port}"
    card = prepare_agent_card(args.worker, card_url)

    logger.info(f"Loading worker model {CANDIDATE_WORKERS[args.worker]} ...")
    worker = LocalWorker(LocalWorkerConfig(model_id=CANDIDATE_WORKERS[args.worker]))
    # scaled_max_new_tokens: leaves --max-new-tokens's default/CLI value alone
    # for plain instruct workers, but gives reasoning-distill workers (see
    # local_worker.REASONING_WORKER_IDS) a much bigger budget so their
    # <think> block has room to finish before the final move.
    gen_budget = scaled_max_new_tokens(args.worker, args.max_new_tokens)
    executor = WorkerAgentExecutor(worker, max_new_tokens=gen_budget, temperature=args.temperature)

    request_handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore())
    app = A2AStarletteApplication(agent_card=card, http_handler=request_handler)

    logger.info(f"Serving worker agent '{args.worker}' on {args.host}:{args.port} (card url {card_url})")
    uvicorn.run(app.build(), host=args.host, port=args.port, timeout_keep_alive=300)


if __name__ == "__main__":
    main()

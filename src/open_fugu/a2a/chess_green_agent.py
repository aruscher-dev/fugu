#!/usr/bin/env python3
"""Blindfold-chess green (judge) agent, AgentBeats-style: receives an
EvalRequest naming one participant role ("fugu_orchestrator") by A2A URL,
plays N blindfold games against a local Stockfish, and returns an EvalResult.

The real chess.Board and Stockfish scoring live entirely in this process --
only the blindfold-legal text (opening move list, then each side's last UCI
move) ever crosses the A2A wire to the orchestrator, exactly matching
harness.py's in-process protocol. This is Phase 1's A2A-native replacement
for a bespoke FastAPI router server (see STATUS.md for the architecture
pivot rationale): the orchestrator purple agent is queried over A2A instead
of an in-process Python call.

Run standalone: python -m open_fugu.a2a.chess_green_agent --port 9009
"""
from __future__ import annotations

import argparse
import logging

import chess
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Part, TaskState, TextPart
from a2a.utils import new_agent_text_message

from open_fugu.agentbeats.green_executor import GreenAgent, GreenExecutor
from open_fugu.agentbeats.models import EvalRequest, EvalResult
from open_fugu.agentbeats.tool_provider import ToolProvider
from open_fugu.chess_blindfold.harness import play_blindfold_vs_engine_async
from open_fugu.reward.stockfish_scorer import StockfishScorer

logger = logging.getLogger(__name__)

DEFAULT_OPENING = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"]  # Ruy Lopez


class ChessBlindfoldGreenAgent(GreenAgent):
    def __init__(self):
        self._tool_provider = ToolProvider()

    def validate_request(self, request: EvalRequest) -> tuple[bool, str]:
        if "fugu_orchestrator" not in request.participants:
            return False, "config must name a 'fugu_orchestrator' participant"
        return True, "ok"

    async def run_eval(self, req: EvalRequest, updater: TaskUpdater) -> None:
        orchestrator_url = str(req.participants["fugu_orchestrator"])
        cfg = req.config
        n_games = int(cfg.get("n_games", 2))
        max_plies = int(cfg.get("max_plies", 24))
        skill_level = int(cfg.get("stockfish_skill_level", 1))
        opening = cfg.get("opening_uci_moves", DEFAULT_OPENING)

        games_summary = []
        try:
            for g in range(n_games):
                llm_color = chess.WHITE if g % 2 == 0 else chess.BLACK
                scorer = StockfishScorer(skill_level=skill_level, depth=8)
                first_call = True

                async def async_move_fn(messages: list[dict]) -> str:
                    nonlocal first_call
                    # Only the LATEST turn's text is new; the orchestrator
                    # (and, behind it, the worker agent) keeps its own running
                    # transcript server-side, keyed by A2A context_id -- same
                    # division of responsibility as harness.py's in-process
                    # `messages` list, just held on the other end of the wire.
                    latest_text = messages[-1]["content"]
                    reply = await self._tool_provider.talk_to_agent(
                        latest_text, orchestrator_url, new_conversation=first_call
                    )
                    first_call = False
                    return reply

                def engine_move_fn(board: chess.Board) -> str:
                    return scorer.best_move(board)

                await updater.update_status(
                    TaskState.working,
                    new_agent_text_message(f"Starting game {g} (LLM plays "
                                            f"{'white' if llm_color == chess.WHITE else 'black'})"),
                )
                try:
                    result = await play_blindfold_vs_engine_async(
                        async_move_fn=async_move_fn,
                        llm_color=llm_color,
                        opening_uci_moves=opening,
                        engine_best_move_fn=engine_move_fn,
                        scorer=scorer,
                        max_plies=max_plies,
                    )
                    legal_plies = [p for p in result.plies if p.legal]
                    losses = [p.centipawn_loss for p in legal_plies if p.centipawn_loss is not None]
                    games_summary.append({
                        "game_idx": g,
                        "llm_color": "white" if llm_color == chess.WHITE else "black",
                        "result": result.result,
                        "termination": result.termination,
                        "n_plies": len(result.plies),
                        "legal_move_rate": (len(legal_plies) / len(result.plies)) if result.plies else None,
                        "mean_acpl": (sum(losses) / len(losses)) if losses else None,
                        "blunder_rate": (sum(p.is_blunder for p in legal_plies) / len(legal_plies)) if legal_plies else None,
                        "final_fen": result.final_fen,
                    })
                except Exception as e:
                    games_summary.append({
                        "game_idx": g,
                        "llm_color": "white" if llm_color == chess.WHITE else "black",
                        "result": "*",
                        "termination": f"error: {type(e).__name__}: {e}",
                        "n_plies": 0,
                        "legal_move_rate": None,
                        "mean_acpl": None,
                        "blunder_rate": None,
                        "final_fen": None,
                    })
                finally:
                    scorer.close()

            legal_rates = [g["legal_move_rate"] for g in games_summary if g["legal_move_rate"] is not None]
            mean_legal_rate = sum(legal_rates) / len(legal_rates) if legal_rates else 0.0
            winner = "fugu_orchestrator" if mean_legal_rate > 0.5 else "stockfish"

            result = EvalResult(
                winner=winner,
                detail={"games": games_summary, "mean_legal_move_rate": mean_legal_rate},
            )
            await updater.add_artifact(
                parts=[Part(root=TextPart(text=result.model_dump_json()))],
                name="Result",
            )
        finally:
            self._tool_provider.reset()


def prepare_agent_card(url: str) -> AgentCard:
    skill = AgentSkill(
        id="blindfold_chess_eval",
        name="Blindfold chess evaluation",
        description="Judges a purple agent (the Fugu orchestrator) at blindfold chess vs. local Stockfish.",
        tags=["chess", "blindfold", "evaluation", "green-agent"],
        examples=["""
            {
              "participants": {"fugu_orchestrator": "http://127.0.0.1:9200"},
              "config": {"n_games": 2, "max_plies": 24, "stockfish_skill_level": 1}
            }"""],
    )
    return AgentCard(
        name="open_fugu_chess_judge",
        description="Green judge agent for Open-Fugu blindfold chess evaluation.",
        url=url,
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill],
    )


def main():
    parser = argparse.ArgumentParser(description="Run the Open-Fugu blindfold chess green (judge) agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9009)
    parser.add_argument("--card-url", default="")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING)

    card_url = args.card_url or f"http://{args.host}:{args.port}"
    card = prepare_agent_card(card_url)
    executor = GreenExecutor(ChessBlindfoldGreenAgent())

    request_handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore())
    app = A2AStarletteApplication(agent_card=card, http_handler=request_handler)

    logger.info(f"Serving chess green judge agent on {args.host}:{args.port}")
    uvicorn.run(app.build(), host=args.host, port=args.port, timeout_keep_alive=300)


if __name__ == "__main__":
    main()

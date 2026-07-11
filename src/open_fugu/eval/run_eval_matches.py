"""Phase 5 -- baseline + Open-Fugu blindfold matches (PLAN.md's phase table:
"5 conditions x ~25 games"). Plays real blindfold-chess games for each
condition through the A2A pipeline built in Phase 1/2 (worker purple agents,
orchestrator purple agent, blindfold-chess green judge) and Phase 5's own
`FuguSelectionDispatch` router (a2a/orchestrator_agent.py), then writes the
raw per-game results. Computing the Phase-6-style aggregate report (ACPL,
blunder rate, win-rate across conditions) is that later phase's job -- this
module only plays games and records what happened, same
collect-then-aggregate split Phase 3/4 already established.

The 5 conditions (PLAN.md's Architecture section: "Open-Fugu vs. each solo
worker vs. random-routing"):
  - One "solo" condition per default worker (green judge's `fugu_orchestrator`
    participant IS the worker agent directly -- both a worker agent and an
    orchestrator agent expose the exact same single-skill A2A interface, so
    no new green-judge code is needed for this reuse).
  - `random_routing`: Phase 1's dummy orchestrator (sticky per-game random
    pick) with the full worker pool available.
  - `open_fugu_sft`: Phase 5's FuguSelectionDispatch orchestrator, using
    Phase 4's trained checkpoint for real per-query routing.

Each condition starts only the agent processes it needs, waits for them to
report ready, sends one EvalRequest (n_games games) to a fresh green judge,
captures the resulting EvalResult, and tears everything down before the next
condition starts -- sequential, not parallel, per this project's "one
machine at a time" / single-GPU constraint (see ToolProvider's docstring on
why concurrent games aren't supported yet either).
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from a2a.client import A2ACardResolver
from a2a.types import DataPart, Message, TaskArtifactUpdateEvent, TaskStatusUpdateEvent, TextPart

from open_fugu.agentbeats.client import send_message
from open_fugu.agentbeats.models import EvalRequest
from open_fugu.models.local_worker import CANDIDATE_WORKERS

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
VENV_PYTHON = "/Data/.venv/bin/python"

DEFAULT_WORKERS = ["qwen2.5-7b", "mistral-7b", "deepseek-r1-distill-qwen-7b"]  # matches Phase 0.5/3/4's pool
DEFAULT_OPENING = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"]  # Ruy Lopez, same as Phase 0.5/1

WORKER_BASE_PORT = 9101
ORCHESTRATOR_PORT = 9200
GREEN_PORT = 9009


@dataclass
class Condition:
    condition_id: str
    kind: str  # "solo" | "orchestrator"
    worker: Optional[str] = None       # kind == "solo"
    router: Optional[str] = None       # kind == "orchestrator": "random" | "fugu"
    checkpoint: Optional[str] = None   # kind == "orchestrator", router == "fugu"


def default_conditions(workers: list, checkpoint_path: Optional[Path]) -> list:
    conditions = [Condition(condition_id=f"solo_{w}", kind="solo", worker=w) for w in workers]
    conditions.append(Condition(condition_id="random_routing", kind="orchestrator", router="random"))
    conditions.append(Condition(
        condition_id="open_fugu_sft", kind="orchestrator", router="fugu",
        checkpoint=str(checkpoint_path) if checkpoint_path else None,
    ))
    return conditions


def build_env() -> dict:
    env = os.environ.copy()
    env["HF_HOME"] = "/Data/.hf_cache"
    env["HF_HUB_DISABLE_XET"] = "1"
    env["PYTHONPATH"] = str(PROJECT_DIR / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def worker_port(short_id: str, workers: list) -> int:
    return WORKER_BASE_PORT + workers.index(short_id)


async def wait_for_all(urls: list, timeout: int = 600) -> bool:
    start = time.time()
    pending = set(urls)
    while time.time() - start < timeout and pending:
        for url in list(pending):
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    resolver = A2ACardResolver(httpx_client=client, base_url=url)
                    await resolver.get_agent_card()
                pending.discard(url)
            except Exception:
                pass
        if pending:
            await asyncio.sleep(2)
    return not pending


def start_worker(short_id: str, port: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [VENV_PYTHON, "-m", "open_fugu.a2a.worker_agent", "--worker", short_id,
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_DIR, env=env, start_new_session=True,
    )


def start_orchestrator(worker_urls: dict, router: str, checkpoint: Optional[str], env: dict) -> subprocess.Popen:
    workers_arg = ",".join(f"{k}={v}" for k, v in worker_urls.items())
    cmd = [VENV_PYTHON, "-m", "open_fugu.a2a.orchestrator_agent",
           "--workers", workers_arg, "--host", "127.0.0.1", "--port", str(ORCHESTRATOR_PORT),
           "--router", router]
    if router == "fugu":
        cmd += ["--checkpoint", checkpoint]
    return subprocess.Popen(cmd, cwd=PROJECT_DIR, env=env, start_new_session=True)


def start_green_judge(env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [VENV_PYTHON, "-m", "open_fugu.a2a.chess_green_agent", "--host", "127.0.0.1", "--port", str(GREEN_PORT)],
        cwd=PROJECT_DIR, env=env, start_new_session=True,
    )


def shutdown(procs: list) -> None:
    for p in procs:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    time.sleep(1)
    for p in procs:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@dataclass
class _Capture:
    """Mirrors agentbeats.client_cli's event_consumer, capturing the final
    EvalResult JSON instead of printing it -- same event-shape handling,
    just accumulate-don't-print, so a genuinely working reference (client_cli
    already used successfully in Phase 1) is reused almost verbatim rather
    than re-deriving A2A streaming semantics from scratch here."""
    data_parts: list = field(default_factory=list)

    def _absorb(self, parts) -> None:
        for part in parts:
            if isinstance(part.root, TextPart):
                try:
                    self.data_parts.append(json.loads(part.root.text))
                except Exception:
                    pass
            elif isinstance(part.root, DataPart):
                self.data_parts.append(part.root.data)

    async def consume(self, event, card) -> None:
        match event:
            case Message() as msg:
                self._absorb(msg.parts)
            case (task, TaskStatusUpdateEvent() as status_event):
                status = status_event.status
                if status.message:
                    self._absorb(status.message.parts)
            case (task, TaskArtifactUpdateEvent() as artifact_event):
                self._absorb(artifact_event.artifact.parts)
            case (task, None):
                if task.status.message:
                    self._absorb(task.status.message.parts)
            case _:
                pass


async def send_eval_request(green_url: str, participants: dict, config: dict) -> dict:
    """Sends one EvalRequest to the green judge and returns the EvalResult
    dict (the last data-shaped part observed -- the judge's final artifact,
    per green_executor.py's run_eval -> add_artifact contract)."""
    req = EvalRequest(participants=participants, config=config)
    capture = _Capture()
    await send_message(req.model_dump_json(), green_url, streaming=True, consumer=capture.consume)
    if not capture.data_parts:
        raise RuntimeError(f"No EvalResult artifact received from green judge at {green_url}")
    return capture.data_parts[-1]


# agentbeats/client.py's send_message() (vendored) hardcodes a 300s httpx
# timeout for the whole request -- fine for every OTHER caller here (a
# single worker turn, or Phase 1's 2-game/8-ply smoke test), but a real
# n_games=25 condition at a realistic max_plies can easily run well past
# that on 7-8B worker inference. Rather than edit the vendored constant,
# split each condition's games into small per-EvalRequest batches -- each
# request comfortably finishes under the timeout, and chess_green_agent's
# run_eval already resets all per-game/per-request state on every call (see
# its `finally: self._tool_provider.reset()`), so batching is behaviorally
# identical to one big request, just several smaller round-trips.
DEFAULT_GAMES_PER_REQUEST = 3


def run_condition(condition: Condition, workers: list, n_games: int, max_plies: int,
                   stockfish_skill_level: int, opening: list, timeout: int = 900,
                   games_per_request: int = DEFAULT_GAMES_PER_REQUEST) -> dict:
    """Starts exactly the processes `condition` needs, runs n_games blindfold
    games (in batches, see DEFAULT_GAMES_PER_REQUEST) through the green
    judge, tears everything down, and returns a merged EvalResult-shaped
    dict. Synchronous entry point (wraps its own asyncio.run) so
    scripts/phase5_baseline_and_fugu_matches.py can call it in a plain loop
    over conditions, same style as Phase 3/0.5's per-worker loops."""
    env = build_env()
    procs: list = []
    try:
        if condition.kind == "solo":
            port = worker_port(condition.worker, workers)
            procs.append(start_worker(condition.worker, port, env))
            orchestrator_role_url = f"http://127.0.0.1:{port}"
            ready_urls = [orchestrator_role_url]
        elif condition.kind == "orchestrator":
            worker_urls = {}
            for w in workers:
                port = worker_port(w, workers)
                procs.append(start_worker(w, port, env))
                worker_urls[w] = f"http://127.0.0.1:{port}"
            procs.append(start_orchestrator(worker_urls, condition.router, condition.checkpoint, env))
            orchestrator_role_url = f"http://127.0.0.1:{ORCHESTRATOR_PORT}"
            ready_urls = list(worker_urls.values()) + [orchestrator_role_url]
        else:
            raise ValueError(f"Unknown condition kind: {condition.kind}")

        green_proc = start_green_judge(env)
        procs.append(green_proc)
        green_url = f"http://127.0.0.1:{GREEN_PORT}"
        ready_urls.append(green_url)

        ready = asyncio.run(wait_for_all(ready_urls, timeout=timeout))
        if not ready:
            raise RuntimeError(f"condition '{condition.condition_id}': agents did not become ready "
                                f"in time ({ready_urls})")

        all_games: list = []
        remaining = n_games
        batch_idx = 0
        while remaining > 0:
            batch_n = min(games_per_request, remaining)
            batch_result = asyncio.run(send_eval_request(
                green_url,
                participants={"fugu_orchestrator": orchestrator_role_url},
                config={
                    "n_games": batch_n,
                    "max_plies": max_plies,
                    "stockfish_skill_level": stockfish_skill_level,
                    "opening_uci_moves": opening,
                },
            ))
            for g in batch_result.get("detail", {}).get("games", []):
                g["game_idx"] = batch_idx * games_per_request + g["game_idx"]
                all_games.append(g)
            remaining -= batch_n
            batch_idx += 1

        legal_rates = [g["legal_move_rate"] for g in all_games if g.get("legal_move_rate") is not None]
        mean_legal_rate = sum(legal_rates) / len(legal_rates) if legal_rates else 0.0
        return {
            "winner": "fugu_orchestrator" if mean_legal_rate > 0.5 else "stockfish",
            "detail": {"games": all_games, "mean_legal_move_rate": mean_legal_rate},
        }
    finally:
        shutdown(procs)


def unknown_workers(workers: list) -> list:
    return [w for w in workers if w not in CANDIDATE_WORKERS]

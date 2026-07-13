#!/usr/bin/env python3
"""M3 smoke test: prove the A2A/AgentBeats pipeline (worker purple, Fugu
orchestrator purple random-routing dummy, blindfold-chess green judge) works
end to end on the 5x5 Gardner Minichess board too, not just full chess --
the minichess track's equivalent of scripts/phase1_agentbeats_smoke_test.py.
Short and cheap on purpose (2 short games): this gates "does the wiring
work on this board size", not "is Open-Fugu any good at Gardner Minichess"
(m2's floor check already covers chess-quality, and currently reports
REVIEW_NEEDED -- see reports/m2_gardner_floor_check_summary.json and
STATUS.md's note on this).

Same worker-as-prerequisite-process structure as Phase 1's smoke test: the
worker agent isn't declared in the scenario TOML's [[participants]] (only
agents the green judge itself talks to go there -- just the orchestrator);
this script starts the worker first, then hands off to
open_fugu.agentbeats.run_scenario for the orchestrator+green+client_cli part.

Needs bin/fairy-stockfish (gitignored, host-specific -- see
scripts/m0_setup_gardner_engine.sh) in addition to everything Phase 1's
smoke test needs.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from a2a.client import A2ACardResolver

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCENARIO = PROJECT_DIR / "config" / "scenario_gardner_minichess_smoke.toml"
REPORTS_DIR = PROJECT_DIR / "reports"  # tracked (not gitignored) -- small summaries
VENV_PYTHON = "/Data/.venv/bin/python"
WORKER_URL = "http://127.0.0.1:9111"


def build_env() -> dict:
    env = os.environ.copy()
    env["HF_HOME"] = "/Data/.hf_cache"
    env["HF_HUB_DISABLE_XET"] = "1"
    env["PYTHONPATH"] = str(PROJECT_DIR / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


async def wait_for(url: str, timeout: int = 300) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resolver = A2ACardResolver(httpx_client=client, base_url=url)
                await resolver.get_agent_card()
                return True
        except Exception:
            await asyncio.sleep(2)
    return False


def main():
    env = build_env()
    print(f"Starting worker agent at {WORKER_URL} (loading model, this can take a while) ...")
    worker_proc = subprocess.Popen(
        [VENV_PYTHON, "-m", "open_fugu.a2a.worker_agent", "--worker", "qwen2.5-7b",
         "--host", "127.0.0.1", "--port", "9111"],
        cwd=PROJECT_DIR, env=env, start_new_session=True,
    )

    try:
        ready = asyncio.run(wait_for(WORKER_URL, timeout=600))
        if not ready:
            print("ERROR: worker agent did not become ready in time.")
            sys.exit(1)
        print("Worker agent ready. Launching orchestrator + green judge (variant=gardner) + client...")

        result = subprocess.run(
            [VENV_PYTHON, "-m", "open_fugu.agentbeats.run_scenario", str(SCENARIO), "--show-logs"],
            cwd=PROJECT_DIR, env=env,
        )

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        REPORTS_DIR.chmod(0o700)
        marker = REPORTS_DIR / "m3_gardner_smoke_test_result.json"
        marker.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "returncode": result.returncode,
            "passed": result.returncode == 0,
            "note": ("returncode==0 means the A2A pipeline (worker -> orchestrator -> "
                     "green judge -> EvalResult) ran to completion without crashing on "
                     "the 5x5 Gardner Minichess board -- this gates wiring, not chess "
                     "quality/legal-move rate (see reports/m2_gardner_floor_check_summary.json "
                     "for that, currently REVIEW_NEEDED)."),
        }, indent=2))
        marker.chmod(0o600)

        sys.exit(result.returncode)
    finally:
        print("Shutting down worker agent...")
        if worker_proc.poll() is None:
            try:
                os.killpg(worker_proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(1)
            if worker_proc.poll() is None:
                try:
                    os.killpg(worker_proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    main()

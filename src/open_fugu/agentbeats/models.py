"""Vendored from the AgentBeats tutorial SDK (MIT license, Copyright 2025
AgentBeats), src/agentbeats/models.py, found locally at
~/Team/AgentBeats_bench/finance_economics/tutorial-agent-beats-comp. Not
published on PyPI, hence vendored rather than pip-installed -- this is the
same approach every AgentBeats competition submission in that course material
takes (each repo carries its own copy of this small helper package).
"""
from typing import Any
from pydantic import BaseModel, HttpUrl


class EvalRequest(BaseModel):
    participants: dict[str, HttpUrl]  # role -> agent endpoint
    config: dict[str, Any]


class EvalResult(BaseModel):
    winner: str  # role of winner (or "draw" / "none")
    detail: dict[str, Any]

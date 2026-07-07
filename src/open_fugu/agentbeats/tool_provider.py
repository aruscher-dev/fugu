"""Vendored (near-verbatim) from the AgentBeats tutorial SDK (MIT license,
Copyright 2025 AgentBeats), src/agentbeats/tool_provider.py.
"""
from open_fugu.agentbeats.client import send_message


class ToolProvider:
    def __init__(self):
        self._context_ids = {}

    async def talk_to_agent(self, message: str, url: str, new_conversation: bool = False):
        """Communicate with another agent by sending a message and receiving
        their response.

        Note (Open-Fugu specific): context continuity is tracked per URL, not
        per logical game -- fine for the sequential (one game at a time)
        matches this project runs, but two concurrent games routed to the
        same worker URL would collide. Not a concern until Phase 5+ considers
        parallelizing eval matches.
        """
        outputs = await send_message(
            message=message,
            base_url=url,
            context_id=None if new_conversation else self._context_ids.get(url, None),
        )
        if outputs.get("status", "completed") != "completed":
            raise RuntimeError(f"{url} responded with: {outputs}")
        self._context_ids[url] = outputs.get("context_id", None)
        return outputs["response"]

    def reset(self):
        self._context_ids = {}

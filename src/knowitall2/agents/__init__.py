"""Agent adapters: register KnowItAll2 with a coding agent, check it, and remove it."""

from __future__ import annotations

from pathlib import Path

from .base import AgentAdapter, AgentError, Check, ServerLaunch, describe_checks, probe_server, server_launch
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

ADAPTERS: dict[str, type[AgentAdapter]] = {
    CodexAdapter.name: CodexAdapter,
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
}
AGENT_NAMES = tuple(ADAPTERS)


def adapter_for(name: str, *, user_home: Path | None = None) -> AgentAdapter:
    try:
        adapter_type = ADAPTERS[name]
    except KeyError:
        raise AgentError(f"Unknown agent {name!r}; supported agents: {', '.join(AGENT_NAMES)}.") from None
    return adapter_type(user_home=user_home)


__all__ = [
    "ADAPTERS",
    "AGENT_NAMES",
    "AgentAdapter",
    "AgentError",
    "Check",
    "ServerLaunch",
    "adapter_for",
    "describe_checks",
    "probe_server",
    "server_launch",
]

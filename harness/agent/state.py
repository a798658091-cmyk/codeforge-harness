from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentState:
    """Mutable state owned by one agent run."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def merge_usage(self, usage: dict[str, int]) -> None:
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)


def reduce_state(state: AgentState, event: dict[str, Any]) -> AgentState:
    """Small reducer used by persistence and deterministic tests."""

    event_type = event.get("type")
    if event_type == "message":
        state.append(event["message"])
    elif event_type == "step":
        state.steps += 1
    elif event_type == "tool_result":
        state.tool_calls += 1
        if not event.get("success", False):
            state.tool_failures += 1
    elif event_type == "usage":
        state.merge_usage(event.get("usage", {}))
    else:
        raise ValueError(f"unknown state event: {event_type}")
    return state

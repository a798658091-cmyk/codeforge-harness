from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import Any

from harness.providers.base import AssistantTurn, ModelProvider

MockResponder = Callable[
    [list[dict[str, Any]], list[dict[str, Any]], int],
    AssistantTurn,
]


class MockProvider(ModelProvider):
    """Deterministic provider for integration tests and offline demos."""

    def __init__(
        self,
        responses: Iterable[AssistantTurn] | None = None,
        responder: MockResponder | None = None,
    ) -> None:
        if responses is None and responder is None:
            raise ValueError("responses or responder is required")
        self._responses = deque(responses or [])
        self._responder = responder
        self.requests: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        call_index = len(self.requests)
        self.requests.append(
            {"messages": deepcopy(messages), "tools": deepcopy(tools)}
        )
        if self._responder is not None:
            return self._responder(messages, tools, call_index)
        if not self._responses:
            raise RuntimeError("MockProvider has no scripted response left")
        return self._responses.popleft()

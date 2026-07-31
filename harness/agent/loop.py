from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from harness.agent.state import AgentState, reduce_state
from harness.providers.base import AssistantTurn, ModelProvider
from harness.tools.registry import ToolRegistry


class AgentLoopLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentRunResult:
    answer: str
    state: AgentState


class AgentLoop:
    """A native model -> tool -> result loop with no graph framework."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        registry: ToolRegistry,
        system_prompt: str,
        max_steps: int = 20,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.provider = provider
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_steps = max_steps

    def run(
        self,
        prompt: str,
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> AgentRunResult:
        state = AgentState(messages=list(history or []))
        if not state.messages or state.messages[0].get("role") != "system":
            state.messages.insert(
                0, {"role": "system", "content": self.system_prompt}
            )
        reduce_state(
            state,
            {"type": "message", "message": {"role": "user", "content": prompt}},
        )

        for _ in range(self.max_steps):
            reduce_state(state, {"type": "step"})
            turn = self.provider.complete(
                state.messages,
                self.registry.schemas(),
            )
            reduce_state(state, {"type": "usage", "usage": turn.usage})
            self._append_assistant_turn(state, turn)

            if not turn.tool_calls:
                return AgentRunResult(answer=turn.content, state=state)

            for call in turn.tool_calls:
                result = self.registry.dispatch(call.name, call.arguments)
                reduce_state(
                    state,
                    {
                        "type": "tool_result",
                        "success": result.success,
                    },
                )
                content = result.content
                if not result.success:
                    content = f"[tool_error:{result.error_type}] {content}"
                reduce_state(
                    state,
                    {
                        "type": "message",
                        "message": {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": content,
                        },
                    },
                )

        raise AgentLoopLimitError(
            f"agent exceeded max_steps={self.max_steps}"
        )

    @staticmethod
    def _append_assistant_turn(
        state: AgentState,
        turn: AssistantTurn,
    ) -> None:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": turn.content,
        }
        if turn.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                        ),
                    },
                }
                for call in turn.tool_calls
            ]
        reduce_state(
            state,
            {"type": "message", "message": message},
        )

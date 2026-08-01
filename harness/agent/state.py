"""定义一次 Agent 运行的内存状态以及统一的事件 reducer。

任务流位置：由 Agent Loop 在每次消息、模型回合、工具结果和用量变化时调用，
为后续持久化与恢复能力提供稳定的状态更新入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentState:
    """归属于一次 Agent 运行、可随事件持续更新的状态。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def append(self, message: dict[str, Any]) -> None:
        """向当前运行状态追加一条对话消息。"""

        self.messages.append(message)

    def merge_usage(self, usage: dict[str, int]) -> None:
        """把一轮模型调用的 Token 用量累加到状态。"""

        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)


def reduce_state(state: AgentState, event: dict[str, Any]) -> AgentState:
    """根据事件集中更新状态，供执行循环、持久化和确定性测试复用。"""

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

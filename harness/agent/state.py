"""定义一次 Agent 运行的内存状态以及统一的事件 reducer。

任务流位置：由 Agent Loop 在每次消息、模型回合、工具结果和用量变化时调用，
并为 SQLite 检查点、Todo 恢复和上下文压缩提供稳定的状态更新入口。
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
    todos: list[dict[str, str]] = field(default_factory=list)
    compactions: int = 0

    def append(self, message: dict[str, Any]) -> None:
        """向当前运行状态追加一条对话消息。"""

        self.messages.append(message)

    def merge_usage(self, usage: dict[str, int]) -> None:
        """把一轮模型调用的 Token 用量累加到状态。"""

        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)

    def to_dict(self) -> dict[str, Any]:
        """转换为可由 JSON 与 SQLite 安全保存的普通字典。"""

        return {
            "messages": self.messages,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "todos": self.todos,
            "compactions": self.compactions,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentState":
        """从数据库中的普通字典恢复状态，并兼容缺少新字段的旧检查点。"""

        return cls(
            messages=list(value.get("messages", [])),
            steps=int(value.get("steps", 0)),
            tool_calls=int(value.get("tool_calls", 0)),
            tool_failures=int(value.get("tool_failures", 0)),
            prompt_tokens=int(value.get("prompt_tokens", 0)),
            completion_tokens=int(value.get("completion_tokens", 0)),
            todos=list(value.get("todos", [])),
            compactions=int(value.get("compactions", 0)),
        )


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
    elif event_type == "todos":
        state.todos = list(event.get("todos", []))
    elif event_type == "compaction":
        state.messages = list(event["messages"])
        state.compactions += 1
    else:
        raise ValueError(f"unknown state event: {event_type}")
    return state

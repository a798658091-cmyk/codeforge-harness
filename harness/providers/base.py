"""定义与模型厂商无关的 Provider 接口和标准化回合数据结构。

任务流位置：Agent Loop 只依赖这里的 ModelProvider、AssistantTurn 和 ToolCall，
真实或模拟 Provider 都在进入执行循环前转换成这套统一协议。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """模型请求调用工具时使用的厂商无关数据结构。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AssistantTurn:
    """经过标准化、可由 Agent Loop 直接消费的一轮模型输出。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)


class ModelProvider(ABC):
    """原生 Agent Loop 所依赖的最小模型调用接口。"""

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        """根据消息和工具 schema 生成一轮标准化模型输出。"""

        raise NotImplementedError

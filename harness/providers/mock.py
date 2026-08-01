"""提供可脚本化、可重复执行的离线 Mock 模型实现。

任务流位置：测试时替代真实 Provider 接入 Agent Loop，按照预设响应生成工具调用
和最终答案，从而验证“模型 → 工具 → 结果 → 模型”闭环而不访问网络。
"""

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
    """供集成测试和离线演示使用的确定性 Provider。"""

    def __init__(
        self,
        responses: Iterable[AssistantTurn] | None = None,
        responder: MockResponder | None = None,
    ) -> None:
        """使用固定响应序列或动态响应函数初始化 Mock Provider。"""

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
        """记录请求并返回下一条预设或动态生成的模型回合。"""

        call_index = len(self.requests)
        self.requests.append(
            {"messages": deepcopy(messages), "tools": deepcopy(tools)}
        )
        if self._responder is not None:
            return self._responder(messages, tools, call_index)
        if not self._responses:
            raise RuntimeError("MockProvider has no scripted response left")
        return self._responses.popleft()

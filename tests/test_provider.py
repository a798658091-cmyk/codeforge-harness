"""使用假 OpenAI 客户端验证真实 Provider 适配器的响应标准化逻辑。

任务流位置：隔离外部网络，只测试“厂商响应 → OpenAICompatibleProvider →
AssistantTurn/ToolCall”的转换，是 Agent Loop 上游协议的单元测试。
"""

from __future__ import annotations

from types import SimpleNamespace

from harness.providers.openai_compatible import OpenAICompatibleProvider


class FakeCompletions:
    """模拟 OpenAI SDK 的 chat.completions 资源。"""

    def __init__(self) -> None:
        """初始化用于保存最后一次请求参数的槽位。"""

        self.request = None

    def create(self, **kwargs):
        """保存请求并返回包含工具调用和用量的假响应。"""

        self.request = kwargs
        tool_call = SimpleNamespace(
            id="call-7",
            function=SimpleNamespace(
                name="read_file",
                arguments='{"path": "README.md"}',
            ),
        )
        message = SimpleNamespace(content=None, tool_calls=[tool_call])
        choice = SimpleNamespace(
            message=message,
            finish_reason="tool_calls",
        )
        usage = SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=3,
            total_tokens=14,
        )
        return SimpleNamespace(choices=[choice], usage=usage)


def test_openai_compatible_provider_normalizes_response() -> None:
    """验证适配器正确转换工具调用、用量并设置请求参数。"""

    completions = FakeCompletions()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    provider = OpenAICompatibleProvider(
        model="test-model",
        api_key="",
        client=client,
    )

    turn = provider.complete(
        [{"role": "user", "content": "read"}],
        [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"path": "README.md"}
    assert turn.usage["total_tokens"] == 14
    assert completions.request["model"] == "test-model"
    assert completions.request["tool_choice"] == "auto"

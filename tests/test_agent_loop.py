"""验证 Mock 模型驱动的原生 Agent Loop、错误回灌和步数上限。

任务流位置：覆盖“Agent Loop → Mock Provider → Tool Registry → tool 消息 →
下一轮模型”的核心闭环，是 Day 1 控制流的主要集成测试。
"""

from pathlib import Path

import pytest

from harness.agent.loop import AgentLoop, AgentLoopLimitError
from harness.providers.base import AssistantTurn, ToolCall
from harness.providers.mock import MockProvider
from harness.safety.permissions import PermissionPolicy
from harness.tools import build_default_registry


def test_mock_llm_agent_loop_executes_tool_and_returns_answer(
    workspace: Path,
) -> None:
    """验证 Mock 模型调用读取工具后能够进入下一轮并返回答案。"""

    provider = MockProvider(
        responses=[
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "src/sample.py"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            AssistantTurn(
                content="The function returns a greeting.",
                finish_reason="stop",
                usage={"prompt_tokens": 20, "completion_tokens": 6},
            ),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=build_default_registry(workspace),
        system_prompt="You are a test agent.",
    )

    result = loop.run("Inspect the greeting function")

    assert result.answer == "The function returns a greeting."
    assert result.state.steps == 2
    assert result.state.tool_calls == 1
    assert result.state.tool_failures == 0
    assert result.state.prompt_tokens == 20
    second_request_messages = provider.requests[1]["messages"]
    assert second_request_messages[-1]["role"] == "tool"
    assert "def greet" in second_request_messages[-1]["content"]
    assert second_request_messages[-1]["tool_call_id"] == "call-1"


def test_tool_validation_failure_is_fed_back_to_model(
    workspace: Path,
) -> None:
    """验证工具参数错误会作为 tool_error 回灌并允许模型恢复。"""

    def responder(messages, tools, call_index):
        """首轮生成非法调用，次轮检查错误消息并返回恢复答案。"""

        if call_index == 0:
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="bad-call",
                        name="read_file",
                        arguments={},
                    )
                ]
            )
        assert messages[-1]["role"] == "tool"
        assert "[tool_error:validation_error]" in messages[-1]["content"]
        return AssistantTurn(content="I recovered from the invalid call.")

    provider = MockProvider(responder=responder)
    loop = AgentLoop(
        provider=provider,
        registry=build_default_registry(workspace),
        system_prompt="test",
    )

    result = loop.run("Use a malformed tool call")

    assert result.answer == "I recovered from the invalid call."
    assert result.state.tool_failures == 1


def test_agent_loop_has_a_hard_step_limit(workspace: Path) -> None:
    """验证持续请求工具时 Agent Loop 会在硬步数上限停止。"""

    provider = MockProvider(
        responder=lambda messages, tools, call_index: AssistantTurn(
            tool_calls=[
                ToolCall(
                    id=f"call-{call_index}",
                    name="search",
                    arguments={"query": "missing"},
                )
            ]
        )
    )
    loop = AgentLoop(
        provider=provider,
        registry=build_default_registry(workspace),
        system_prompt="test",
        max_steps=2,
    )

    with pytest.raises(AgentLoopLimitError):
        loop.run("never stop")


def test_permission_denial_is_fed_back_to_model(workspace: Path) -> None:
    """验证权限拒绝会作为工具错误回灌，模型随后仍可完成回答。"""

    def responder(messages, tools, call_index):
        """首轮请求被禁止的读取，次轮检查拒绝消息并完成回答。"""

        if call_index == 0:
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="denied-call",
                        name="read_file",
                        arguments={"path": "src/sample.py"},
                    )
                ]
            )
        assert messages[-1]["role"] == "tool"
        assert "[tool_error:permission_denied]" in messages[-1]["content"]
        return AssistantTurn(content="The read was denied safely.")

    loop = AgentLoop(
        provider=MockProvider(responder=responder),
        registry=build_default_registry(
            workspace,
            permission_policy=PermissionPolicy(
                {"read_file": "deny"},
                default="allow",
            ),
        ),
        system_prompt="test",
    )

    result = loop.run("Try a denied read")

    assert result.answer == "The read was denied safely."
    assert result.state.tool_calls == 1
    assert result.state.tool_failures == 1

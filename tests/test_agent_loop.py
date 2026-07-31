from pathlib import Path

import pytest

from harness.agent.loop import AgentLoop, AgentLoopLimitError
from harness.providers.base import AssistantTurn, ToolCall
from harness.providers.mock import MockProvider
from harness.tools import build_default_registry


def test_mock_llm_agent_loop_executes_tool_and_returns_answer(
    workspace: Path,
) -> None:
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
    def responder(messages, tools, call_index):
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

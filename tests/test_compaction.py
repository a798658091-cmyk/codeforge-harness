"""验证确定性上下文压缩的触发、摘要和 Agent Loop 接线。

任务流位置：构造超长消息历史，在 Provider 调用前确认旧消息被摘要、近期消息及
工具调用链保留，并确认压缩次数进入 AgentState。
"""

from pathlib import Path

from harness.agent.loop import AgentLoop
from harness.agent.state import AgentState
from harness.context.compaction import SUMMARY_PREFIX, ContextCompactor
from harness.providers.base import AssistantTurn
from harness.providers.mock import MockProvider
from harness.tools import build_default_registry


def test_compactor_replaces_old_messages_with_bounded_summary() -> None:
    """验证超过消息阈值时保留系统消息和最近历史。"""

    messages = [{"role": "system", "content": "rules"}]
    messages.extend(
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
        for index in range(10)
    )
    compactor = ContextCompactor(
        max_messages=6,
        keep_recent=3,
        max_characters=10000,
    )

    result = compactor.compact(messages)

    assert result.compacted is True
    assert result.messages[0]["content"] == "rules"
    assert result.messages[1]["content"].startswith(SUMMARY_PREFIX)
    assert [message["content"] for message in result.messages[-3:]] == [
        "m7",
        "m8",
        "m9",
    ]


def test_compactor_does_not_leave_orphan_tool_message() -> None:
    """验证切分点落在 tool 消息时会连同其 assistant 调用一起保留。"""

    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "name": "read_file", "content": "result"},
        {"role": "assistant", "content": "recent answer"},
        {"role": "user", "content": "recent question"},
    ]
    compactor = ContextCompactor(
        max_messages=4,
        keep_recent=3,
        max_characters=10000,
    )

    result = compactor.compact(messages)

    summary_index = 1
    assert result.messages[summary_index + 1]["role"] == "assistant"
    assert result.messages[summary_index + 2]["role"] == "tool"


def test_agent_loop_compacts_restored_history_before_provider(
    workspace: Path,
) -> None:
    """验证恢复的长历史在真实 Provider 边界前先被压缩。"""

    initial = AgentState(
        messages=[
            {"role": "system", "content": "rules"},
            *[
                {"role": "user", "content": f"old-{index}"}
                for index in range(8)
            ],
        ]
    )

    def responder(messages, tools, call_index):
        """断言 Provider 只看到压缩后的历史。"""

        assert any(
            str(message.get("content", "")).startswith(SUMMARY_PREFIX)
            for message in messages
        )
        assert len(messages) < 10
        return AssistantTurn(content="done")

    loop = AgentLoop(
        provider=MockProvider(responder=responder),
        registry=build_default_registry(workspace),
        system_prompt="rules",
        compactor=ContextCompactor(
            max_messages=6,
            keep_recent=3,
            max_characters=10000,
        ),
    )

    result = loop.run("new", initial_state=initial)

    assert result.state.compactions == 1

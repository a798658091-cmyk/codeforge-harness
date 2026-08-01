"""验证 AgentState reducer 对消息、步数、工具指标和 Token 用量的归并。

任务流位置：直接测试 Agent Loop 每轮都会调用的状态更新层，确保未来持久化和
恢复功能可以依赖稳定、可预测的事件语义。
"""

from harness.agent.state import AgentState, reduce_state


def test_reducer_merges_messages_usage_and_tool_metrics() -> None:
    """验证 reducer 能合并消息、步数、Token 和工具失败指标。"""

    state = AgentState()
    reduce_state(
        state,
        {"type": "message", "message": {"role": "user", "content": "fix"}},
    )
    reduce_state(state, {"type": "step"})
    reduce_state(
        state,
        {
            "type": "usage",
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        },
    )
    reduce_state(state, {"type": "tool_result", "success": False})

    assert state.messages == [{"role": "user", "content": "fix"}]
    assert state.steps == 1
    assert state.tool_calls == 1
    assert state.tool_failures == 1
    assert state.prompt_tokens == 10
    assert state.completion_tokens == 4


def test_reducer_rejects_unknown_events() -> None:
    """验证 reducer 会拒绝未定义的状态事件类型。"""

    state = AgentState()

    try:
        reduce_state(state, {"type": "not-real"})
    except ValueError as exc:
        assert "unknown state event" in str(exc)
    else:
        raise AssertionError("unknown event was accepted")

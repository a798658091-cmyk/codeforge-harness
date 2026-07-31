from harness.agent.state import AgentState, reduce_state


def test_reducer_merges_messages_usage_and_tool_metrics() -> None:
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
    state = AgentState()

    try:
        reduce_state(state, {"type": "not-real"})
    except ValueError as exc:
        assert "unknown state event" in str(exc)
    else:
        raise AssertionError("unknown event was accepted")

"""验证最小 MessageBus 的线程安全历史、过滤与观察者隔离语义。

任务流位置：Subagent 和 Worktree 发布生命周期事件后，主 Agent 可通过同一总线
查询；本测试确保坏观察者不会阻断任务，且历史不会无限增长。
"""

from harness.delegation.message_bus import MessageBus


def test_message_bus_keeps_bounded_filterable_history() -> None:
    """验证有界历史、来源过滤和订阅异常隔离。"""

    bus = MessageBus(max_events=2)
    received: list[str] = []
    bus.subscribe(lambda event: received.append(event.event_type))
    bus.subscribe(lambda event: 1 / 0)

    bus.publish("subagent.queued", "subagent:a")
    bus.publish("subagent.running", "subagent:a")
    bus.publish("subagent.completed", "subagent:b")

    assert received == [
        "subagent.queued",
        "subagent.running",
        "subagent.completed",
    ]
    assert [
        event.event_type for event in bus.history(limit=10)
    ] == ["subagent.running", "subagent.completed"]
    assert [
        event.event_type
        for event in bus.history(limit=10, source="subagent:a")
    ] == ["subagent.running"]

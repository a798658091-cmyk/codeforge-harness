"""提供进程内、线程安全且可查询的最小 MessageBus。

任务流位置：主 Agent、可写 Subagent 与 WorktreeManager 把生命周期事件发布到
这里；状态工具和终端展示再按事件类型或来源读取历史，从而让并发过程可观察。
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import Field

from harness.tools.base import BaseTool, ToolArguments, ToolContext, ToolError


@dataclass(frozen=True)
class MessageEvent:
    """描述一个不可变的协作事件及其来源和结构化数据。"""

    id: str
    event_type: str
    source: str
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        """返回适合 JSON 序列化和工具输出的独立字典。"""

        return asdict(self)


Subscriber = Callable[[MessageEvent], None]


class MessageBus:
    """在当前进程中保存有界事件历史并通知订阅者。"""

    def __init__(self, *, max_events: int = 1000) -> None:
        """设置最多保留的事件数，并初始化并发保护。"""

        if max_events < 1:
            raise ValueError("max_events must be at least 1")
        self._events: deque[MessageEvent] = deque(maxlen=max_events)
        self._subscribers: dict[str, Subscriber] = {}
        self._lock = threading.RLock()

    def publish(
        self,
        event_type: str,
        source: str,
        payload: dict[str, Any] | None = None,
    ) -> MessageEvent:
        """记录事件，并在锁外通知订阅者以避免回调互相阻塞。"""

        event = MessageEvent(
            id=uuid.uuid4().hex[:12],
            event_type=event_type,
            source=source,
            payload=dict(payload or {}),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._events.append(event)
            subscribers = list(self._subscribers.values())
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:
                # 观察者不能破坏实际任务；需要可靠投递时再升级为持久化队列。
                continue
        return event

    def subscribe(self, subscriber: Subscriber) -> str:
        """注册进程内观察者并返回可用于取消订阅的标识。"""

        subscription_id = uuid.uuid4().hex
        with self._lock:
            self._subscribers[subscription_id] = subscriber
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """移除一个观察者，并说明该标识此前是否存在。"""

        with self._lock:
            return self._subscribers.pop(subscription_id, None) is not None

    def history(
        self,
        *,
        limit: int = 50,
        event_type: str | None = None,
        source: str | None = None,
    ) -> list[MessageEvent]:
        """按可选条件返回从旧到新排列的最近事件快照。"""

        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._lock:
            matched = [
                event
                for event in self._events
                if (event_type is None or event.event_type == event_type)
                and (source is None or event.source == source)
            ]
        return matched[-limit:]


class MessageBusEventsArguments(ToolArguments):
    """定义事件查询可使用的条数、类型和来源过滤器。"""

    limit: int = Field(default=20, ge=1, le=200)
    event_type: str | None = Field(default=None, min_length=1, max_length=100)
    source: str | None = Field(default=None, min_length=1, max_length=100)


class MessageBusEventsTool(BaseTool):
    """让主 Agent 查询 Subagent 和 Worktree 的近期协作事件。"""

    name: ClassVar[str] = "message_bus_events"
    description: ClassVar[str] = (
        "Read recent in-process subagent and worktree lifecycle events."
    )
    arguments_model: ClassVar[type[ToolArguments]] = MessageBusEventsArguments

    def __init__(self, bus: MessageBus | None) -> None:
        """绑定可选 MessageBus；CLI 未启用团队能力时保持工具可解释失败。"""

        self.bus = bus

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """过滤事件历史并返回 JSON。"""

        if not isinstance(arguments, MessageBusEventsArguments):
            raise TypeError("message_bus_events received unexpected arguments")
        if self.bus is None:
            raise ToolError("message bus is disabled")
        events = self.bus.history(
            limit=arguments.limit,
            event_type=arguments.event_type,
            source=arguments.source,
        )
        return json.dumps(
            [event.to_dict() for event in events],
            ensure_ascii=False,
            indent=2,
        )


__all__ = [
    "MessageBus",
    "MessageBusEventsTool",
    "MessageEvent",
]

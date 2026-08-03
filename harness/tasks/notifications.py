"""实现进程内通知中心及供 Agent 查询和确认的工具。

任务流位置：后台 Shell 在完成、失败、超时或取消时向 NotificationCenter 发布
事件；Agent 可通过工具读取/确认，CLI 退出前也会展示仍未读取的通知。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import ClassVar, Literal

from pydantic import Field

from harness.tools.base import BaseTool, ToolArguments, ToolContext, ToolError


@dataclass(frozen=True)
class Notification:
    """描述一条带级别、来源和已读状态的运行时通知。"""

    id: str
    level: Literal["info", "success", "warning", "error"]
    title: str
    message: str
    source: str
    created_at: str
    read: bool = False


class NotificationCenter:
    """以线程安全方式保存当前 Harness 进程产生的通知。"""

    def __init__(self) -> None:
        """创建空通知集合和可重入锁。"""

        self._notifications: list[Notification] = []
        self._lock = threading.RLock()

    def emit(
        self,
        *,
        level: Literal["info", "success", "warning", "error"],
        title: str,
        message: str,
        source: str,
    ) -> Notification:
        """创建一条通知并追加到当前进程队列。"""

        notification = Notification(
            id=uuid.uuid4().hex,
            level=level,
            title=title.strip(),
            message=message.strip(),
            source=source.strip(),
            created_at=datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            ),
        )
        with self._lock:
            self._notifications.append(notification)
        return notification

    def list(
        self,
        *,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[Notification]:
        """按时间正序返回通知快照，可只查看未读项。"""

        if not 1 <= limit <= 200:
            raise ValueError("notification limit must be between 1 and 200")
        with self._lock:
            selected = [
                notification
                for notification in self._notifications
                if not unread_only or not notification.read
            ]
            return list(selected[-limit:])

    def acknowledge(self, notification_id: str) -> Notification | None:
        """把指定通知标记为已读，不存在时返回 None。"""

        with self._lock:
            for index, notification in enumerate(self._notifications):
                if notification.id != notification_id:
                    continue
                acknowledged = replace(notification, read=True)
                self._notifications[index] = acknowledged
                return acknowledged
        return None


class NotificationListArguments(ToolArguments):
    """定义通知查询的未读过滤和数量限制。"""

    unread_only: bool = False
    limit: int = Field(default=50, ge=1, le=200)


class NotificationAcknowledgeArguments(ToolArguments):
    """定义确认通知所需的通知 ID。"""

    notification_id: str = Field(min_length=1, max_length=80)


class NotificationListTool(BaseTool):
    """让 Agent 查询后台任务产生的通知。"""

    name: ClassVar[str] = "notification_list"
    description: ClassVar[str] = (
        "List runtime notifications, optionally only unread ones."
    )
    arguments_model: ClassVar[type[ToolArguments]] = NotificationListArguments

    def __init__(self, center: NotificationCenter | None) -> None:
        """绑定可选的进程内通知中心。"""

        self.center = center

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """查询通知并返回 JSON 数组。"""

        if not isinstance(arguments, NotificationListArguments):
            raise TypeError("notification_list received unexpected arguments")
        center = _require_center(self.center)
        notifications = center.list(
            unread_only=arguments.unread_only,
            limit=arguments.limit,
        )
        return json.dumps(
            [_notification_to_dict(item) for item in notifications],
            ensure_ascii=False,
            indent=2,
        )


class NotificationAcknowledgeTool(BaseTool):
    """让 Agent 把一条运行时通知标记为已读。"""

    name: ClassVar[str] = "notification_ack"
    description: ClassVar[str] = (
        "Mark one runtime notification as read by notification_id."
    )
    arguments_model: ClassVar[type[ToolArguments]] = (
        NotificationAcknowledgeArguments
    )

    def __init__(self, center: NotificationCenter | None) -> None:
        """绑定可选的进程内通知中心。"""

        self.center = center

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """确认指定通知并返回更新后的 JSON。"""

        if not isinstance(arguments, NotificationAcknowledgeArguments):
            raise TypeError("notification_ack received unexpected arguments")
        notification = _require_center(self.center).acknowledge(
            arguments.notification_id
        )
        if notification is None:
            raise ToolError(
                f"notification not found: {arguments.notification_id}"
            )
        return json.dumps(
            _notification_to_dict(notification),
            ensure_ascii=False,
            indent=2,
        )


def _require_center(center: NotificationCenter | None) -> NotificationCenter:
    """取得已配置通知中心，缺失时返回工具错误。"""

    if center is None:
        raise ToolError("notifications are disabled")
    return center


def _notification_to_dict(notification: Notification) -> dict[str, object]:
    """把通知转换为工具返回所需的 JSON 字典。"""

    return {
        "id": notification.id,
        "level": notification.level,
        "title": notification.title,
        "message": notification.message,
        "source": notification.source,
        "created_at": notification.created_at,
        "read": notification.read,
    }


__all__ = [
    "Notification",
    "NotificationAcknowledgeTool",
    "NotificationCenter",
    "NotificationListTool",
]

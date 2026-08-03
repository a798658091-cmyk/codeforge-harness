"""实现 Agent 可调用的 Todo 清单及读写工具。

任务流位置：Tool Registry 注册 todo_read 与 todo_write；模型用它们维护当前任务
计划，Agent Loop 再把清单同步到 AgentState，随 SQLite 检查点一起保存和恢复。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, ClassVar

from pydantic import Field

from harness.tasks.models import TodoItem, TodoStatus
from harness.tools.base import BaseTool, ToolArguments, ToolContext, ToolError


class TodoList:
    """维护一份按顺序排列且 ID 唯一的内存待办清单。"""

    def __init__(self, items: Iterable[TodoItem | dict[str, Any]] = ()) -> None:
        """使用可选的已保存数据初始化清单。"""

        self._items: list[TodoItem] = []
        self.replace(items)

    def replace(
        self,
        items: Iterable[TodoItem | dict[str, Any]],
    ) -> None:
        """原子替换完整清单，并拒绝重复 ID 和多个进行中任务。"""

        normalized = [
            item if isinstance(item, TodoItem) else TodoItem.model_validate(item)
            for item in items
        ]
        ids = [item.id for item in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("todo item ids must be unique")
        active_count = sum(
            item.status is TodoStatus.IN_PROGRESS for item in normalized
        )
        if active_count > 1:
            raise ValueError("only one todo item may be in_progress")
        self._items = [item.model_copy(deep=True) for item in normalized]

    def snapshot(self) -> list[dict[str, str]]:
        """返回适合写入 AgentState 和 JSON 的独立清单快照。"""

        return [
            item.model_dump(mode="json")
            for item in self._items
        ]

    def render(self) -> str:
        """把当前清单渲染为供模型读取的紧凑 JSON。"""

        return json.dumps(self.snapshot(), ensure_ascii=False, indent=2)


class TodoReadArguments(ToolArguments):
    """定义 todo_read 的空参数对象。"""

    pass


class TodoWriteItem(ToolArguments):
    """定义模型写入 Todo 工具时使用的单项参数。"""

    id: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=2000)
    status: TodoStatus = TodoStatus.PENDING


class TodoWriteArguments(ToolArguments):
    """定义原子覆盖完整 Todo 清单的工具参数。"""

    items: list[TodoWriteItem] = Field(max_length=100)


class TodoReadTool(BaseTool):
    """向模型返回当前任务清单。"""

    name: ClassVar[str] = "todo_read"
    description: ClassVar[str] = (
        "Read the current task checklist and each item's status."
    )
    arguments_model: ClassVar[type[ToolArguments]] = TodoReadArguments

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """读取 ToolContext 中共享的 TodoList。"""

        todo_list = _require_todo_list(context)
        return todo_list.render()


class TodoWriteTool(BaseTool):
    """让模型以完整快照方式创建或更新任务清单。"""

    name: ClassVar[str] = "todo_write"
    description: ClassVar[str] = (
        "Replace the current task checklist. Keep IDs stable, mark at most "
        "one item in_progress, and retain unfinished items."
    )
    arguments_model: ClassVar[type[ToolArguments]] = TodoWriteArguments

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """校验并原子替换共享 TodoList 的全部内容。"""

        if not isinstance(arguments, TodoWriteArguments):
            raise TypeError("todo_write received unexpected arguments")
        todo_list = _require_todo_list(context)
        todo_list.replace(
            [item.model_dump(mode="python") for item in arguments.items]
        )
        return todo_list.render()


def _require_todo_list(context: ToolContext) -> TodoList:
    """取得工具上下文中的 TodoList，缺失时返回清晰的工具错误。"""

    if not isinstance(context.todo_list, TodoList):
        raise ToolError("todo state is not configured")
    return context.todo_list


__all__ = ["TodoList", "TodoReadTool", "TodoWriteTool"]

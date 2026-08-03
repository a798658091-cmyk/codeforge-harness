"""定义 Todo 子系统使用的状态枚举和数据模型。

任务流位置：模型通过 Todo 工具提交任务清单时先由这里的 Pydantic 模型校验，
随后 TodoList 更新内存状态，并由 AgentState 与 SQLite 检查点持久化。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TodoStatus(str, Enum):
    """表示单个待办事项所处的三个稳定阶段。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class TodoItem(BaseModel):
    """描述一个可序列化、可在检查点中恢复的待办事项。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=2000)
    status: TodoStatus = TodoStatus.PENDING

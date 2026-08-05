"""导出可写 Subagent 使用的 Git Worktree 隔离能力。

任务流位置：CLI 创建 WorktreeManager 后交给团队管理器；模型本身不直接执行
底层 Git 命令，只能通过受权限控制的 Subagent 管理工具使用它。
"""

from harness.worktrees.manager import (
    WorktreeConflictError,
    WorktreeError,
    WorktreeManager,
    WorktreeRecord,
)

__all__ = [
    "WorktreeConflictError",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeRecord",
]

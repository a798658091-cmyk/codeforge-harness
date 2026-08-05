"""组装并导出 CodeForge 默认工具注册表。

任务流位置：CLI 和测试在启动 Agent Loop 前调用 build_default_registry，把安全
Workspace、代码工具、Todo、Skills、Memory、委派和后台工具注入注册表统一分发。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from harness.context.skills import (
    ListSkillsTool,
    ReadSkillTool,
    SkillRegistry,
)
from harness.context.memory import (
    MemoryDeleteTool,
    MemorySearchTool,
    MemoryStore,
    MemoryWriteTool,
)
from harness.safety.audit import AuditLogger
from harness.safety.hooks import HookManager
from harness.safety.permissions import (
    ApprovalHandler,
    PermissionPolicy,
    build_default_permission_policy,
)
from harness.safety.workspace import Workspace
from harness.tasks.todo import TodoList, TodoReadTool, TodoWriteTool
from harness.tasks.background import (
    BackgroundCancelTool,
    BackgroundJobManager,
    BackgroundOutputTool,
    BackgroundStartTool,
    BackgroundStatusTool,
)
from harness.tasks.notifications import (
    NotificationAcknowledgeTool,
    NotificationCenter,
    NotificationListTool,
)
from harness.tools.base import ToolContext
from harness.tools.filesystem import (
    EditFileTool,
    ReadFileTool,
    WriteFileTool,
)
from harness.tools.patch import ApplyPatchTool
from harness.tools.registry import ToolRegistry
from harness.tools.search import SearchTool
from harness.tools.shell import ShellTool
from harness.tools.testing import RunTestsTool

if TYPE_CHECKING:
    from harness.delegation.message_bus import MessageBus
    from harness.delegation.subagent import ReadonlySubagentRunner
    from harness.delegation.team import WritableSubagentManager


def build_default_registry(
    workspace: str | Path,
    *,
    permission_policy: PermissionPolicy | None = None,
    approval_handler: ApprovalHandler | None = None,
    hooks: HookManager | None = None,
    audit_logger: AuditLogger | None = None,
    todo_list: TodoList | None = None,
    skill_registry: SkillRegistry | None = None,
    memory_store: MemoryStore | None = None,
    subagent_runner: ReadonlySubagentRunner | None = None,
    writable_subagents: WritableSubagentManager | None = None,
    message_bus: MessageBus | None = None,
    background_manager: BackgroundJobManager | None = None,
    notification_center: NotificationCenter | None = None,
) -> ToolRegistry:
    """为 workspace 创建带可选安全组件的默认工具注册表。"""

    # 延迟导入避免 AgentLoop → tools.registry → tools → Subagent → AgentLoop 循环。
    from harness.delegation.message_bus import MessageBusEventsTool
    from harness.delegation.subagent import DelegateReadonlyTool
    from harness.delegation.team import (
        SubagentCancelTool,
        SubagentDiffTool,
        SubagentIntegrateTool,
        SubagentListTool,
        SubagentSpawnTool,
        SubagentStatusTool,
    )

    context = ToolContext(
        workspace=Workspace(Path(workspace)),
        todo_list=todo_list or TodoList(),
    )
    skills = skill_registry or SkillRegistry()
    return ToolRegistry(
        context,
        tools=[
            ReadFileTool(),
            WriteFileTool(),
            EditFileTool(),
            SearchTool(),
            ApplyPatchTool(),
            ShellTool(),
            RunTestsTool(),
            TodoReadTool(),
            TodoWriteTool(),
            ListSkillsTool(skills),
            ReadSkillTool(skills),
            MemorySearchTool(memory_store),
            MemoryWriteTool(memory_store),
            MemoryDeleteTool(memory_store),
            DelegateReadonlyTool(subagent_runner),
            SubagentSpawnTool(writable_subagents),
            SubagentStatusTool(writable_subagents),
            SubagentListTool(writable_subagents),
            SubagentCancelTool(writable_subagents),
            SubagentDiffTool(writable_subagents),
            SubagentIntegrateTool(writable_subagents),
            MessageBusEventsTool(message_bus),
            BackgroundStartTool(background_manager),
            BackgroundStatusTool(background_manager),
            BackgroundOutputTool(background_manager),
            BackgroundCancelTool(background_manager),
            NotificationListTool(notification_center),
            NotificationAcknowledgeTool(notification_center),
        ],
        permission_policy=(
            permission_policy or build_default_permission_policy()
        ),
        approval_handler=approval_handler,
        hooks=hooks,
        audit_logger=audit_logger,
    )


__all__ = ["ToolRegistry", "build_default_registry"]

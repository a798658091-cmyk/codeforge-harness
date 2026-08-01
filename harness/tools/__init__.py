"""组装并导出 CodeForge Day 1 的默认工具注册表。

任务流位置：CLI 和测试在启动 Agent Loop 前调用 build_default_registry，把安全
Workspace 与七种工具注入 Tool Registry，供模型产生工具调用后统一分发。
"""

from __future__ import annotations

from pathlib import Path

from harness.safety.audit import AuditLogger
from harness.safety.hooks import HookManager
from harness.safety.permissions import (
    ApprovalHandler,
    PermissionPolicy,
    build_default_permission_policy,
)
from harness.safety.workspace import Workspace
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


def build_default_registry(
    workspace: str | Path,
    *,
    permission_policy: PermissionPolicy | None = None,
    approval_handler: ApprovalHandler | None = None,
    hooks: HookManager | None = None,
    audit_logger: AuditLogger | None = None,
) -> ToolRegistry:
    """为 workspace 创建带可选安全组件的默认工具注册表。"""

    context = ToolContext(workspace=Workspace(Path(workspace)))
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
        ],
        permission_policy=(
            permission_policy or build_default_permission_policy()
        ),
        approval_handler=approval_handler,
        hooks=hooks,
        audit_logger=audit_logger,
    )


__all__ = ["ToolRegistry", "build_default_registry"]

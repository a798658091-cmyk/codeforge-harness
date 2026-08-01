"""汇总 workspace 沙箱、权限、Hooks 和审计安全组件。

任务流位置：位于 Tool Registry 与具体文件/命令工具之间，对工具调用统一提供
路径隔离、执行授权、生命周期扩展点和持久化审计能力。
"""

from harness.safety.audit import AuditLogger, AuditLogError
from harness.safety.hooks import (
    HookExecutionError,
    HookManager,
    PostToolUseEvent,
    PreToolUseEvent,
    PreToolUseRejected,
    PreToolUseResult,
)
from harness.safety.permissions import (
    DEFAULT_PERMISSION_RULES,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionResult,
    build_default_permission_policy,
    parse_permission_rules,
)
from harness.safety.workspace import Workspace, WorkspaceViolation

__all__ = [
    "AuditLogger",
    "AuditLogError",
    "DEFAULT_PERMISSION_RULES",
    "HookExecutionError",
    "HookManager",
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionRequest",
    "PermissionResult",
    "PostToolUseEvent",
    "PreToolUseEvent",
    "PreToolUseRejected",
    "PreToolUseResult",
    "Workspace",
    "WorkspaceViolation",
    "build_default_permission_policy",
    "parse_permission_rules",
]

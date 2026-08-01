"""集中完成工具注册、schema 导出、参数校验、执行和错误隔离。

任务流位置：Agent Loop 收到模型的 ToolCall 后进入本模块；注册表验证参数并
调用具体 workspace 工具，再把结构化结果交还 Agent Loop 回灌模型。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any, Iterable

from pydantic import ValidationError

from harness.safety.audit import AuditLogError, AuditLogger
from harness.safety.hooks import (
    HookExecutionError,
    HookManager,
    PostToolUseEvent,
    PreToolUseRejected,
)
from harness.safety.permissions import (
    ApprovalHandler,
    PermissionPolicy,
    PermissionResult,
)
from harness.tools.base import (
    BaseTool,
    ToolContext,
    ToolError,
    ToolOutput,
)


@dataclass(frozen=True)
class ToolExecutionResult:
    """封装一次工具分发的结果、耗时和错误类型。"""

    tool_name: str
    success: bool
    content: str
    duration_ms: float
    error_type: str | None = None
    permission_decision: str | None = None
    permission_granted: bool | None = None
    hook_errors: tuple[str, ...] = ()
    audit_error: str | None = None


class ToolRegistry:
    """让所有工具调用都通过同一个窄入口接受校验和分发。"""

    def __init__(
        self,
        context: ToolContext,
        tools: Iterable[BaseTool] | None = None,
        *,
        permission_policy: PermissionPolicy | None = None,
        approval_handler: ApprovalHandler | None = None,
        hooks: HookManager | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        """绑定工具、安全组件和初始工具集合。"""

        self.context = context
        self.permission_policy = permission_policy or PermissionPolicy()
        self.approval_handler = approval_handler
        self.hooks = hooks or HookManager()
        self.audit_logger = audit_logger
        self._tools: dict[str, BaseTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        """注册一个工具，并拒绝重复的工具名称。"""

        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """按名称查找工具，未注册时返回 None。"""

        return self._tools.get(name)

    def names(self) -> list[str]:
        """按注册顺序返回所有工具名称。"""

        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """导出全部已注册工具的 OpenAI function schemas。"""

        return [tool.openai_schema() for tool in self._tools.values()]

    def dispatch(
        self,
        name: str,
        raw_arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        """查找工具、校验原始参数、执行并隔离所有错误。"""

        started = perf_counter()
        tool = self._tools.get(name)
        if tool is None:
            return self._finish(
                self._result(
                    name=name,
                    started=started,
                    success=False,
                    content=f"unknown tool: {name}",
                    error_type="unknown_tool",
                ),
                arguments=raw_arguments,
            )
        try:
            arguments = tool.arguments_model.model_validate(raw_arguments)
        except ValidationError as exc:
            return self._finish(
                self._result(
                    name=name,
                    started=started,
                    success=False,
                    content=exc.json(),
                    error_type="validation_error",
                ),
                arguments=raw_arguments,
            )

        validated_arguments = arguments.model_dump(mode="python")
        permission = self.permission_policy.authorize(
            name,
            validated_arguments,
            self.approval_handler,
        )
        if not permission.allowed:
            return self._finish(
                self._result(
                    name=name,
                    started=started,
                    success=False,
                    content=f"tool permission denied: {permission.reason}",
                    error_type="permission_denied",
                    permission=permission,
                ),
                arguments=validated_arguments,
                permission_reason=permission.reason,
            )

        try:
            pre_result = self.hooks.run_pre_tool_use(
                name,
                validated_arguments,
                self.context.workspace.root,
            )
        except PreToolUseRejected as exc:
            reason = str(exc) or "rejected by PreToolUse hook"
            return self._finish(
                self._result(
                    name=name,
                    started=started,
                    success=False,
                    content=f"PreToolUse hook rejected tool call: {reason}",
                    error_type="pre_hook_rejected",
                    permission=permission,
                ),
                arguments=validated_arguments,
                permission_reason=permission.reason,
            )
        except HookExecutionError as exc:
            return self._finish(
                self._result(
                    name=name,
                    started=started,
                    success=False,
                    content=str(exc),
                    error_type="pre_hook_error",
                    permission=permission,
                ),
                arguments=validated_arguments,
                permission_reason=permission.reason,
            )
        if not pre_result.allow:
            reason = pre_result.reason or "rejected by PreToolUse hook"
            return self._finish(
                self._result(
                    name=name,
                    started=started,
                    success=False,
                    content=f"PreToolUse hook rejected tool call: {reason}",
                    error_type="pre_hook_rejected",
                    permission=permission,
                ),
                arguments=validated_arguments,
                permission_reason=permission.reason,
            )

        try:
            outcome = tool.execute(arguments, self.context)
            if isinstance(outcome, ToolOutput):
                result = self._result(
                    name=name,
                    started=started,
                    success=outcome.success,
                    content=outcome.content,
                    error_type=None if outcome.success else "tool_error",
                    permission=permission,
                )
            else:
                result = self._result(
                    name=name,
                    started=started,
                    success=True,
                    content=str(outcome),
                    permission=permission,
                )
        except (ToolError, OSError, ValueError) as exc:
            result = self._result(
                name=name,
                started=started,
                success=False,
                content=str(exc),
                error_type=type(exc).__name__,
                permission=permission,
            )
        except Exception as exc:  # 隔离工具内部缺陷，避免击穿模型循环
            result = self._result(
                name=name,
                started=started,
                success=False,
                content=f"unexpected tool failure: {type(exc).__name__}: {exc}",
                error_type="internal_error",
                permission=permission,
            )
        return self._finish(
            result,
            arguments=validated_arguments,
            permission_reason=permission.reason,
            run_post_hook=True,
        )

    @staticmethod
    def _result(
        *,
        name: str,
        started: float,
        success: bool,
        content: str,
        error_type: str | None = None,
        permission: PermissionResult | None = None,
    ) -> ToolExecutionResult:
        """根据起始时间构造统一的工具执行结果。"""

        return ToolExecutionResult(
            tool_name=name,
            success=success,
            content=content,
            duration_ms=round((perf_counter() - started) * 1000, 3),
            error_type=error_type,
            permission_decision=(
                permission.decision.value if permission is not None else None
            ),
            permission_granted=(
                permission.allowed if permission is not None else None
            ),
        )

    def _finish(
        self,
        result: ToolExecutionResult,
        *,
        arguments: Any,
        permission_reason: str | None = None,
        run_post_hook: bool = False,
    ) -> ToolExecutionResult:
        """运行后置 Hook、写入审计，并返回附带诊断信息的结果。"""

        if run_post_hook:
            hook_errors = self.hooks.run_post_tool_use(
                PostToolUseEvent(
                    tool_name=result.tool_name,
                    arguments=dict(arguments),
                    workspace=self.context.workspace.root,
                    success=result.success,
                    content=result.content,
                    error_type=result.error_type,
                    duration_ms=result.duration_ms,
                )
            )
            if hook_errors:
                result = replace(result, hook_errors=hook_errors)

        if self.audit_logger is not None:
            try:
                self.audit_logger.record_tool_call(
                    tool_name=result.tool_name,
                    arguments=arguments,
                    permission_decision=result.permission_decision,
                    permission_granted=result.permission_granted,
                    permission_reason=permission_reason,
                    success=result.success,
                    duration_ms=result.duration_ms,
                    error_type=result.error_type,
                    content=result.content,
                    hook_errors=result.hook_errors,
                )
            except AuditLogError as exc:
                result = replace(result, audit_error=str(exc))
        return result

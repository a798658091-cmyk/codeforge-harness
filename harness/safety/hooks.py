
"""实现可注册的 PreToolUse 与 PostToolUse Hook 执行链。

任务流位置：权限允许工具调用后，Tool Registry 先运行 PreToolUse；具体工具
执行完毕后再运行 PostToolUse。前置 Hook 失败时关闭执行，后置 Hook 失败则记录
错误但不掩盖已经发生的工具结果。
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class HookError(RuntimeError):
    """所有 Hook 管线错误的基类。"""


class PreToolUseRejected(HookError):
    """前置 Hook 主动阻止工具调用时抛出的异常。"""


class HookExecutionError(HookError):
    """前置 Hook 意外失败时由 HookManager 包装的异常。"""


@dataclass(frozen=True)
class PreToolUseEvent:
    """提供给 PreToolUse Hook 的只读工具调用快照。"""

    tool_name: str
    arguments: dict[str, Any]
    workspace: Path


@dataclass(frozen=True)
class PreToolUseResult:
    """表示前置 Hook 是否允许工具继续执行。"""

    allow: bool = True
    reason: str = ""


@dataclass(frozen=True)
class PostToolUseEvent:
    """提供给 PostToolUse Hook 的工具调用和执行结果快照。"""

    tool_name: str
    arguments: dict[str, Any]
    workspace: Path
    success: bool
    content: str
    error_type: str | None
    duration_ms: float


PreToolUseHook = Callable[[PreToolUseEvent], PreToolUseResult | None]
PostToolUseHook = Callable[[PostToolUseEvent], None]


class HookManager:
    """按注册顺序管理和运行前置、后置工具 Hook。"""

    def __init__(
        self,
        *,
        pre_tool_use: list[PreToolUseHook] | None = None,
        post_tool_use: list[PostToolUseHook] | None = None,
    ) -> None:
        """使用可选的初始 Hook 列表创建管理器。"""

        self._pre_tool_use = list(pre_tool_use or [])
        self._post_tool_use = list(post_tool_use or [])

    def register_pre_tool_use(self, hook: PreToolUseHook) -> None:
        """把一个 PreToolUse Hook 追加到执行链。"""

        self._pre_tool_use.append(hook)

    def register_post_tool_use(self, hook: PostToolUseHook) -> None:
        """把一个 PostToolUse Hook 追加到执行链。"""

        self._post_tool_use.append(hook)

    def run_pre_tool_use(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        workspace: Path,
    ) -> PreToolUseResult:
        """运行前置 Hook，并在拒绝或异常时立即停止执行链。"""

        event = PreToolUseEvent(
            tool_name=tool_name,
            arguments=deepcopy(arguments),
            workspace=workspace,
        )
        for hook in self._pre_tool_use:
            try:
                result = hook(event)
            except PreToolUseRejected:
                raise
            except Exception as exc:
                raise HookExecutionError(
                    f"PreToolUse hook {self._hook_name(hook)} failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if result is not None and not result.allow:
                return result
        return PreToolUseResult()

    def run_post_tool_use(
        self,
        event: PostToolUseEvent,
    ) -> tuple[str, ...]:
        """运行所有后置 Hook，并以文本形式收集各 Hook 的异常。"""

        errors: list[str] = []
        for hook in self._post_tool_use:
            try:
                hook(event)
            except Exception as exc:
                errors.append(
                    f"PostToolUse hook {self._hook_name(hook)} failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        return tuple(errors)

    @staticmethod
    def _hook_name(hook: Callable[..., Any]) -> str:
        """返回便于诊断的 Hook 函数或可调用对象名称。"""

        return getattr(hook, "__name__", type(hook).__name__)


"""实现工具调用的 allow、ask、deny 权限决策与规则解析。

任务流位置：Tool Registry 完成 Pydantic 参数校验后、运行 PreToolUse Hook 和
具体工具之前调用本模块；拒绝结果会直接作为结构化工具错误回灌 Agent Loop。
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PermissionDecision(str, Enum):
    """表示工具权限规则支持的三种决策。"""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


DEFAULT_PERMISSION_RULES = {
    "read_file": PermissionDecision.ALLOW,
    "search": PermissionDecision.ALLOW,
}


@dataclass(frozen=True)
class PermissionRequest:
    """传递给人工审批函数的工具调用信息。"""

    tool_name: str
    arguments: dict[str, Any]
    decision: PermissionDecision


@dataclass(frozen=True)
class PermissionResult:
    """描述权限规则和人工审批共同产生的最终授权结果。"""

    decision: PermissionDecision
    allowed: bool
    reason: str
    approved: bool | None = None


ApprovalHandler = Callable[[PermissionRequest], bool]


class PermissionPolicy:
    """按照精确名称或 Glob 规则决定每种工具的权限。"""

    def __init__(
        self,
        rules: Mapping[str, PermissionDecision | str] | None = None,
        *,
        default: PermissionDecision | str = PermissionDecision.ALLOW,
    ) -> None:
        """保存有序权限规则和没有匹配时使用的默认决策。"""

        self.default = PermissionDecision(default)
        self.rules = {
            pattern: PermissionDecision(decision)
            for pattern, decision in (rules or {}).items()
        }

    def decision_for(self, tool_name: str) -> PermissionDecision:
        """优先匹配精确工具名，再按定义顺序匹配 Glob 规则。"""

        if tool_name in self.rules:
            return self.rules[tool_name]
        for pattern, decision in self.rules.items():
            if fnmatch.fnmatchcase(tool_name, pattern):
                return decision
        return self.default

    def authorize(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        approval_handler: ApprovalHandler | None = None,
    ) -> PermissionResult:
        """应用规则，并在 ask 决策时调用审批函数得到最终结果。"""

        decision = self.decision_for(tool_name)
        if decision is PermissionDecision.ALLOW:
            return PermissionResult(
                decision=decision,
                allowed=True,
                reason="allowed by policy",
            )
        if decision is PermissionDecision.DENY:
            return PermissionResult(
                decision=decision,
                allowed=False,
                reason="denied by policy",
            )
        if approval_handler is None:
            return PermissionResult(
                decision=decision,
                allowed=False,
                reason=(
                    "approval required but no approval handler is configured"
                ),
            )

        request = PermissionRequest(
            tool_name=tool_name,
            arguments=dict(arguments),
            decision=decision,
        )
        try:
            approved = bool(approval_handler(request))
        except (EOFError, KeyboardInterrupt):
            approved = False
        except Exception as exc:
            return PermissionResult(
                decision=decision,
                allowed=False,
                approved=False,
                reason=(
                    "approval handler failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        return PermissionResult(
            decision=decision,
            allowed=approved,
            approved=approved,
            reason="approved by user" if approved else "approval declined",
        )


def build_default_permission_policy() -> PermissionPolicy:
    """创建只读工具允许、其余工具询问的默认本地策略。"""

    return PermissionPolicy(
        DEFAULT_PERMISSION_RULES,
        default=PermissionDecision.ASK,
    )


def parse_permission_rules(
    value: str | None,
) -> dict[str, PermissionDecision]:
    """解析逗号分隔的 ``工具模式=决策`` 权限规则。"""

    rules: dict[str, PermissionDecision] = {}
    if not value or not value.strip():
        return rules
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                f"invalid permission rule {item!r}; "
                "expected TOOL=allow|ask|deny"
            )
        pattern, raw_decision = (part.strip() for part in item.split("=", 1))
        if not pattern:
            raise ValueError("permission rule tool pattern cannot be empty")
        try:
            rules[pattern] = PermissionDecision(raw_decision.lower())
        except ValueError as exc:
            raise ValueError(
                f"invalid permission decision for {pattern!r}: "
                f"{raw_decision!r}"
            ) from exc
    return rules

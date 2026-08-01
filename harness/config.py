"""集中加载模型、workspace、请求限制、权限和审计配置。

任务流位置：CLI 解析完显式参数后调用本模块；生成的 Settings 用于构造真实
Provider、Tool Registry 安全边界和 Agent Loop 的运行环境。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from harness.safety.permissions import (
    DEFAULT_PERMISSION_RULES,
    PermissionDecision,
    parse_permission_rules,
)
from harness.safety.workspace import Workspace, WorkspaceViolation


def _normalize_permission_rules(
    value: str | Mapping[str, PermissionDecision | str] | None,
) -> dict[str, PermissionDecision]:
    """把环境变量文本或调用方映射统一转换为权限决策字典。"""

    if isinstance(value, str) or value is None:
        return parse_permission_rules(value)
    return {
        pattern: PermissionDecision(decision)
        for pattern, decision in value.items()
    }


def _resolve_audit_log(
    workspace: Path,
    value: str | Path | None,
    *,
    disabled: bool,
) -> Path | None:
    """解析 workspace 内的审计路径，并处理显式关闭配置。"""

    if disabled:
        return None
    raw_value = value if value is not None else ".codeforge/audit.jsonl"
    if str(raw_value).strip().lower() in {"", "off", "none", "false"}:
        return None
    try:
        return Workspace(workspace).resolve(raw_value)
    except (WorkspaceViolation, FileNotFoundError, NotADirectoryError) as exc:
        raise ConfigurationError(f"invalid audit log path: {exc}") from exc

class ConfigurationError(ValueError):
    """配置缺失或配置值不可用时抛出的异常。"""

    pass


@dataclass(frozen=True)
class Settings:
    """保存一次 CodeForge 运行所需的已解析配置。"""

    workspace: Path
    model: str
    api_key: str
    base_url: str | None
    max_tokens: int = 4096
    request_timeout: float = 60.0
    permission_default: PermissionDecision = PermissionDecision.ASK
    permission_rules: dict[str, PermissionDecision] = field(
        default_factory=lambda: dict(DEFAULT_PERMISSION_RULES)
    )
    audit_log: Path | None = None

    @classmethod
    def from_env(
        cls,
        *,
        workspace: str | Path = ".",
        model: str | None = None,
        base_url: str | None = None,
        permission_default: PermissionDecision | str | None = None,
        permission_rules: (
            str | Mapping[str, PermissionDecision | str] | None
        ) = None,
        audit_log: str | Path | None = None,
        disable_audit: bool = False,
    ) -> "Settings":
        """按显式参数和环境变量优先级加载并校验配置。"""

        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass

        resolved_model = (
            model
            or os.getenv("CODEFORGE_MODEL")
            or os.getenv("MODEL_ID")
            or os.getenv("OPENAI_MODEL")
        )
        api_key = (
            os.getenv("CODEFORGE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
        )
        resolved_base_url = (
            base_url
            or os.getenv("CODEFORGE_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
        )
        resolved_workspace = Path(workspace).expanduser().resolve()
        raw_default = (
            permission_default
            or os.getenv("CODEFORGE_PERMISSION_DEFAULT")
            or PermissionDecision.ASK
        )
        try:
            resolved_permission_default = PermissionDecision(raw_default)
            resolved_permission_rules = dict(DEFAULT_PERMISSION_RULES)
            resolved_permission_rules.update(
                _normalize_permission_rules(
                    os.getenv("CODEFORGE_PERMISSION_RULES")
                )
            )
            resolved_permission_rules.update(
                _normalize_permission_rules(permission_rules)
            )
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc

        raw_audit_log = (
            audit_log
            if audit_log is not None
            else os.getenv("CODEFORGE_AUDIT_LOG")
        )
        if not resolved_model:
            raise ConfigurationError(
                "missing model: set CODEFORGE_MODEL"
            )
        if not api_key:
            raise ConfigurationError(
                "missing API key: set CODEFORGE_API_KEY, "
                "OPENAI_API_KEY, or DEEPSEEK_API_KEY"
            )
        return cls(
            workspace=resolved_workspace,
            model=resolved_model,
            api_key=api_key,
            base_url=resolved_base_url,
            max_tokens=int(os.getenv("CODEFORGE_MAX_TOKENS", "4096")),
            request_timeout=float(
                os.getenv("CODEFORGE_REQUEST_TIMEOUT", "60")
            ),
            permission_default=resolved_permission_default,
            permission_rules=resolved_permission_rules,
            audit_log=_resolve_audit_log(
                resolved_workspace,
                raw_audit_log,
                disabled=disable_audit,
            ),
        )

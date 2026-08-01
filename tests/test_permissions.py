"""验证工具 allow、ask、deny 决策和 Shell hard-deny 安全基线。

任务流位置：通过 Tool Registry 的统一入口检查参数校验后的权限决策，确认拒绝
调用不会进入具体工具，同时永久危险命令无法因普通 allow 规则绕过。
"""

from pathlib import Path

import pytest

from harness.config import ConfigurationError, Settings
from harness.safety.permissions import (
    PermissionDecision,
    PermissionPolicy,
    parse_permission_rules,
)
from harness.tools import build_default_registry


def test_day1_hard_deny_blocks_destructive_shell_command(
    workspace: Path,
) -> None:
    """验证 Day 1 基线会直接阻止破坏性的 Git 重置命令。"""

    registry = build_default_registry(
        workspace,
        permission_policy=PermissionPolicy(default="allow"),
    )

    result = registry.dispatch(
        "shell",
        {"command": "git reset --hard HEAD"},
    )

    assert result.success is False
    assert "hard deny" in result.content


def test_default_registry_asks_for_mutating_tool(workspace: Path) -> None:
    """验证公共默认注册表不会静默执行修改型工具。"""

    registry = build_default_registry(workspace)

    result = registry.dispatch(
        "write_file",
        {"path": "default-blocked.txt", "content": "blocked"},
    )

    assert result.error_type == "permission_denied"
    assert result.permission_decision == "ask"
    assert not (workspace / "default-blocked.txt").exists()


def test_permission_allow_executes_tool(workspace: Path) -> None:
    """验证 allow 决策会执行工具并写入授权元数据。"""

    registry = build_default_registry(
        workspace,
        permission_policy=PermissionPolicy(
            {"read_file": PermissionDecision.ALLOW},
            default=PermissionDecision.DENY,
        ),
    )

    result = registry.dispatch("read_file", {"path": "src/sample.py"})

    assert result.success
    assert result.permission_decision == "allow"
    assert result.permission_granted is True


def test_permission_deny_prevents_file_write(workspace: Path) -> None:
    """验证 deny 决策在工具执行前阻止文件写入。"""

    registry = build_default_registry(
        workspace,
        permission_policy=PermissionPolicy(
            {"write_file": "deny"},
            default="allow",
        ),
    )

    result = registry.dispatch(
        "write_file",
        {"path": "blocked.txt", "content": "blocked"},
    )

    assert result.success is False
    assert result.error_type == "permission_denied"
    assert result.permission_decision == "deny"
    assert not (workspace / "blocked.txt").exists()


def test_permission_ask_uses_approval_handler(workspace: Path) -> None:
    """验证 ask 决策只有在审批函数同意后才执行工具。"""

    requests = []

    def approve(request) -> bool:
        """记录审批请求并同意本次调用。"""

        requests.append(request)
        return True

    registry = build_default_registry(
        workspace,
        permission_policy=PermissionPolicy(
            {"read_file": "ask"},
            default="deny",
        ),
        approval_handler=approve,
    )

    result = registry.dispatch("read_file", {"path": "src/sample.py"})

    assert result.success
    assert result.permission_decision == "ask"
    assert result.permission_granted is True
    assert requests[0].tool_name == "read_file"


def test_permission_ask_fails_closed_without_approval(
    workspace: Path,
) -> None:
    """验证 ask 缺少审批函数时按拒绝处理。"""

    registry = build_default_registry(
        workspace,
        permission_policy=PermissionPolicy(
            {"write_file": "ask"},
            default="deny",
        ),
    )

    result = registry.dispatch(
        "write_file",
        {"path": "not-approved.txt", "content": "blocked"},
    )

    assert result.error_type == "permission_denied"
    assert not (workspace / "not-approved.txt").exists()


def test_permission_rules_support_globs_and_parser() -> None:
    """验证规则解析器和 Glob 工具名匹配能够协同工作。"""

    rules = parse_permission_rules("read_*=allow,shell=deny")
    policy = PermissionPolicy(rules, default="ask")

    assert policy.decision_for("read_file") is PermissionDecision.ALLOW
    assert policy.decision_for("shell") is PermissionDecision.DENY
    assert policy.decision_for("edit_file") is PermissionDecision.ASK


def test_settings_load_security_policy_and_workspace_audit_path(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证配置层加载权限覆盖并把审计路径限制在 workspace。"""

    monkeypatch.setenv("CODEFORGE_MODEL", "test-model")
    monkeypatch.setenv("CODEFORGE_API_KEY", "test-key")

    settings = Settings.from_env(
        workspace=workspace,
        permission_rules="shell=deny,run_tests=allow",
        audit_log="logs/audit.jsonl",
    )

    assert settings.permission_default is PermissionDecision.ASK
    assert settings.permission_rules["read_file"] is PermissionDecision.ALLOW
    assert settings.permission_rules["shell"] is PermissionDecision.DENY
    assert settings.audit_log == workspace / "logs" / "audit.jsonl"


def test_settings_reject_external_audit_path(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证配置层拒绝把审计日志写到 workspace 外部。"""

    monkeypatch.setenv("CODEFORGE_MODEL", "test-model")
    monkeypatch.setenv("CODEFORGE_API_KEY", "test-key")
    external = tmp_path_factory.mktemp("external-audit") / "audit.jsonl"

    with pytest.raises(ConfigurationError):
        Settings.from_env(workspace=workspace, audit_log=external)

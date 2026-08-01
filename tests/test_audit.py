"""验证 JSONL 审计覆盖工具成功、校验失败、权限拒绝和敏感信息脱敏。

任务流位置：从 Tool Registry 发起不同结果的工具调用，检查调用链末端生成的
审计记录完整、可逐行解析，并且审计写入异常不会隐藏原工具结果。
"""

import json
from pathlib import Path

from harness.safety.audit import AuditLogger
from harness.safety.permissions import PermissionPolicy
from harness.tools import build_default_registry


def _read_records(path: Path) -> list[dict]:
    """读取并解析审计 JSONL 文件中的全部记录。"""

    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_audit_records_success_validation_and_denial(
    workspace: Path,
) -> None:
    """验证三种关键分发结果都会各自产生一条审计记录。"""

    audit_path = workspace / ".codeforge" / "audit.jsonl"
    registry = build_default_registry(
        workspace,
        permission_policy=PermissionPolicy(
            {"write_file": "deny"},
            default="allow",
        ),
        audit_logger=AuditLogger(audit_path),
    )

    registry.dispatch("read_file", {"path": "src/sample.py"})
    registry.dispatch("read_file", {})
    registry.dispatch(
        "write_file",
        {"path": "blocked.txt", "content": "blocked"},
    )

    records = _read_records(audit_path)
    assert [record["result"]["error_type"] for record in records] == [
        None,
        "validation_error",
        "permission_denied",
    ]
    assert records[0]["permission"]["decision"] == "allow"
    assert records[2]["permission"]["granted"] is False


def test_audit_redacts_sensitive_fields_and_strings(
    workspace: Path,
) -> None:
    """验证审计记录不会保留敏感字段或常见 Key 字符串。"""

    audit_path = workspace / "audit.jsonl"
    logger = AuditLogger(audit_path)

    logger.record_tool_call(
        tool_name="shell",
        arguments={
            "api_key": "sk-secretvalue123",
            "command": "run --token=secret-token sk-anothersecret",
        },
        permission_decision="deny",
        permission_granted=False,
        permission_reason="denied by policy",
        success=False,
        duration_ms=0.1,
        error_type="permission_denied",
        content="password=hunter2",
    )

    raw = audit_path.read_text(encoding="utf-8")
    assert "secretvalue123" not in raw
    assert "secret-token" not in raw
    assert "anothersecret" not in raw
    assert "hunter2" not in raw
    assert "[REDACTED]" in raw


def test_audit_write_failure_is_exposed_without_changing_tool_success(
    workspace: Path,
) -> None:
    """验证审计写入失败会进入结果元数据而不伪造工具失败。"""

    registry = build_default_registry(
        workspace,
        audit_logger=AuditLogger(workspace),
    )

    result = registry.dispatch("read_file", {"path": "src/sample.py"})

    assert result.success
    assert result.audit_error is not None


"""把每次工具调用以脱敏 JSON Lines 记录到 workspace 审计文件。

任务流位置：Tool Registry 在未知工具、参数错误、权限拒绝、Hook 拒绝和实际
执行完成后统一调用本模块，使安全决策和执行结果都留下可追溯记录。
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogError(OSError):
    """审计日志无法写入时抛出的异常。"""


class AuditLogger:
    """以线程安全方式追加脱敏的 UTF-8 JSONL 工具审计记录。"""

    sensitive_markers = (
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
    )
    secret_patterns = (
        re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}"),
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password)"
            r"\s*[:=]\s*[^\s,;]+"
        ),
    )

    def __init__(self, path: str | Path) -> None:
        """绑定 JSONL 文件路径并创建进程内写入锁。"""

        self.path = Path(path)
        self._lock = threading.Lock()

    def record_tool_call(
        self,
        *,
        tool_name: str,
        arguments: Any,
        permission_decision: str | None,
        permission_granted: bool | None,
        permission_reason: str | None,
        success: bool,
        duration_ms: float,
        error_type: str | None,
        content: str,
        hook_errors: tuple[str, ...] = (),
    ) -> None:
        """构造、脱敏并追加一条完整工具调用审计记录。"""

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "tool_use",
            "tool_name": tool_name,
            "arguments": self._redact(arguments),
            "permission": {
                "decision": permission_decision,
                "granted": permission_granted,
                "reason": permission_reason,
            },
            "result": {
                "success": success,
                "error_type": error_type,
                "duration_ms": duration_ms,
                "content": self._redact_string(content[:2000]),
            },
            "hook_errors": list(hook_errors),
        }
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            default=str,
                        )
                    )
                    stream.write("\n")
        except OSError as exc:
            raise AuditLogError(
                f"failed to write audit log {self.path}: {exc}"
            ) from exc

    @classmethod
    def _redact(cls, value: Any, key: str = "") -> Any:
        """递归隐藏敏感字段，并脱敏普通字符串中的常见密钥格式。"""

        normalized_key = key.lower().replace("-", "_")
        if any(marker in normalized_key for marker in cls.sensitive_markers):
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {
                str(item_key): cls._redact(item_value, str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._redact(item) for item in value]
        if isinstance(value, str):
            return cls._redact_string(value)
        return value

    @classmethod
    def _redact_string(cls, value: str) -> str:
        """替换字符串内常见的 API Key 和凭据赋值片段。"""

        redacted = value
        for pattern in cls.secret_patterns:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

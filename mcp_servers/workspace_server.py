"""提供 workspace Python 文件统计工具的最小真实 stdio MCP Server。

任务流位置：Harness 根据示例 MCP 配置启动本进程，通过 stdin/stdout 完成
initialize、tools/list 和 tools/call；统计结果随后返回主 Tool Registry 和模型。
"""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_VERSIONS = {
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
}
IGNORED_DIRECTORIES = {
    ".codeforge",
    ".git",
    ".pytest_cache",
    ".uv-cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


def main() -> int:
    """逐行处理 MCP JSON-RPC 消息，直到客户端关闭 stdin。"""

    initialized = False
    for raw_line in sys.stdin:
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON-RPC input: {exc}", file=sys.stderr)
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params", {})

        if method == "initialize" and request_id is not None:
            requested = str(params.get("protocolVersion", PROTOCOL_VERSION))
            negotiated = (
                requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
            )
            _respond(
                request_id,
                {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "codeforge-workspace-demo",
                        "version": "0.1.0",
                    },
                },
            )
            continue
        if method == "notifications/initialized":
            initialized = True
            continue
        if method == "notifications/cancelled":
            continue
        if request_id is None:
            continue
        if not initialized:
            _error(request_id, -32002, "server is not initialized")
            continue
        if method == "ping":
            _respond(request_id, {})
        elif method == "tools/list":
            _respond(request_id, {"tools": [_project_stats_definition()]})
        elif method == "tools/call":
            _handle_tool_call(request_id, params)
        else:
            _error(request_id, -32601, f"unknown method: {method}")
    return 0


def _project_stats_definition() -> dict[str, Any]:
    """返回 project_stats 的 MCP Tool 定义与输入 JSON Schema。"""

    return {
        "name": "project_stats",
        "description": (
            "Count matching UTF-8 source files and their total lines inside "
            "the configured workspace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory.",
                    "default": ".",
                },
                "glob": {
                    "type": "string",
                    "description": "Filename glob, for example *.py.",
                    "default": "*.py",
                },
            },
            "additionalProperties": False,
        },
    }


def _handle_tool_call(request_id: Any, params: Any) -> None:
    """校验工具名和参数，执行统计并返回标准 CallToolResult。"""

    if not isinstance(params, dict) or params.get("name") != "project_stats":
        _error(request_id, -32602, "unknown tool")
        return
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        _error(request_id, -32602, "tool arguments must be an object")
        return
    try:
        stats = _collect_project_stats(
            str(arguments.get("path", ".")),
            str(arguments.get("glob", "*.py")),
        )
    except (OSError, ValueError) as exc:
        _respond(
            request_id,
            {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            },
        )
        return
    text = (
        f"Found {stats['file_count']} files matching {stats['glob']} under "
        f"{stats['path']}, with {stats['line_count']} total lines."
    )
    _respond(
        request_id,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": stats,
            "isError": False,
        },
    )


def _collect_project_stats(relative_path: str, pattern: str) -> dict[str, Any]:
    """在当前 workspace 边界内递归统计匹配的 UTF-8 文件。"""

    workspace = Path.cwd().resolve()
    target = (workspace / relative_path).resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("path escapes the MCP workspace") from exc
    if not target.is_dir():
        raise ValueError(f"not a directory: {relative_path}")
    if not pattern.strip():
        raise ValueError("glob cannot be empty")

    file_count = 0
    line_count = 0
    for candidate in target.rglob("*"):
        if not candidate.is_file():
            continue
        relative_parts = candidate.relative_to(workspace).parts
        if any(part in IGNORED_DIRECTORIES for part in relative_parts):
            continue
        if not fnmatch.fnmatch(candidate.name, pattern):
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        file_count += 1
        line_count += len(text.splitlines())
    return {
        "path": relative_path,
        "glob": pattern,
        "file_count": file_count,
        "line_count": line_count,
    }


def _respond(request_id: Any, result: dict[str, Any]) -> None:
    """向 stdout 写入单行 JSON-RPC 成功响应。"""

    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> None:
    """向 stdout 写入单行 JSON-RPC 协议错误。"""

    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _send(message: dict[str, Any]) -> None:
    """序列化一个不含物理换行的 UTF-8 JSON-RPC 消息并立即刷新。"""

    sys.stdout.write(
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())

"""验证真实 stdio MCP 握手、工具发现、调用、校验和 CLI 生命周期。

任务流位置：测试启动独立本地 MCP Server 子进程，通过真实 stdin/stdout 完成
initialize、tools/list 和 tools/call，再确认动态工具进入主 Registry 安全链。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness import cli
from harness.mcp_stdio import (
    MCPConfigurationError,
    StdioMCPManager,
    StdioMCPServerConfig,
    load_mcp_config,
)
from harness.safety.permissions import PermissionPolicy
from harness.tools import build_default_registry


def _server_path() -> Path:
    """返回仓库中真实示例 MCP Server 的绝对路径。"""

    return (
        Path(__file__).resolve().parents[1]
        / "mcp_servers"
        / "workspace_server.py"
    )


def _manager(workspace: Path) -> StdioMCPManager:
    """为临时 workspace 创建指向示例 Server 的 MCP Manager。"""

    config = StdioMCPServerConfig(
        command=[sys.executable, str(_server_path())],
        cwd=".",
        timeout_seconds=5,
    )
    return StdioMCPManager({"workspace": config}, workspace)


def test_real_stdio_mcp_discovers_and_calls_tool(workspace: Path) -> None:
    """验证真实子进程完成握手、发现工具并返回文本和结构化结果。"""

    manager = _manager(workspace)
    try:
        remote_tools = manager.start_and_discover()
        registry = build_default_registry(
            workspace,
            permission_policy=PermissionPolicy(default="allow"),
        )
        for tool in remote_tools:
            registry.register(tool)

        result = registry.dispatch(
            "mcp_workspace_project_stats",
            {"path": "src", "glob": "*.py"},
        )

        assert result.success is True
        assert "Found 1 files" in result.content
        assert '"file_count": 1' in result.content
        assert manager.clients["workspace"].protocol_version == "2025-11-25"
    finally:
        manager.close()
    assert manager.clients["workspace"].process is None


def test_mcp_tool_arguments_use_registry_validation(workspace: Path) -> None:
    """验证远端调用前仍由 Pydantic 拒绝 inputSchema 之外的参数。"""

    manager = _manager(workspace)
    try:
        registry = build_default_registry(
            workspace,
            permission_policy=PermissionPolicy(default="allow"),
        )
        for tool in manager.start_and_discover():
            registry.register(tool)

        result = registry.dispatch(
            "mcp_workspace_project_stats",
            {"path": ".", "glob": "*.py", "unexpected": True},
        )

        assert result.success is False
        assert result.error_type == "validation_error"
    finally:
        manager.close()


def test_cli_list_tools_includes_explicit_mcp_config(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验证 --mcp-config 能发现工具，并在列出后关闭临时 Server。"""

    config_path = workspace / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": {
                    "workspace": {
                        "command": [sys.executable, str(_server_path())],
                        "cwd": ".",
                        "timeout_seconds": 5,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "--workspace",
            str(workspace),
            "--mcp-config",
            "mcp.json",
            "--list-tools",
        ]
    )

    assert exit_code == 0
    assert "mcp_workspace_project_stats" in capsys.readouterr().out


def test_mcp_config_path_cannot_escape_workspace(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """验证 MCP 配置本身必须位于显式 workspace 内。"""

    external = tmp_path_factory.mktemp("external-mcp") / "mcp.json"
    external.write_text('{"servers": {}}', encoding="utf-8")

    with pytest.raises(MCPConfigurationError):
        load_mcp_config(external, workspace)

"""验证 SQLite 长期记忆的跨实例检索、工具访问和敏感信息保护。

任务流位置：覆盖 memory_write/search/delete 和 CLI 自动召回所依赖的 MemoryStore，
证明记忆按 workspace 数据库持久化且不会保存常见 API Key。
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from harness.config import ConfigurationError, Settings
from harness import cli
from harness.context.memory import (
    MemoryStore,
    SensitiveMemoryError,
)
from harness.providers.base import AssistantTurn
from harness.safety.permissions import PermissionPolicy
from harness.tools import build_default_registry


class _MemoryCLIProvider:
    """记录 CLI 发给 Provider 的系统上下文而不访问网络。"""

    requests: list[list[dict]] = []

    def __init__(self, **kwargs) -> None:
        """接受真实 Provider 参数以兼容 CLI 装配。"""

        self.options = kwargs

    def complete(self, messages, tools) -> AssistantTurn:
        """保存消息并返回确定性答案。"""

        self.requests.append(deepcopy(messages))
        return AssistantTurn(content="memory context received")


def test_memory_persists_updates_and_searches_across_instances(
    workspace: Path,
) -> None:
    """验证稳定 key 更新不会改变 ID，且新实例能检索旧数据。"""

    path = workspace / ".codeforge" / "memory.sqlite3"
    first_store = MemoryStore(path)
    created = first_store.write(
        "test-command",
        "项目测试命令是 pytest -q",
        tags=["pytest", "workflow"],
    )
    updated = first_store.write(
        "test-command",
        "完整测试命令是 python -m pytest -q",
        tags=["pytest"],
    )

    second_store = MemoryStore(path)
    results = second_store.search("如何运行 pytest 测试")

    assert created.id == updated.id
    assert results[0].key == "test-command"
    assert "python -m pytest" in results[0].content
    assert "test-command" in second_store.render_relevant("pytest")


def test_memory_rejects_credentials(workspace: Path) -> None:
    """验证疑似 API Key 或密码赋值不会进入长期存储。"""

    store = MemoryStore(workspace / "memory.sqlite3")

    with pytest.raises(SensitiveMemoryError):
        store.write("provider", "api_key=sk-super-secret-value")

    assert store.list_recent() == []


def test_memory_tools_write_search_and_delete(workspace: Path) -> None:
    """验证主 Tool Registry 能完整管理一条长期记忆。"""

    store = MemoryStore(workspace / "memory.sqlite3")
    registry = build_default_registry(
        workspace,
        memory_store=store,
        permission_policy=PermissionPolicy(default="allow"),
    )

    written = registry.dispatch(
        "memory_write",
        {
            "key": "python-version",
            "content": "项目要求 Python 3.10+",
            "tags": ["python", "runtime"],
        },
    )
    searched = registry.dispatch(
        "memory_search",
        {"query": "Python runtime", "limit": 5},
    )
    deleted = registry.dispatch(
        "memory_delete",
        {"key": "python-version"},
    )

    assert written.success is True
    assert json.loads(searched.content)[0]["key"] == "python-version"
    assert deleted.success is True
    assert store.list_recent() == []


def test_separate_memory_databases_are_isolated(workspace: Path) -> None:
    """验证不同 workspace 数据库不会互相召回记忆。"""

    first = MemoryStore(workspace / "one" / "memory.sqlite3")
    second = MemoryStore(workspace / "two" / "memory.sqlite3")
    first.write("private-project-fact", "only workspace one knows this")

    assert first.search("workspace one")
    assert second.search("workspace one") == []


def test_memory_database_path_is_workspace_sandboxed(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Memory SQLite 路径不能通过配置逃逸出 workspace。"""

    monkeypatch.setenv("CODEFORGE_MODEL", "test-model")
    monkeypatch.setenv("CODEFORGE_API_KEY", "test-key")

    settings = Settings.from_env(
        workspace=workspace,
        memory_db=".state/memory.sqlite3",
    )

    assert settings.memory_db == (
        workspace / ".state" / "memory.sqlite3"
    ).resolve()
    with pytest.raises(ConfigurationError):
        Settings.from_env(
            workspace=workspace,
            memory_db=workspace.parent / "outside-memory.sqlite3",
        )


def test_cli_automatically_injects_relevant_memory(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证新会话无需 --resume 也能召回 workspace 长期记忆。"""

    memory_path = workspace / ".codeforge" / "memory.sqlite3"
    MemoryStore(memory_path).write(
        "format-command",
        "格式化命令使用 ruff format",
        tags=["format"],
    )
    _MemoryCLIProvider.requests = []
    monkeypatch.setenv("CODEFORGE_MODEL", "test-model")
    monkeypatch.setenv("CODEFORGE_API_KEY", "test-key")
    monkeypatch.setenv(
        "CODEFORGE_MEMORY_DB",
        ".codeforge/memory.sqlite3",
    )
    monkeypatch.setattr(cli, "OpenAICompatibleProvider", _MemoryCLIProvider)

    exit_code = cli.main(
        [
            "--workspace",
            str(workspace),
            "--no-session",
            "项目的格式化命令是什么",
        ]
    )

    assert exit_code == 0
    system_prompt = _MemoryCLIProvider.requests[0][0]["content"]
    assert "format-command" in system_prompt
    assert "ruff format" in system_prompt

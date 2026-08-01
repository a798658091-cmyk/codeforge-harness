"""验证 Day 1 七种默认工具的成功路径和关键失败保护。

任务流位置：绕过模型层，直接从 Tool Registry 分发到文件、搜索、Patch、Shell
和 pytest 工具，检查具体 workspace 操作及结构化执行结果。
"""

from __future__ import annotations

import sys
from pathlib import Path

from harness.tools.registry import ToolRegistry


def test_file_tools_cover_create_read_and_exact_edit(
    registry: ToolRegistry,
    workspace: Path,
) -> None:
    """验证文件工具可以依次创建、按行读取并精确修改文本。"""

    created = registry.dispatch(
        "write_file",
        {"path": "notes/todo.txt", "content": "first\nsecond\n"},
    )
    read = registry.dispatch(
        "read_file",
        {"path": "notes/todo.txt", "offset": 2, "limit": 1},
    )
    edited = registry.dispatch(
        "edit_file",
        {
            "path": "notes/todo.txt",
            "old_text": "second",
            "new_text": "done",
        },
    )

    assert created.success
    assert read.content == "2: second"
    assert edited.success
    assert (workspace / "notes" / "todo.txt").read_text(
        encoding="utf-8"
    ) == "first\ndone\n"


def test_write_file_refuses_overwrite(registry: ToolRegistry) -> None:
    """验证 write_file 不会覆盖 workspace 中的已有文件。"""

    result = registry.dispatch(
        "write_file",
        {"path": "src/sample.py", "content": "destroyed"},
    )

    assert result.success is False
    assert "refusing to overwrite" in result.content


def test_search_finds_matching_source_lines(
    registry: ToolRegistry,
) -> None:
    """验证 search 能按范围、Glob 和大小写规则返回源码行。"""

    result = registry.dispatch(
        "search",
        {
            "query": "GREET",
            "path": "src",
            "glob": "*.py",
            "case_sensitive": False,
        },
    )

    assert result.success
    assert "src/sample.py:1: def greet(name):" in result.content


def test_apply_patch_validates_all_changes_before_writing(
    registry: ToolRegistry,
    workspace: Path,
) -> None:
    """验证 apply_patch 在任一变更无效时不会写入其他变更。"""

    result = registry.dispatch(
        "apply_patch",
        {
            "changes": [
                {
                    "operation": "add",
                    "path": "src/new_module.py",
                    "content": "VALUE = 1\n",
                },
                {
                    "operation": "update",
                    "path": "src/sample.py",
                    "old_text": "does not exist",
                    "new_text": "replacement",
                },
            ]
        },
    )

    assert result.success is False
    assert not (workspace / "src" / "new_module.py").exists()


def test_apply_patch_adds_and_updates_multiple_files(
    registry: ToolRegistry,
    workspace: Path,
) -> None:
    """验证 apply_patch 能在一次调用中新增并更新多个文件。"""

    result = registry.dispatch(
        "apply_patch",
        {
            "changes": [
                {
                    "operation": "add",
                    "path": "src/constants.py",
                    "content": "GREETING = 'hello'\n",
                },
                {
                    "operation": "update",
                    "path": "src/sample.py",
                    "old_text": "hello",
                    "new_text": "hi",
                },
            ]
        },
    )

    assert result.success
    assert (workspace / "src" / "constants.py").exists()
    assert "hi" in (workspace / "src" / "sample.py").read_text(
        encoding="utf-8"
    )


def test_shell_runs_in_workspace_and_blocks_hard_deny(
    registry: ToolRegistry,
) -> None:
    """验证 Shell 正常执行命令，同时拒绝 hard-deny 危险命令。"""

    command = f'"{sys.executable}" -c "print(6 * 7)"'
    allowed = registry.dispatch("shell", {"command": command})
    denied = registry.dispatch(
        "shell",
        {"command": "git reset --hard HEAD"},
    )

    assert allowed.success
    assert "42" in allowed.content
    assert denied.success is False
    assert "hard deny" in denied.content


def test_run_tests_tool_executes_pytest(
    registry: ToolRegistry,
    workspace: Path,
) -> None:
    """验证 run_tests 能在临时 workspace 内运行真实 pytest。"""

    sample = workspace / "sample_test.py"
    sample.write_text(
        "def test_sample():\n"
        "    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )

    result = registry.dispatch(
        "run_tests",
        {"targets": ["sample_test.py"], "extra_args": ["-q"]},
    )

    assert result.success
    assert "1 passed" in result.content

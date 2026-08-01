"""验证 Workspace 接受内部路径并拒绝相对或绝对的目录逃逸。

任务流位置：直接测试所有文件类工具共同依赖的最底层路径安全边界，确保工具在
真正读写或启动子进程前只能解析到测试 workspace 内部。
"""

from pathlib import Path

import pytest

from harness.safety.workspace import Workspace, WorkspaceViolation
from harness.tools.registry import ToolRegistry


def test_workspace_accepts_relative_paths(workspace: Path) -> None:
    """验证 Workspace 能把内部相对路径解析为正确绝对路径。"""

    boundary = Workspace(workspace)

    resolved = boundary.resolve("src/sample.py", must_exist=True)

    assert resolved == workspace / "src" / "sample.py"


def test_workspace_rejects_parent_traversal(workspace: Path) -> None:
    """验证 Workspace 拒绝使用父目录片段逃逸根目录。"""

    boundary = Workspace(workspace)

    with pytest.raises(WorkspaceViolation):
        boundary.resolve("../outside.txt")


def test_workspace_rejects_absolute_external_path(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """验证 Workspace 拒绝直接传入外部绝对文件路径。"""

    boundary = Workspace(workspace)
    external = tmp_path_factory.mktemp("external") / "secret.txt"
    external.write_text("secret", encoding="utf-8")

    with pytest.raises(WorkspaceViolation):
        boundary.resolve(external, must_exist=True)


def test_workspace_rejects_symlink_escape(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """验证指向外部目录的符号链接不能绕过 workspace 边界。"""

    external = tmp_path_factory.mktemp("symlink-external")
    (external / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "outside-link"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(WorkspaceViolation):
        Workspace(workspace).resolve(
            "outside-link/secret.txt",
            must_exist=True,
        )


def test_shell_cwd_cannot_escape_workspace(
    registry: ToolRegistry,
) -> None:
    """验证 Shell 的工作目录不能通过父目录逃逸 workspace。"""

    result = registry.dispatch(
        "shell",
        {"command": "echo blocked", "cwd": ".."},
    )

    assert result.success is False
    assert "escapes workspace" in result.content

"""验证真实 Git Worktree 的隔离、提交、差异和主分支集成闭环。

任务流位置：模拟可写 Subagent 在独立目录修改文件，随后由 WorktreeManager
提交并 cherry-pick 回主工作区，覆盖最关键的隔离边界而不访问真实 LLM。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harness.worktrees import (
    WorktreeConflictError,
    WorktreeError,
    WorktreeManager,
)


def _git(repo: Path, *arguments: str) -> str:
    """在测试仓库执行 Git，并直接暴露失败输出。"""

    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    """创建具有单个基线提交且忽略 Worktree 目录的临时 Git 仓库。"""

    if shutil.which("git") is None:
        pytest.skip("git executable is required")
    _git(tmp_path, "init")
    (tmp_path / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "initial",
    )
    return tmp_path


def test_worktree_change_can_be_reviewed_and_integrated(
    git_workspace: Path,
) -> None:
    """验证隔离文件不可见、补丁可审查、集成后才进入主工作区。"""

    manager = WorktreeManager(git_workspace)
    record = manager.create("worker1")
    child_file = record.path / "worker-result.txt"
    child_file.write_text("created in worktree\n", encoding="utf-8")

    assert not (git_workspace / "worker-result.txt").exists()
    commit = manager.commit("worker1", "worker change")
    assert commit
    assert "worker-result.txt" in manager.diff("worker1")

    manager.integrate("worker1")

    assert (git_workspace / "worker-result.txt").read_text(
        encoding="utf-8"
    ) == "created in worktree\n"
    assert manager.get("worker1").status == "integrated"
    manager.remove("worker1")


def test_integrate_rejects_dirty_main_workspace(
    git_workspace: Path,
) -> None:
    """验证主工作区有未提交修改时不会集成或覆盖用户内容。"""

    manager = WorktreeManager(git_workspace)
    record = manager.create("dirty-main")
    (record.path / "worker.txt").write_text("worker\n", encoding="utf-8")
    commit = manager.commit("dirty-main", "worker change")
    (git_workspace / "local.txt").write_text("local\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="uncommitted changes"):
        manager.integrate("dirty-main")

    assert commit
    assert not (git_workspace / "worker.txt").exists()
    assert (git_workspace / "local.txt").read_text(
        encoding="utf-8"
    ) == "local\n"
    assert manager.get("dirty-main").status == "committed"


def test_conflicting_second_worktree_aborts_cherry_pick(
    git_workspace: Path,
) -> None:
    """验证两个分支冲突时保留首个结果并清理 cherry-pick 中间态。"""

    conflict_file = git_workspace / "conflict.txt"
    conflict_file.write_text("base\n", encoding="utf-8")
    _git(git_workspace, "add", "conflict.txt")
    _git(
        git_workspace,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "conflict base",
    )
    manager = WorktreeManager(git_workspace)
    first = manager.create("first")
    second = manager.create("second")
    (first.path / "conflict.txt").write_text("first\n", encoding="utf-8")
    (second.path / "conflict.txt").write_text("second\n", encoding="utf-8")
    manager.commit("first", "first change")
    manager.commit("second", "second change")

    manager.integrate("first")
    with pytest.raises(WorktreeConflictError):
        manager.integrate("second")

    assert conflict_file.read_text(encoding="utf-8") == "first\n"
    assert manager.get("second").status == "conflict"
    assert _git(git_workspace, "status", "--porcelain") == ""
    assert not (git_workspace / ".git" / "CHERRY_PICK_HEAD").exists()

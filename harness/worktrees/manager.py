"""管理可写 Subagent 使用的独立 Git Worktree 和变更集成。

任务流位置：WritableSubagentManager 启动任务前在这里创建分支工作区，完成后
提交变更；主 Agent 明确调用集成工具时，本模块再把提交 cherry-pick 回主分支。
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """Git Worktree 创建、提交、查询或清理失败时的统一异常。"""


class WorktreeConflictError(WorktreeError):
    """子分支提交无法干净集成回主工作区时抛出的异常。"""


@dataclass
class WorktreeRecord:
    """保存一个隔离工作区的路径、分支、基线提交和当前状态。"""

    id: str
    path: Path
    branch: str
    base_commit: str
    status: str = "ready"
    commit: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """返回将 Path 转成文本后的可序列化快照。"""

        result = asdict(self)
        result["path"] = str(self.path)
        return result


class WorktreeManager:
    """以线程安全方式创建、提交、集成和移除受管 Git Worktree。"""

    _safe_id = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def __init__(
        self,
        workspace: str | Path,
        *,
        worktree_root: str | Path | None = None,
    ) -> None:
        """绑定 Git 仓库根目录和默认的 ``.worktrees`` 存放位置。"""

        self.workspace = Path(workspace).resolve()
        self.root = (
            Path(worktree_root).resolve()
            if worktree_root is not None
            else self.workspace / ".worktrees"
        )
        try:
            self.root.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError("worktree root must be inside workspace") from exc
        self._records: dict[str, WorktreeRecord] = {}
        self._lock = threading.RLock()
        self._ensure_repository()

    def create(self, worktree_id: str) -> WorktreeRecord:
        """从主工作区当前 HEAD 创建唯一分支和隔离目录。"""

        self._validate_id(worktree_id)
        with self._lock:
            if worktree_id in self._records:
                raise WorktreeError(f"worktree already exists: {worktree_id}")
            path = (self.root / worktree_id).resolve()
            if path.exists():
                raise WorktreeError(f"worktree path already exists: {path}")
            branch = f"codeforge/subagent/{worktree_id}"
            base_commit = self._git("rev-parse", "HEAD").strip()
            self.root.mkdir(parents=True, exist_ok=True)
            self._git("worktree", "add", "-b", branch, str(path), base_commit)
            record = WorktreeRecord(
                id=worktree_id,
                path=path,
                branch=branch,
                base_commit=base_commit,
            )
            self._records[worktree_id] = record
            return self._copy(record)

    def get(self, worktree_id: str) -> WorktreeRecord:
        """按标识返回工作区记录副本，不允许调用方修改内部状态。"""

        with self._lock:
            record = self._records.get(worktree_id)
            if record is None:
                raise WorktreeError(f"unknown worktree: {worktree_id}")
            return self._copy(record)

    def commit(self, worktree_id: str, message: str) -> str | None:
        """提交隔离工作区的全部变更；没有变更时返回 None。"""

        with self._lock:
            record = self._require(worktree_id)
            if not self._git("status", "--porcelain", cwd=record.path).strip():
                record.status = "no_changes"
                return None
            self._git("add", "-A", cwd=record.path)
            self._git(
                "-c",
                "user.name=CodeForge Subagent",
                "-c",
                "user.email=codeforge-subagent@local",
                "commit",
                "-m",
                message[:200],
                cwd=record.path,
            )
            record.commit = self._git(
                "rev-parse", "HEAD", cwd=record.path
            ).strip()
            record.status = "committed"
            return record.commit

    def diff(self, worktree_id: str, *, max_chars: int = 50_000) -> str:
        """返回子工作区相对创建基线的补丁，并限制工具上下文大小。"""

        with self._lock:
            record = self._require(worktree_id)
            output = self._git(
                "diff",
                "--no-ext-diff",
                record.base_commit,
                cwd=record.path,
            )
            return output[:max_chars] or "(no changes)"

    def integrate(self, worktree_id: str) -> str:
        """要求主工作区干净，并把子工作区提交 cherry-pick 到当前分支。"""

        with self._lock:
            record = self._require(worktree_id)
            if not record.commit:
                raise WorktreeError("subagent has no commit to integrate")
            if self._git("status", "--porcelain").strip():
                raise WorktreeError(
                    "main workspace has uncommitted changes; commit or stash "
                    "them before integrating a subagent"
                )
            try:
                self._git("cherry-pick", record.commit)
            except WorktreeError as exc:
                subprocess.run(
                    ["git", "cherry-pick", "--abort"],
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                record.status = "conflict"
                raise WorktreeConflictError(
                    f"failed to integrate {worktree_id}: {exc}"
                ) from exc
            record.status = "integrated"
            return record.commit

    def remove(self, worktree_id: str, *, force: bool = False) -> None:
        """移除隔离目录；默认拒绝丢弃其中尚未提交的修改。"""

        with self._lock:
            record = self._require(worktree_id)
            arguments = ["worktree", "remove"]
            if force:
                arguments.append("--force")
            arguments.append(str(record.path))
            self._git(*arguments)
            if record.status == "integrated":
                self._git("branch", "-D", record.branch)
            del self._records[worktree_id]

    def list(self) -> list[WorktreeRecord]:
        """返回当前进程创建的全部工作区记录副本。"""

        with self._lock:
            return [self._copy(record) for record in self._records.values()]

    def _ensure_repository(self) -> None:
        """确认目标目录是具有有效 HEAD 的 Git 工作区。"""

        if not self.workspace.is_dir():
            raise WorktreeError(f"workspace is not a directory: {self.workspace}")
        self._git("rev-parse", "--verify", "HEAD")

    @classmethod
    def _validate_id(cls, worktree_id: str) -> None:
        """限制标识字符，避免它逃逸目录或生成异常分支名。"""

        if not cls._safe_id.fullmatch(worktree_id):
            raise ValueError("invalid worktree id")

    def _require(self, worktree_id: str) -> WorktreeRecord:
        """取得内部可变记录，调用者必须已持有管理器锁。"""

        record = self._records.get(worktree_id)
        if record is None:
            raise WorktreeError(f"unknown worktree: {worktree_id}")
        return record

    def _git(self, *arguments: str, cwd: Path | None = None) -> str:
        """以参数数组执行 Git，并把非零退出码转换为清晰异常。"""

        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=cwd or self.workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            raise WorktreeError("git executable was not found") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise WorktreeError(detail or f"git exited {completed.returncode}")
        return completed.stdout

    @staticmethod
    def _copy(record: WorktreeRecord) -> WorktreeRecord:
        """复制记录，隔离内部可变状态。"""

        return WorktreeRecord(**asdict(record))


__all__ = [
    "WorktreeConflictError",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeRecord",
]

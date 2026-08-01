"""实现 workspace 根目录校验、路径规范化与越界阻断。

任务流位置：Tool Registry 将 Workspace 放入 ToolContext；每个文件、搜索、
Patch、Shell 和测试工具在实际访问路径前都通过本模块解析安全路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceViolation(ValueError):
    """请求路径在规范化后逃逸出配置的 workspace 时抛出。"""


@dataclass(frozen=True)
class Workspace:
    """表示经过校验的工作区根目录及其安全路径解析能力。"""

    root: Path

    def __post_init__(self) -> None:
        """规范化根目录，并确认它是已存在的目录。"""

        resolved = self.root.expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"workspace does not exist: {resolved}")
        if not resolved.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {resolved}")
        object.__setattr__(self, "root", resolved)

    def resolve(
        self,
        path: str | Path,
        *,
        must_exist: bool = False,
    ) -> Path:
        """解析路径并拒绝规范化后逃逸出工作区的目标。"""

        raw = Path(path).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceViolation(
                f"path escapes workspace: {path}"
            )
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"path does not exist: {path}")
        return resolved

    def relative(self, path: str | Path) -> str:
        """返回安全目标相对于工作区根目录的 POSIX 风格路径。"""

        return self.resolve(path).relative_to(self.root).as_posix()

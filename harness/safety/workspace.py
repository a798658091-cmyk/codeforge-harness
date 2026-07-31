from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceViolation(ValueError):
    """Raised when a requested path escapes the configured workspace."""


@dataclass(frozen=True)
class Workspace:
    root: Path

    def __post_init__(self) -> None:
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
        return self.resolve(path).relative_to(self.root).as_posix()

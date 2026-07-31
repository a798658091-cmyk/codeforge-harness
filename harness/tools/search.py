from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from harness.safety.workspace import WorkspaceViolation
from harness.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolError,
)


class SearchArguments(ToolArguments):
    query: str = Field(min_length=1)
    path: str = "."
    glob: str = "*"
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=500)


class SearchTool(BaseTool):
    name: ClassVar[str] = "search"
    description: ClassVar[str] = (
        "Search text recursively inside workspace files."
    )
    arguments_model: ClassVar[type[ToolArguments]] = SearchArguments
    max_file_bytes = 2_000_000

    def execute(
        self,
        arguments: SearchArguments,
        context: ToolContext,
    ) -> str:
        root = context.workspace.resolve(arguments.path, must_exist=True)
        candidates = [root] if root.is_file() else root.rglob("*")
        needle = (
            arguments.query
            if arguments.case_sensitive
            else arguments.query.casefold()
        )
        matches: list[str] = []

        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                safe_candidate = context.workspace.resolve(
                    candidate,
                    must_exist=True,
                )
            except WorkspaceViolation:
                continue
            relative = safe_candidate.relative_to(
                context.workspace.root
            ).as_posix()
            if not self._glob_matches(relative, safe_candidate, arguments.glob):
                continue
            if safe_candidate.stat().st_size > self.max_file_bytes:
                continue
            try:
                lines = safe_candidate.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                haystack = line if arguments.case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append(f"{relative}:{line_number}: {line}")
                    if len(matches) >= arguments.max_results:
                        return "\n".join(matches)
        return "\n".join(matches) if matches else "(no matches)"

    @staticmethod
    def _glob_matches(relative: str, path: Path, pattern: str) -> bool:
        if not pattern:
            raise ToolError("glob cannot be empty")
        return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(
            path.name,
            pattern,
        )

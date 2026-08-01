"""实现 workspace 内递归文本搜索和文件过滤。

任务流位置：模型发出 search 调用后由 Tool Registry 分发到这里；本模块逐个
校验候选路径并生成“文件:行号:内容”结果，随后由 Agent Loop 回灌模型。
"""

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
    """定义搜索词、搜索范围、Glob、大小写和结果数量参数。"""

    query: str = Field(min_length=1)
    path: str = "."
    glob: str = "*"
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=500)


class SearchTool(BaseTool):
    """递归搜索 workspace 内符合过滤条件的 UTF-8 文本文件。"""

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
        """遍历安全候选文件并返回包含路径和行号的匹配项。"""

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
        """判断相对路径或文件名是否匹配指定 Glob 模式。"""

        if not pattern:
            raise ToolError("glob cannot be empty")
        return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(
            path.name,
            pattern,
        )

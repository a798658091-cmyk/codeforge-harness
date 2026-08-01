"""实现受 workspace 约束的文本文件读取、创建和精确替换工具。

任务流位置：Tool Registry 完成参数校验后调用这里；工具先经 Workspace 解析
目标路径，再执行文件操作并把可读结果返回 Agent Loop。
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from harness.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolError,
)


class ReadFileArguments(ToolArguments):
    """定义 read_file 的路径、起始行和读取行数参数。"""

    path: str = Field(min_length=1)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=2000, ge=1, le=10000)


class ReadFileTool(BaseTool):
    """读取 workspace 内的 UTF-8 文本并附加行号。"""

    name: ClassVar[str] = "read_file"
    description: ClassVar[str] = (
        "Read a UTF-8 text file inside the workspace with line numbers."
    )
    arguments_model: ClassVar[type[ToolArguments]] = ReadFileArguments

    def execute(
        self,
        arguments: ReadFileArguments,
        context: ToolContext,
    ) -> str:
        """校验目标为文件，并返回请求范围内的带行号文本。"""

        path = context.workspace.resolve(arguments.path, must_exist=True)
        if not path.is_file():
            raise ToolError(f"not a file: {arguments.path}")
        lines = path.read_text(encoding="utf-8").splitlines()
        start = arguments.offset - 1
        selected = lines[start : start + arguments.limit]
        if not selected:
            return "(no lines in requested range)"
        return "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(
                selected,
                start=arguments.offset,
            )
        )


class WriteFileArguments(ToolArguments):
    """定义 write_file 的目标路径和文本内容参数。"""

    path: str = Field(min_length=1)
    content: str


class WriteFileTool(BaseTool):
    """在 workspace 内创建新文件，并拒绝覆盖现有目标。"""

    name: ClassVar[str] = "write_file"
    description: ClassVar[str] = (
        "Create a new UTF-8 text file inside the workspace. "
        "It refuses to overwrite existing files."
    )
    arguments_model: ClassVar[type[ToolArguments]] = WriteFileArguments

    def execute(
        self,
        arguments: WriteFileArguments,
        context: ToolContext,
    ) -> str:
        """创建必要的父目录并写入新的 UTF-8 文本文件。"""

        path = context.workspace.resolve(arguments.path)
        if path.exists():
            raise ToolError(
                f"refusing to overwrite existing file: {arguments.path}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(arguments.content, encoding="utf-8")
        return f"created {context.workspace.relative(path)} ({len(arguments.content)} chars)"


class EditFileArguments(ToolArguments):
    """定义 edit_file 的精确匹配文本、替换文本和多处替换选项。"""

    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    replace_all: bool = False


class EditFileTool(BaseTool):
    """对 workspace 内已有文件执行受控的精确文本替换。"""

    name: ClassVar[str] = "edit_file"
    description: ClassVar[str] = (
        "Replace exact text in an existing workspace file."
    )
    arguments_model: ClassVar[type[ToolArguments]] = EditFileArguments

    def execute(
        self,
        arguments: EditFileArguments,
        context: ToolContext,
    ) -> str:
        """确认匹配数量符合要求后写回替换后的 UTF-8 文本。"""

        path = context.workspace.resolve(arguments.path, must_exist=True)
        if not path.is_file():
            raise ToolError(f"not a file: {arguments.path}")
        original = path.read_text(encoding="utf-8")
        matches = original.count(arguments.old_text)
        if matches == 0:
            raise ToolError("old_text was not found")
        if matches > 1 and not arguments.replace_all:
            raise ToolError(
                f"old_text matched {matches} times; set replace_all=true"
            )
        count = -1 if arguments.replace_all else 1
        updated = original.replace(
            arguments.old_text,
            arguments.new_text,
            count,
        )
        path.write_text(updated, encoding="utf-8")
        replaced = matches if arguments.replace_all else 1
        return f"updated {context.workspace.relative(path)} ({replaced} replacement(s))"

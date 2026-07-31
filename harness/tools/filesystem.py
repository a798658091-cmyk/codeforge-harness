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
    path: str = Field(min_length=1)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=2000, ge=1, le=10000)


class ReadFileTool(BaseTool):
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
    path: str = Field(min_length=1)
    content: str


class WriteFileTool(BaseTool):
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
        path = context.workspace.resolve(arguments.path)
        if path.exists():
            raise ToolError(
                f"refusing to overwrite existing file: {arguments.path}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(arguments.content, encoding="utf-8")
        return f"created {context.workspace.relative(path)} ({len(arguments.content)} chars)"


class EditFileArguments(ToolArguments):
    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    replace_all: bool = False


class EditFileTool(BaseTool):
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

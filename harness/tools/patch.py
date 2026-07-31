from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from harness.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolError,
)


class PatchChange(ToolArguments):
    operation: Literal["add", "update"]
    path: str = Field(min_length=1)
    content: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    expected_replacements: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_operation(self) -> "PatchChange":
        if self.operation == "add" and self.content is None:
            raise ValueError("add requires content")
        if self.operation == "update":
            if not self.old_text:
                raise ValueError("update requires non-empty old_text")
            if self.new_text is None:
                raise ValueError("update requires new_text")
        return self


class ApplyPatchArguments(ToolArguments):
    changes: list[PatchChange] = Field(min_length=1, max_length=50)


@dataclass
class _PreparedChange:
    path: Path
    existed: bool
    original: str | None
    updated: str


class ApplyPatchTool(BaseTool):
    name: ClassVar[str] = "apply_patch"
    description: ClassVar[str] = (
        "Atomically add or update one or more workspace files using "
        "validated exact-text changes."
    )
    arguments_model: ClassVar[type[ToolArguments]] = ApplyPatchArguments

    def execute(
        self,
        arguments: ApplyPatchArguments,
        context: ToolContext,
    ) -> str:
        prepared: list[_PreparedChange] = []
        seen: set[Path] = set()
        for change in arguments.changes:
            path = context.workspace.resolve(change.path)
            if path in seen:
                raise ToolError(f"duplicate patch path: {change.path}")
            seen.add(path)
            if change.operation == "add":
                if path.exists():
                    raise ToolError(f"add target already exists: {change.path}")
                prepared.append(
                    _PreparedChange(
                        path=path,
                        existed=False,
                        original=None,
                        updated=change.content or "",
                    )
                )
                continue

            if not path.is_file():
                raise ToolError(f"update target is not a file: {change.path}")
            original = path.read_text(encoding="utf-8")
            assert change.old_text is not None
            assert change.new_text is not None
            matches = original.count(change.old_text)
            if matches != change.expected_replacements:
                raise ToolError(
                    f"{change.path}: expected {change.expected_replacements} "
                    f"match(es), found {matches}"
                )
            updated = original.replace(
                change.old_text,
                change.new_text,
                change.expected_replacements,
            )
            prepared.append(
                _PreparedChange(
                    path=path,
                    existed=True,
                    original=original,
                    updated=updated,
                )
            )

        written: list[_PreparedChange] = []
        try:
            for change in prepared:
                change.path.parent.mkdir(parents=True, exist_ok=True)
                change.path.write_text(change.updated, encoding="utf-8")
                written.append(change)
        except OSError:
            for change in reversed(written):
                if change.existed:
                    change.path.write_text(
                        change.original or "",
                        encoding="utf-8",
                    )
                elif change.path.exists():
                    change.path.unlink()
            raise

        names = [
            context.workspace.relative(change.path)
            for change in prepared
        ]
        return f"applied {len(prepared)} change(s): {', '.join(names)}"

from __future__ import annotations

from pathlib import Path

from harness.safety.workspace import Workspace
from harness.tools.base import ToolContext
from harness.tools.filesystem import (
    EditFileTool,
    ReadFileTool,
    WriteFileTool,
)
from harness.tools.patch import ApplyPatchTool
from harness.tools.registry import ToolRegistry
from harness.tools.search import SearchTool
from harness.tools.shell import ShellTool
from harness.tools.testing import RunTestsTool


def build_default_registry(workspace: str | Path) -> ToolRegistry:
    context = ToolContext(workspace=Workspace(Path(workspace)))
    return ToolRegistry(
        context,
        tools=[
            ReadFileTool(),
            WriteFileTool(),
            EditFileTool(),
            SearchTool(),
            ApplyPatchTool(),
            ShellTool(),
            RunTestsTool(),
        ],
    )


__all__ = ["ToolRegistry", "build_default_registry"]

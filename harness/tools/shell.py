from __future__ import annotations

import os
import subprocess
from typing import ClassVar

from pydantic import Field

from harness.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolError,
    ToolOutput,
)


class ShellArguments(ToolArguments):
    command: str = Field(min_length=1, max_length=10000)
    cwd: str = "."
    timeout_seconds: int = Field(default=60, ge=1, le=120)


class ShellTool(BaseTool):
    name: ClassVar[str] = "shell"
    description: ClassVar[str] = (
        "Run a shell command inside the workspace. "
        "A permanent hard-deny list blocks catastrophic commands."
    )
    arguments_model: ClassVar[type[ToolArguments]] = ShellArguments
    hard_deny = (
        "rm -rf /",
        "shutdown",
        "reboot",
        "mkfs",
        "format c:",
        "git reset --hard",
        "remove-item -recurse c:\\",
    )

    def execute(
        self,
        arguments: ShellArguments,
        context: ToolContext,
    ) -> ToolOutput:
        normalized = " ".join(arguments.command.lower().split())
        for pattern in self.hard_deny:
            if pattern in normalized:
                raise ToolError(
                    f"command blocked by hard deny rule: {pattern}"
                )
        cwd = context.workspace.resolve(arguments.cwd, must_exist=True)
        if not cwd.is_dir():
            raise ToolError(f"cwd is not a directory: {arguments.cwd}")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not any(
                marker in key.upper()
                for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
            )
        }
        try:
            completed = subprocess.run(
                arguments.command,
                shell=True,
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=arguments.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(
                f"command timed out after {arguments.timeout_seconds}s"
            ) from exc
        output = (completed.stdout + completed.stderr).strip()
        content = (
            f"exit_code={completed.returncode}\n"
            f"{output or '(no output)'}"
        )
        return ToolOutput(
            content=content[:100_000],
            success=completed.returncode == 0,
        )

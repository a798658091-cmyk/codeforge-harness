from __future__ import annotations

import subprocess
import sys
from typing import ClassVar

from pydantic import Field

from harness.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolError,
    ToolOutput,
)


class RunTestsArguments(ToolArguments):
    targets: list[str] = Field(default_factory=lambda: ["tests"])
    extra_args: list[str] = Field(default_factory=lambda: ["-q"])
    cwd: str = "."
    timeout_seconds: int = Field(default=120, ge=1, le=300)


class RunTestsTool(BaseTool):
    name: ClassVar[str] = "run_tests"
    description: ClassVar[str] = (
        "Run pytest using the current Python interpreter inside the workspace."
    )
    arguments_model: ClassVar[type[ToolArguments]] = RunTestsArguments

    def execute(
        self,
        arguments: RunTestsArguments,
        context: ToolContext,
    ) -> ToolOutput:
        cwd = context.workspace.resolve(arguments.cwd, must_exist=True)
        safe_targets = [
            str(context.workspace.resolve(target, must_exist=True))
            for target in arguments.targets
        ]
        command = [
            sys.executable,
            "-m",
            "pytest",
            *safe_targets,
            *arguments.extra_args,
            "-p",
            "no:cacheprovider",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=arguments.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(
                f"tests timed out after {arguments.timeout_seconds}s"
            ) from exc
        output = (completed.stdout + completed.stderr).strip()
        return ToolOutput(
            content=(
                f"exit_code={completed.returncode}\n"
                f"{output or '(no output)'}"
            )[:100_000],
            success=completed.returncode == 0,
        )

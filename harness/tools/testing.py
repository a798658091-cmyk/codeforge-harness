"""实现供 Agent 调用的 workspace 内 pytest 测试工具。

任务流位置：模型要求验证代码时由 Tool Registry 调用本模块；本模块使用当前
Python 解释器启动 pytest 子进程，并把退出码和测试输出返回 Agent Loop。
"""

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
    """定义 pytest 目标、附加参数、工作目录和超时时间。"""

    targets: list[str] = Field(default_factory=lambda: ["tests"])
    extra_args: list[str] = Field(default_factory=lambda: ["-q"])
    cwd: str = "."
    timeout_seconds: int = Field(default=120, ge=1, le=300)


class RunTestsTool(BaseTool):
    """使用当前 Python 解释器在 workspace 内运行 pytest。"""

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
        """解析安全测试目标，启动 pytest 并返回退出码和输出。"""

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

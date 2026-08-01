"""实现受 workspace、超时、环境变量过滤和 hard-deny 约束的 Shell 工具。

任务流位置：模型的 shell 调用经 Tool Registry 分发到这里；命令通过安全检查后
在 workspace 子目录执行，stdout、stderr 和退出码再返回 Agent Loop。
"""

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
    """定义 Shell 命令、工作目录和超时时间参数。"""

    command: str = Field(min_length=1, max_length=10000)
    cwd: str = "."
    timeout_seconds: int = Field(default=60, ge=1, le=120)


class ShellTool(BaseTool):
    """在 workspace 工作目录内执行带基础安全保护的通用命令。"""

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
        """检查命令和 cwd，过滤敏感环境变量并返回子进程输出。"""

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

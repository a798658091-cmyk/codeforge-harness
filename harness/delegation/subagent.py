"""实现同步、隔离且只能读取 workspace 的最小 Subagent。

任务流位置：主 Agent 调用 delegate_readonly 后，本模块创建独立 Agent Loop 和
只读 Tool Registry；子 Agent 完成调查后只返回文本结果，不修改文件或启动 Shell。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from harness.agent.loop import AgentLoop
from harness.context.memory import MemorySearchTool, MemoryStore
from harness.context.skills import (
    ListSkillsTool,
    ReadSkillTool,
    SkillRegistry,
)
from harness.delegation.protocols import SubagentResult
from harness.providers.base import ModelProvider
from harness.safety.audit import AuditLogger
from harness.safety.permissions import PermissionPolicy
from harness.safety.workspace import Workspace
from harness.tools.base import BaseTool, ToolArguments, ToolContext, ToolError
from harness.tools.filesystem import ReadFileTool
from harness.tools.registry import ToolRegistry
from harness.tools.search import SearchTool


READONLY_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search",
        "list_skills",
        "read_skill",
        "memory_search",
    }
)


class ReadonlySubagentRunner:
    """使用共享 Provider 依次执行一个无写权限的隔离子任务。"""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        workspace: str | Path,
        skill_registry: SkillRegistry | None = None,
        memory_store: MemoryStore | None = None,
        audit_logger: AuditLogger | None = None,
        default_max_steps: int = 6,
    ) -> None:
        """注入 Provider、工作区和只读上下文依赖。"""

        if not 1 <= default_max_steps <= 10:
            raise ValueError("subagent max steps must be between 1 and 10")
        self.provider = provider
        self.workspace = Workspace(Path(workspace))
        self.skill_registry = skill_registry or SkillRegistry()
        self.memory_store = memory_store
        self.audit_logger = audit_logger
        self.default_max_steps = default_max_steps

    def run(
        self,
        task: str,
        *,
        max_steps: int | None = None,
    ) -> SubagentResult:
        """为一个研究任务创建独立只读循环并返回精简结果。"""

        normalized_task = task.strip()
        if not normalized_task:
            raise ValueError("subagent task cannot be empty")
        resolved_max_steps = max_steps or self.default_max_steps
        if not 1 <= resolved_max_steps <= 10:
            raise ValueError("subagent max steps must be between 1 and 10")
        registry = self._build_registry()
        loop = AgentLoop(
            provider=self.provider,
            registry=registry,
            system_prompt=(
                "You are a read-only CodeForge research subagent. "
                f"The workspace is {self.workspace.root}. "
                "Inspect files, search code, read skills, and search verified "
                "memory only when useful. You cannot modify files, run shell "
                "commands, update Todo, create another subagent, or request "
                "more authority. Return concise evidence and file paths to "
                "the parent agent."
            ),
            max_steps=resolved_max_steps,
        )
        result = loop.run(normalized_task)
        return SubagentResult(
            subagent_id=uuid.uuid4().hex[:12],
            answer=result.answer,
            steps=result.state.steps,
            tool_calls=result.state.tool_calls,
            tool_failures=result.state.tool_failures,
        )

    def _build_registry(self) -> ToolRegistry:
        """构造只包含读取、搜索、Skills 和 Memory 查询的注册表。"""

        context = ToolContext(workspace=self.workspace)
        tools: list[BaseTool] = [
            ReadFileTool(),
            SearchTool(),
            ListSkillsTool(self.skill_registry),
            ReadSkillTool(self.skill_registry),
        ]
        if self.memory_store is not None:
            tools.append(MemorySearchTool(self.memory_store))
        policy = PermissionPolicy(
            {name: "allow" for name in READONLY_TOOL_NAMES},
            default="deny",
        )
        return ToolRegistry(
            context,
            tools=tools,
            permission_policy=policy,
            audit_logger=self.audit_logger,
        )


class DelegateReadonlyArguments(ToolArguments):
    """定义主 Agent 委派的研究任务和子循环步数。"""

    task: str = Field(min_length=1, max_length=10000)
    max_steps: int | None = Field(default=None, ge=1, le=10)


class DelegateReadonlyTool(BaseTool):
    """让主 Agent 同步委派一个不会修改 workspace 的调查任务。"""

    name: ClassVar[str] = "delegate_readonly"
    description: ClassVar[str] = (
        "Delegate one bounded read-only investigation to an isolated subagent. "
        "It cannot write files, run shell commands, or delegate again."
    )
    arguments_model: ClassVar[type[ToolArguments]] = DelegateReadonlyArguments

    def __init__(self, runner: ReadonlySubagentRunner | None) -> None:
        """绑定可选的只读 Subagent 运行器。"""

        self.runner = runner

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """运行子任务并返回主 Agent 可消费的 JSON 结果。"""

        if not isinstance(arguments, DelegateReadonlyArguments):
            raise TypeError("delegate_readonly received unexpected arguments")
        if self.runner is None:
            raise ToolError("read-only subagent is disabled")
        result = self.runner.run(
            arguments.task,
            max_steps=arguments.max_steps,
        )
        return json.dumps(
            {
                "subagent_id": result.subagent_id,
                "answer": result.answer,
                "steps": result.steps,
                "tool_calls": result.tool_calls,
                "tool_failures": result.tool_failures,
                "mode": "read_only",
            },
            ensure_ascii=False,
            indent=2,
        )


__all__ = [
    "DelegateReadonlyTool",
    "READONLY_TOOL_NAMES",
    "ReadonlySubagentRunner",
]

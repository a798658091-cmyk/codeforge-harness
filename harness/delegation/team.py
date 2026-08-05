"""实现异步可写 Subagent 管理器及主 Agent 可调用的团队工具。

任务流位置：主 Agent 通过 subagent_spawn 派发任务；本模块为 Worker 绑定独立
Worktree、受限工具集和 Agent Loop，并把状态发布到 MessageBus。完成后的提交
只有在主 Agent 调用 subagent_integrate 且通过权限审批后才进入主工作区。
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import (
    CancelledError,
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from harness.agent.loop import (
    AgentLoop,
    AgentLoopCancelledError,
)
from harness.context.skills import ListSkillsTool, ReadSkillTool, SkillRegistry
from harness.delegation.message_bus import MessageBus
from harness.providers.base import ModelProvider
from harness.safety.audit import AuditLogger
from harness.safety.permissions import PermissionPolicy
from harness.safety.workspace import Workspace
from harness.tasks.todo import TodoList, TodoReadTool, TodoWriteTool
from harness.tools.base import BaseTool, ToolArguments, ToolContext, ToolError
from harness.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
from harness.tools.patch import ApplyPatchTool
from harness.tools.registry import ToolRegistry
from harness.tools.search import SearchTool
from harness.tools.shell import ShellTool
from harness.tools.testing import RunTestsTool
from harness.worktrees import WorktreeManager


class WritableSubagentStatus(str, Enum):
    """表示可写 Subagent 从排队到集成的稳定生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTEGRATED = "integrated"


@dataclass
class WritableSubagentTask:
    """保存一个异步 Worker 的任务、隔离位置、结果和运行指标。"""

    id: str
    task: str
    worktree_path: str
    branch: str
    max_steps: int
    status: WritableSubagentStatus = WritableSubagentStatus.QUEUED
    answer: str = ""
    error: str | None = None
    commit: str | None = None
    steps: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    created_at: str = field(default_factory=lambda: _utc_now())
    started_at: str | None = None
    finished_at: str | None = None
    cancel_event: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )

    def to_dict(self) -> dict[str, object]:
        """移除内部 Event，并把枚举转换成适合 JSON 的文本。"""

        return {
            "id": self.id,
            "task": self.task,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "max_steps": self.max_steps,
            "status": self.status.value,
            "answer": self.answer,
            "error": self.error,
            "commit": self.commit,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class WritableSubagentManager:
    """最多并发运行少量隔离 Worker，并管理查询、取消和集成。"""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        workspace: str | Path,
        worktrees: WorktreeManager,
        message_bus: MessageBus,
        audit_logger: AuditLogger | None = None,
        max_workers: int = 2,
    ) -> None:
        """注入共享 Provider、Git 隔离器、事件总线和并发上限。"""

        if not 1 <= max_workers <= 4:
            raise ValueError("max_workers must be between 1 and 4")
        self.provider = provider
        self.workspace = Path(workspace).resolve()
        self.worktrees = worktrees
        self.message_bus = message_bus
        self.audit_logger = audit_logger
        self.max_workers = max_workers
        self._tasks: dict[str, WritableSubagentTask] = {}
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="codeforge-subagent",
        )

    def spawn(self, task: str, *, max_steps: int = 12) -> WritableSubagentTask:
        """同步创建 Worktree，再异步启动一个具备写入能力的 Worker。"""

        normalized_task = task.strip()
        if not normalized_task:
            raise ValueError("subagent task cannot be empty")
        if not 1 <= max_steps <= 30:
            raise ValueError("subagent max steps must be between 1 and 30")
        subagent_id = uuid.uuid4().hex[:12]
        worktree = self.worktrees.create(subagent_id)
        child = WritableSubagentTask(
            id=subagent_id,
            task=normalized_task,
            worktree_path=str(worktree.path),
            branch=worktree.branch,
            max_steps=max_steps,
        )
        self.message_bus.publish(
            "worktree.created",
            f"worktree:{subagent_id}",
            worktree.to_dict(),
        )
        with self._lock:
            self._tasks[subagent_id] = child
        self._publish(child, "subagent.queued")
        with self._lock:
            self._futures[subagent_id] = self._executor.submit(
                self._run,
                subagent_id,
            )
        return self.status(subagent_id)

    def status(self, subagent_id: str) -> WritableSubagentTask:
        """返回指定 Worker 的线程安全状态快照。"""

        with self._lock:
            return self._copy(self._require(subagent_id))

    def wait(
        self,
        subagent_id: str,
        *,
        timeout_seconds: int,
    ) -> WritableSubagentTask:
        """最多等待指定秒数，终态出现或超时后返回最新快照。"""

        if not 0 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        with self._lock:
            self._require(subagent_id)
            future = self._futures.get(subagent_id)
        if future is not None and timeout_seconds > 0:
            try:
                future.result(timeout=timeout_seconds)
            except (FutureTimeoutError, CancelledError):
                pass
        return self.status(subagent_id)

    def list(self) -> list[WritableSubagentTask]:
        """按创建顺序返回当前进程派生的全部 Worker。"""

        with self._lock:
            return [self._copy(task) for task in self._tasks.values()]

    def cancel(self, subagent_id: str) -> WritableSubagentTask:
        """请求取消排队或运行中的 Worker，并在安全边界协作停止。"""

        with self._lock:
            task = self._require(subagent_id)
            if task.status not in {
                WritableSubagentStatus.QUEUED,
                WritableSubagentStatus.RUNNING,
            }:
                return self._copy(task)
            task.cancel_event.set()
            future = self._futures.get(subagent_id)
            if future is not None and future.cancel():
                task.status = WritableSubagentStatus.CANCELLED
                task.finished_at = _utc_now()
                publish_now = True
            else:
                publish_now = False
        self._publish(task, "subagent.cancel_requested")
        if publish_now:
            self._publish(task, "subagent.cancelled")
        return self.status(subagent_id)

    def diff(self, subagent_id: str) -> str:
        """取得指定 Worker 相对创建基线的完整 Git 补丁。"""

        with self._lock:
            self._require(subagent_id)
        return self.worktrees.diff(subagent_id)

    def integrate(self, subagent_id: str) -> WritableSubagentTask:
        """仅允许把已完成且有提交的 Worker 变更集成回主分支。"""

        with self._lock:
            task = self._require(subagent_id)
            if task.status is not WritableSubagentStatus.COMPLETED:
                raise ToolError(
                    "only a completed subagent can be integrated; "
                    f"current status={task.status.value}"
                )
            if task.commit is None:
                raise ToolError("subagent completed without file changes")
        commit = self.worktrees.integrate(subagent_id)
        self.message_bus.publish(
            "worktree.integrated",
            f"worktree:{subagent_id}",
            {"subagent_id": subagent_id, "commit": commit},
        )
        with self._lock:
            task = self._require(subagent_id)
            task.status = WritableSubagentStatus.INTEGRATED
            task.commit = commit
            snapshot = self._copy(task)
        self._publish(task, "subagent.integrated")
        return snapshot

    def shutdown(self, *, cancel_running: bool = True) -> None:
        """CLI 退出时请求停止所有 Worker，并等待线程释放 Provider。"""

        if cancel_running:
            with self._lock:
                active_ids = [
                    task.id
                    for task in self._tasks.values()
                    if task.status
                    in {
                        WritableSubagentStatus.QUEUED,
                        WritableSubagentStatus.RUNNING,
                    }
                ]
            for subagent_id in active_ids:
                self.cancel(subagent_id)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _run(self, subagent_id: str) -> None:
        """在线程中运行隔离 Agent Loop，随后提交或记录明确终态。"""

        with self._lock:
            task = self._require(subagent_id)
            if task.cancel_event.is_set():
                task.status = WritableSubagentStatus.CANCELLED
                task.finished_at = _utc_now()
                self._publish(task, "subagent.cancelled")
                return
            task.status = WritableSubagentStatus.RUNNING
            task.started_at = _utc_now()
            task_text = task.task
            max_steps = task.max_steps
            worktree_path = Path(task.worktree_path)
        self._publish(task, "subagent.running")
        try:
            registry = self._build_registry(worktree_path)
            loop = AgentLoop(
                provider=self.provider,
                registry=registry,
                system_prompt=(
                    "You are a writable CodeForge worker in an isolated Git "
                    f"worktree at {worktree_path}. Complete only the delegated "
                    "task. Inspect before editing, keep a short Todo for "
                    "multi-step work, run focused tests when useful, and return "
                    "a concise result. You cannot create subagents, access "
                    "parent memory, MCP, notifications, or background jobs."
                ),
                max_steps=max_steps,
                cancel_check=task.cancel_event.is_set,
            )
            result = loop.run(task_text)
            if task.cancel_event.is_set():
                raise AgentLoopCancelledError("subagent was cancelled")
            commit = self.worktrees.commit(
                subagent_id,
                f"CodeForge subagent {subagent_id}: {task_text[:120]}",
            )
            self.message_bus.publish(
                "worktree.committed" if commit else "worktree.no_changes",
                f"worktree:{subagent_id}",
                {"subagent_id": subagent_id, "commit": commit},
            )
            with self._lock:
                task = self._require(subagent_id)
                task.answer = result.answer
                task.commit = commit
                task.steps = result.state.steps
                task.tool_calls = result.state.tool_calls
                task.tool_failures = result.state.tool_failures
                task.status = WritableSubagentStatus.COMPLETED
                task.finished_at = _utc_now()
            self._publish(task, "subagent.completed")
        except AgentLoopCancelledError:
            with self._lock:
                task = self._require(subagent_id)
                task.status = WritableSubagentStatus.CANCELLED
                task.finished_at = _utc_now()
            self._publish(task, "subagent.cancelled")
        except Exception as exc:
            with self._lock:
                task = self._require(subagent_id)
                task.status = WritableSubagentStatus.FAILED
                task.error = f"{type(exc).__name__}: {exc}"
                task.finished_at = _utc_now()
            self._publish(task, "subagent.failed")

    def _build_registry(self, worktree_path: Path) -> ToolRegistry:
        """为 Worker 构造无 Memory、MCP、后台任务和递归委派的工具白名单。"""

        skills = SkillRegistry.discover(worktree_path)
        tools: list[BaseTool] = [
            ReadFileTool(),
            WriteFileTool(),
            EditFileTool(),
            SearchTool(),
            ApplyPatchTool(),
            ShellTool(),
            RunTestsTool(),
            TodoReadTool(),
            TodoWriteTool(),
            ListSkillsTool(skills),
            ReadSkillTool(skills),
        ]
        allowed = {tool.name: "allow" for tool in tools}
        return ToolRegistry(
            ToolContext(
                workspace=Workspace(worktree_path),
                todo_list=TodoList(),
            ),
            tools=tools,
            permission_policy=PermissionPolicy(allowed, default="deny"),
            audit_logger=self.audit_logger,
        )

    def _publish(self, task: WritableSubagentTask, event_type: str) -> None:
        """把任务精简快照发布到共享 MessageBus。"""

        with self._lock:
            snapshot = task.to_dict()
        snapshot.pop("task", None)
        self.message_bus.publish(
            event_type,
            f"subagent:{task.id}",
            snapshot,
        )

    def _require(self, subagent_id: str) -> WritableSubagentTask:
        """取得内部任务对象；调用方必须已持有锁或仅作只读访问。"""

        task = self._tasks.get(subagent_id)
        if task is None:
            raise ToolError(f"subagent not found: {subagent_id}")
        return task

    @staticmethod
    def _copy(task: WritableSubagentTask) -> WritableSubagentTask:
        """创建状态副本，并为副本复制取消标记而非共享内部 Event。"""

        copied = WritableSubagentTask(
            **{
                key: value
                for key, value in task.to_dict().items()
                if key not in {"status", "cancel_event"}
            },
            status=task.status,
        )
        if task.cancel_event.is_set():
            copied.cancel_event.set()
        return copied


class SubagentSpawnArguments(ToolArguments):
    """定义可写 Worker 的任务说明和最大模型回合数。"""

    task: str = Field(min_length=1, max_length=20_000)
    max_steps: int = Field(default=12, ge=1, le=30)


class SubagentIdArguments(ToolArguments):
    """定义状态、差异、取消和集成工具共用的 Subagent 标识。"""

    subagent_id: str = Field(min_length=1, max_length=64)


class SubagentStatusArguments(SubagentIdArguments):
    """定义状态查询可选的阻塞等待时间，减少无意义快速轮询。"""

    wait_seconds: int = Field(default=0, ge=0, le=60)


class SubagentListArguments(ToolArguments):
    """定义 subagent_list 的空参数对象。"""


class SubagentSpawnTool(BaseTool):
    """异步启动一个位于独立 Git Worktree 的可写 Worker。"""

    name: ClassVar[str] = "subagent_spawn"
    description: ClassVar[str] = (
        "Start an asynchronous writable worker in an isolated Git worktree. "
        "Returns immediately with subagent_id; poll status before integration."
    )
    arguments_model: ClassVar[type[ToolArguments]] = SubagentSpawnArguments

    def __init__(self, manager: WritableSubagentManager | None) -> None:
        """绑定可选团队管理器。"""

        self.manager = manager

    def execute(self, arguments: ToolArguments, context: ToolContext) -> str:
        """启动 Worker 并返回初始任务快照。"""

        if not isinstance(arguments, SubagentSpawnArguments):
            raise TypeError("subagent_spawn received unexpected arguments")
        task = _require_manager(self.manager).spawn(
            arguments.task,
            max_steps=arguments.max_steps,
        )
        return _render(task)


class SubagentStatusTool(BaseTool):
    """查询一个可写 Worker 的进度、答案、提交和错误。"""

    name: ClassVar[str] = "subagent_status"
    description: ClassVar[str] = (
        "Get status and result for a writable subagent. Optionally wait up to "
        "60 seconds so the worker can finish without rapid polling."
    )
    arguments_model: ClassVar[type[ToolArguments]] = SubagentStatusArguments

    def __init__(self, manager: WritableSubagentManager | None) -> None:
        """绑定可选团队管理器。"""

        self.manager = manager

    def execute(self, arguments: ToolArguments, context: ToolContext) -> str:
        """返回指定 Worker 的当前状态 JSON。"""

        if not isinstance(arguments, SubagentStatusArguments):
            raise TypeError("subagent_status received unexpected arguments")
        return _render(
            _require_manager(self.manager).wait(
                arguments.subagent_id,
                timeout_seconds=arguments.wait_seconds,
            )
        )


class SubagentListTool(BaseTool):
    """列出当前进程创建的全部可写 Worker。"""

    name: ClassVar[str] = "subagent_list"
    description: ClassVar[str] = "List all writable subagents for this run."
    arguments_model: ClassVar[type[ToolArguments]] = SubagentListArguments

    def __init__(self, manager: WritableSubagentManager | None) -> None:
        """绑定可选团队管理器。"""

        self.manager = manager

    def execute(self, arguments: ToolArguments, context: ToolContext) -> str:
        """返回全部 Worker 的 JSON 数组。"""

        tasks = _require_manager(self.manager).list()
        return json.dumps(
            [task.to_dict() for task in tasks],
            ensure_ascii=False,
            indent=2,
        )


class SubagentCancelTool(BaseTool):
    """请求取消一个排队或运行中的可写 Worker。"""

    name: ClassVar[str] = "subagent_cancel"
    description: ClassVar[str] = "Request cancellation of a writable subagent."
    arguments_model: ClassVar[type[ToolArguments]] = SubagentIdArguments

    def __init__(self, manager: WritableSubagentManager | None) -> None:
        """绑定可选团队管理器。"""

        self.manager = manager

    def execute(self, arguments: ToolArguments, context: ToolContext) -> str:
        """发出取消请求并返回最新状态。"""

        checked = _checked_id(arguments, self.name)
        return _render(_require_manager(self.manager).cancel(checked.subagent_id))


class SubagentDiffTool(BaseTool):
    """读取可写 Worker 相对创建基线产生的 Git 补丁。"""

    name: ClassVar[str] = "subagent_diff"
    description: ClassVar[str] = (
        "Read the Git diff produced by a writable subagent before integration."
    )
    arguments_model: ClassVar[type[ToolArguments]] = SubagentIdArguments

    def __init__(self, manager: WritableSubagentManager | None) -> None:
        """绑定可选团队管理器。"""

        self.manager = manager

    def execute(self, arguments: ToolArguments, context: ToolContext) -> str:
        """返回指定 Worker 的补丁文本。"""

        checked = _checked_id(arguments, self.name)
        return _require_manager(self.manager).diff(checked.subagent_id)


class SubagentIntegrateTool(BaseTool):
    """经主 Agent 权限链批准后集成一个 Worker 的提交。"""

    name: ClassVar[str] = "subagent_integrate"
    description: ClassVar[str] = (
        "Cherry-pick a completed writable subagent commit into the main "
        "workspace. The main workspace must be clean."
    )
    arguments_model: ClassVar[type[ToolArguments]] = SubagentIdArguments

    def __init__(self, manager: WritableSubagentManager | None) -> None:
        """绑定可选团队管理器。"""

        self.manager = manager

    def execute(self, arguments: ToolArguments, context: ToolContext) -> str:
        """集成提交并返回最终任务状态。"""

        checked = _checked_id(arguments, self.name)
        return _render(
            _require_manager(self.manager).integrate(checked.subagent_id)
        )


def _checked_id(arguments: ToolArguments, tool_name: str) -> SubagentIdArguments:
    """确认共用参数模型类型，避免工具实现收到意外对象。"""

    if not isinstance(arguments, SubagentIdArguments):
        raise TypeError(f"{tool_name} received unexpected arguments")
    return arguments


def _require_manager(
    manager: WritableSubagentManager | None,
) -> WritableSubagentManager:
    """取得团队管理器，CLI 未启用时返回清晰工具错误。"""

    if manager is None:
        raise ToolError("writable subagents are disabled")
    return manager


def _render(task: WritableSubagentTask) -> str:
    """把 Worker 快照渲染为格式化 JSON。"""

    return json.dumps(task.to_dict(), ensure_ascii=False, indent=2)


def _utc_now() -> str:
    """生成带时区的 UTC ISO 时间。"""

    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "SubagentCancelTool",
    "SubagentDiffTool",
    "SubagentIntegrateTool",
    "SubagentListTool",
    "SubagentSpawnTool",
    "SubagentStatusTool",
    "WritableSubagentManager",
    "WritableSubagentStatus",
    "WritableSubagentTask",
]

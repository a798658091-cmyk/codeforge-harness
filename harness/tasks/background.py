"""实现受 workspace 约束的后台 Shell 作业及其 Agent 工具。

任务流位置：模型通过 background_start 启动非阻塞命令，再用 status/output/cancel
管理作业；作业进入终态时发布通知，CLI 退出时终止仍在运行的子进程树。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from harness.safety.workspace import Workspace
from harness.tasks.notifications import NotificationCenter
from harness.tools.base import BaseTool, ToolArguments, ToolContext, ToolError
from harness.tools.shell import ShellTool


class BackgroundJobStatus(str, Enum):
    """表示后台作业从运行到终止的稳定状态。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass
class BackgroundJob:
    """保存一个后台子进程及其可展示的运行元数据。"""

    id: str
    command: str
    cwd: str
    log_path: Path
    process: subprocess.Popen[bytes]
    timeout_seconds: int
    started_at: str
    status: BackgroundJobStatus = BackgroundJobStatus.RUNNING
    return_code: int | None = None
    finished_at: str | None = None


class BackgroundJobManager:
    """负责启动、观察、读取和终止当前进程拥有的后台作业。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        notifications: NotificationCenter | None = None,
        max_running_jobs: int = 4,
    ) -> None:
        """绑定 workspace、通知中心和最大并发作业数。"""

        if max_running_jobs < 1:
            raise ValueError("max_running_jobs must be at least 1")
        self.workspace = Workspace(Path(workspace))
        self.notifications = notifications
        self.max_running_jobs = max_running_jobs
        self._jobs: dict[str, BackgroundJob] = {}
        self._watchers: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    def start(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout_seconds: int = 600,
    ) -> BackgroundJob:
        """安全检查后启动后台命令，立即返回 job_id。"""

        normalized_command = command.strip()
        if not normalized_command:
            raise ValueError("background command cannot be empty")
        if not 1 <= timeout_seconds <= 3600:
            raise ValueError("background timeout must be between 1 and 3600")
        _check_hard_deny(normalized_command)
        safe_cwd = self.workspace.resolve(cwd, must_exist=True)
        if not safe_cwd.is_dir():
            raise NotADirectoryError(f"not a directory: {cwd}")

        with self._lock:
            running = sum(
                job.status is BackgroundJobStatus.RUNNING
                for job in self._jobs.values()
            )
            if running >= self.max_running_jobs:
                raise ToolError(
                    "maximum number of running background jobs reached"
                )
            job_id = uuid.uuid4().hex[:12]
            log_path = self.workspace.resolve(
                f".codeforge/background/{job_id}.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            environment = _filtered_environment()
            popen_options: dict[str, object] = {
                "shell": True,
                "cwd": safe_cwd,
                "env": environment,
                "stderr": subprocess.STDOUT,
            }
            if os.name == "nt":
                popen_options["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                popen_options["start_new_session"] = True
            with log_path.open("wb") as log_stream:
                process = subprocess.Popen(
                    normalized_command,
                    stdout=log_stream,
                    **popen_options,
                )
            job = BackgroundJob(
                id=job_id,
                command=normalized_command,
                cwd=self.workspace.relative(safe_cwd) or ".",
                log_path=log_path,
                process=process,
                timeout_seconds=timeout_seconds,
                started_at=_utc_now(),
            )
            self._jobs[job_id] = job
            watcher = threading.Thread(
                target=self._watch,
                args=(job_id,),
                name=f"codeforge-bg-{job_id}",
                daemon=True,
            )
            self._watchers[job_id] = watcher
            watcher.start()
            return job

    def status(self, job_id: str) -> BackgroundJob:
        """返回指定作业的当前状态快照。"""

        with self._lock:
            return self._require_job(job_id)

    def output(self, job_id: str, *, last_characters: int = 20000) -> str:
        """读取后台日志末尾的 UTF-8 文本，并容忍命令的非法字节。"""

        if not 1 <= last_characters <= 100000:
            raise ValueError(
                "last_characters must be between 1 and 100000"
            )
        with self._lock:
            job = self._require_job(job_id)
            log_path = job.log_path
        try:
            data = log_path.read_bytes()
        except FileNotFoundError:
            return "(no output yet)"
        text = data.decode("utf-8", errors="replace")
        return text[-last_characters:] or "(no output yet)"

    def cancel(self, job_id: str) -> BackgroundJob:
        """终止指定作业的进程树，并发布取消通知。"""

        with self._lock:
            job = self._require_job(job_id)
            if job.status is not BackgroundJobStatus.RUNNING:
                return job
        self._terminate(job_id, BackgroundJobStatus.CANCELLED)
        with self._lock:
            return self._require_job(job_id)

    def list(self) -> list[BackgroundJob]:
        """按启动顺序返回当前进程创建的全部作业。"""

        with self._lock:
            return list(self._jobs.values())

    def shutdown(self, *, cancel_running: bool = True) -> None:
        """在 CLI 退出时清理运行中作业，并短暂等待观察线程收尾。"""

        if cancel_running:
            with self._lock:
                running_ids = [
                    job.id
                    for job in self._jobs.values()
                    if job.status is BackgroundJobStatus.RUNNING
                ]
            for job_id in running_ids:
                self._terminate(job_id, BackgroundJobStatus.CANCELLED)
        with self._lock:
            watchers = list(self._watchers.values())
        for watcher in watchers:
            watcher.join(timeout=1.0)

    def _watch(self, job_id: str) -> None:
        """等待一个作业完成或超时，并把终态同步到通知中心。"""

        with self._lock:
            job = self._require_job(job_id)
            process = job.process
            timeout = job.timeout_seconds
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate(job_id, BackgroundJobStatus.TIMED_OUT)
            return
        with self._lock:
            current = self._require_job(job_id)
            if current.status is not BackgroundJobStatus.RUNNING:
                return
            terminal_status = (
                BackgroundJobStatus.SUCCEEDED
                if return_code == 0
                else BackgroundJobStatus.FAILED
            )
            current.status = terminal_status
            current.return_code = return_code
            current.finished_at = _utc_now()
        self._notify(current)

    def _terminate(
        self,
        job_id: str,
        terminal_status: BackgroundJobStatus,
    ) -> None:
        """终止仍在运行的进程树，并以指定终态完成作业。"""

        with self._lock:
            job = self._require_job(job_id)
            if job.status is not BackgroundJobStatus.RUNNING:
                return
            process = job.process
            # 先占有终态，避免观察线程在进程被终止后抢先写成 failed。
            job.status = terminal_status
            # 持锁完成终止和通知，使外部看见终态时相关元数据也已经完整。
            _terminate_process_tree(process)
            try:
                return_code = process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait(timeout=2.0)
            current = self._require_job(job_id)
            current.return_code = return_code
            current.finished_at = _utc_now()
            self._notify(current)

    def _notify(self, job: BackgroundJob) -> None:
        """根据作业终态发布一次成功、失败、超时或取消通知。"""

        if self.notifications is None:
            return
        level_map = {
            BackgroundJobStatus.SUCCEEDED: "success",
            BackgroundJobStatus.FAILED: "error",
            BackgroundJobStatus.CANCELLED: "warning",
            BackgroundJobStatus.TIMED_OUT: "error",
        }
        self.notifications.emit(
            level=level_map[job.status],
            title=f"Background job {job.status.value}",
            message=(
                f"job_id={job.id}, return_code={job.return_code}, "
                f"command={job.command[:500]}"
            ),
            source="background_shell",
        )

    def _require_job(self, job_id: str) -> BackgroundJob:
        """取得指定作业，不存在时抛出工具错误。"""

        job = self._jobs.get(job_id)
        if job is None:
            raise ToolError(f"background job not found: {job_id}")
        return job


class BackgroundStartArguments(ToolArguments):
    """定义后台命令、工作目录和最长运行时间。"""

    command: str = Field(min_length=1, max_length=10000)
    cwd: str = "."
    timeout_seconds: int = Field(default=600, ge=1, le=3600)


class BackgroundJobArguments(ToolArguments):
    """定义查询或取消后台作业所需的 job_id。"""

    job_id: str = Field(min_length=1, max_length=80)


class BackgroundOutputArguments(BackgroundJobArguments):
    """定义读取后台日志时的尾部字符数。"""

    last_characters: int = Field(default=20000, ge=1, le=100000)


class BackgroundStartTool(BaseTool):
    """让 Agent 启动不阻塞模型循环的 Shell 作业。"""

    name: ClassVar[str] = "background_start"
    description: ClassVar[str] = (
        "Start a non-blocking shell job inside the workspace and return job_id."
    )
    arguments_model: ClassVar[type[ToolArguments]] = BackgroundStartArguments

    def __init__(self, manager: BackgroundJobManager | None) -> None:
        """绑定可选后台作业管理器。"""

        self.manager = manager

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """启动作业并立即返回状态 JSON。"""

        if not isinstance(arguments, BackgroundStartArguments):
            raise TypeError("background_start received unexpected arguments")
        job = _require_manager(self.manager).start(
            arguments.command,
            cwd=arguments.cwd,
            timeout_seconds=arguments.timeout_seconds,
        )
        return json.dumps(_job_to_dict(job), ensure_ascii=False, indent=2)


class BackgroundStatusTool(BaseTool):
    """让 Agent 查询后台作业是否仍在运行。"""

    name: ClassVar[str] = "background_status"
    description: ClassVar[str] = "Get status and metadata for a background job."
    arguments_model: ClassVar[type[ToolArguments]] = BackgroundJobArguments

    def __init__(self, manager: BackgroundJobManager | None) -> None:
        """绑定可选后台作业管理器。"""

        self.manager = manager

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """返回指定作业的状态 JSON。"""

        if not isinstance(arguments, BackgroundJobArguments):
            raise TypeError("background_status received unexpected arguments")
        job = _require_manager(self.manager).status(arguments.job_id)
        return json.dumps(_job_to_dict(job), ensure_ascii=False, indent=2)


class BackgroundOutputTool(BaseTool):
    """让 Agent 非阻塞读取后台作业日志。"""

    name: ClassVar[str] = "background_output"
    description: ClassVar[str] = "Read the tail of a background job log."
    arguments_model: ClassVar[type[ToolArguments]] = BackgroundOutputArguments

    def __init__(self, manager: BackgroundJobManager | None) -> None:
        """绑定可选后台作业管理器。"""

        self.manager = manager

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """读取指定作业日志末尾。"""

        if not isinstance(arguments, BackgroundOutputArguments):
            raise TypeError("background_output received unexpected arguments")
        return _require_manager(self.manager).output(
            arguments.job_id,
            last_characters=arguments.last_characters,
        )


class BackgroundCancelTool(BaseTool):
    """让 Agent 主动终止不再需要的后台作业。"""

    name: ClassVar[str] = "background_cancel"
    description: ClassVar[str] = "Cancel a running background job and its process tree."
    arguments_model: ClassVar[type[ToolArguments]] = BackgroundJobArguments

    def __init__(self, manager: BackgroundJobManager | None) -> None:
        """绑定可选后台作业管理器。"""

        self.manager = manager

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """终止指定作业并返回终态 JSON。"""

        if not isinstance(arguments, BackgroundJobArguments):
            raise TypeError("background_cancel received unexpected arguments")
        job = _require_manager(self.manager).cancel(arguments.job_id)
        return json.dumps(_job_to_dict(job), ensure_ascii=False, indent=2)


def _require_manager(
    manager: BackgroundJobManager | None,
) -> BackgroundJobManager:
    """取得后台管理器，未配置时返回工具错误。"""

    if manager is None:
        raise ToolError("background shell is disabled")
    return manager


def _check_hard_deny(command: str) -> None:
    """复用同步 Shell 的永久禁止模式拦截灾难性命令。"""

    normalized = " ".join(command.lower().split())
    for pattern in ShellTool.hard_deny:
        if pattern in normalized:
            raise ToolError(
                f"command blocked by hard deny rule: {pattern}"
            )


def _filtered_environment() -> dict[str, str]:
    """移除名称疑似凭据的环境变量，避免泄漏到后台进程。"""

    return {
        key: value
        for key, value in os.environ.items()
        if not any(
            marker in key.upper()
            for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
        )
    }


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """按平台终止后台 Shell 及其子进程树。"""

    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def _job_to_dict(job: BackgroundJob) -> dict[str, object]:
    """把作业模型转换为工具返回所需的 JSON 字典。"""

    return {
        "job_id": job.id,
        "status": job.status.value,
        "command": job.command,
        "cwd": job.cwd,
        "timeout_seconds": job.timeout_seconds,
        "return_code": job.return_code,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _utc_now() -> str:
    """生成可排序的 UTC ISO 时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


__all__ = [
    "BackgroundCancelTool",
    "BackgroundJob",
    "BackgroundJobManager",
    "BackgroundJobStatus",
    "BackgroundOutputTool",
    "BackgroundStartTool",
    "BackgroundStatusTool",
]

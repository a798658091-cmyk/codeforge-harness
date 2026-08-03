"""验证后台 Shell 生命周期、日志、取消和通知工具。

任务流位置：模型启动 background_start 后由 BackgroundJobManager 管理进程与日志，
终态写入 NotificationCenter；测试仅在 pytest 临时 workspace 启动短命子进程。
"""

import json
import sys
import time
from pathlib import Path

import pytest

from harness.safety.workspace import WorkspaceViolation
from harness.safety.permissions import PermissionPolicy
from harness.tasks.background import (
    BackgroundJobManager,
    BackgroundJobStatus,
)
from harness.tasks.notifications import NotificationCenter
from harness.tools import build_default_registry
from harness.tools.base import ToolError


def _python_command(code: str) -> str:
    """构造兼容 Windows 路径空格的 Python 后台命令。"""

    escaped = code.replace('"', '\\"')
    return f'"{sys.executable}" -c "{escaped}"'


def _wait_for_terminal(
    manager: BackgroundJobManager,
    job_id: str,
    *,
    timeout: float = 5.0,
):
    """短轮询等待测试作业进入终态，超时则使断言清晰失败。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.status(job_id)
        if job.status is not BackgroundJobStatus.RUNNING:
            return job
        time.sleep(0.02)
    raise AssertionError(f"background job did not finish: {job_id}")


def test_background_tools_run_collect_output_and_notify(
    workspace: Path,
) -> None:
    """验证后台启动立即返回，完成后可读取日志和成功通知。"""

    notifications = NotificationCenter()
    manager = BackgroundJobManager(
        workspace,
        notifications=notifications,
    )
    registry = build_default_registry(
        workspace,
        background_manager=manager,
        notification_center=notifications,
        permission_policy=PermissionPolicy(default="allow"),
    )
    try:
        started = registry.dispatch(
            "background_start",
            {"command": _python_command("print(24680)")},
        )
        job_id = json.loads(started.content)["job_id"]
        job = _wait_for_terminal(manager, job_id)
        status = registry.dispatch(
            "background_status",
            {"job_id": job_id},
        )
        output = registry.dispatch(
            "background_output",
            {"job_id": job_id},
        )
        listed = registry.dispatch(
            "notification_list",
            {"unread_only": True},
        )

        assert started.success is True
        assert job.status is BackgroundJobStatus.SUCCEEDED
        assert json.loads(status.content)["status"] == "succeeded"
        assert "24680" in output.content
        assert json.loads(listed.content)[0]["level"] == "success"
    finally:
        manager.shutdown()


def test_background_cancel_terminates_job_and_emits_warning(
    workspace: Path,
) -> None:
    """验证取消工具终止长任务并产生 warning 通知。"""

    notifications = NotificationCenter()
    manager = BackgroundJobManager(
        workspace,
        notifications=notifications,
    )
    registry = build_default_registry(
        workspace,
        background_manager=manager,
        notification_center=notifications,
        permission_policy=PermissionPolicy(default="allow"),
    )
    try:
        started = registry.dispatch(
            "background_start",
            {
                "command": _python_command(
                    "import time; time.sleep(30)"
                )
            },
        )
        job_id = json.loads(started.content)["job_id"]
        cancelled = registry.dispatch(
            "background_cancel",
            {"job_id": job_id},
        )

        assert cancelled.success is True
        assert json.loads(cancelled.content)["status"] == "cancelled"
        assert notifications.list()[-1].level == "warning"
    finally:
        manager.shutdown()


def test_notification_acknowledge_marks_event_read(workspace: Path) -> None:
    """验证 Agent 能确认通知并从未读查询中移除它。"""

    center = NotificationCenter()
    notification = center.emit(
        level="info",
        title="ready",
        message="background task is ready",
        source="test",
    )
    registry = build_default_registry(
        workspace,
        notification_center=center,
        permission_policy=PermissionPolicy(default="allow"),
    )

    acknowledged = registry.dispatch(
        "notification_ack",
        {"notification_id": notification.id},
    )
    unread = registry.dispatch(
        "notification_list",
        {"unread_only": True},
    )

    assert acknowledged.success is True
    assert json.loads(acknowledged.content)["read"] is True
    assert json.loads(unread.content) == []


def test_background_shell_reuses_hard_deny_and_workspace_boundary(
    workspace: Path,
) -> None:
    """验证后台命令不能绕过同步 Shell 的 hard-deny 或 cwd 沙箱。"""

    manager = BackgroundJobManager(workspace)
    try:
        with pytest.raises(ToolError):
            manager.start("git reset --hard")
        with pytest.raises(WorkspaceViolation):
            manager.start(_python_command("print(1)"), cwd="..")
    finally:
        manager.shutdown()


def test_background_timeout_sets_terminal_state_and_notification(
    workspace: Path,
) -> None:
    """验证观察线程会终止超时作业并发布 error 通知。"""

    center = NotificationCenter()
    manager = BackgroundJobManager(workspace, notifications=center)
    try:
        job = manager.start(
            _python_command("import time; time.sleep(30)"),
            timeout_seconds=1,
        )
        terminal = _wait_for_terminal(manager, job.id, timeout=6.0)

        assert terminal.status is BackgroundJobStatus.TIMED_OUT
        assert center.list()[-1].level == "error"
    finally:
        manager.shutdown()

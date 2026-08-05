"""验证可写 Subagent、Worktree 与 MessageBus 的最小端到端协作。

任务流位置：Worker 接收委派后调用真实 write_file 工具，在独立分支提交；测试
等待异步终态、检查事件和补丁，再显式集成到主工作区。
"""

from __future__ import annotations

import shutil
import subprocess
import time
import json
from pathlib import Path

import pytest

from harness.delegation.message_bus import MessageBus
from harness.delegation.team import (
    WritableSubagentManager,
    WritableSubagentStatus,
)
from harness.agent.loop import AgentLoop
from harness.agent.prompt import build_system_prompt
from harness.providers.base import AssistantTurn, ToolCall
from harness.providers.mock import MockProvider
from harness.safety.permissions import PermissionPolicy
from harness.tools import build_default_registry
from harness.worktrees import WorktreeManager


def _git(repo: Path, *arguments: str) -> None:
    """在临时仓库执行必须成功的 Git 命令。"""

    subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _wait_for_terminal(
    manager: WritableSubagentManager,
    subagent_id: str,
) -> WritableSubagentStatus:
    """短暂轮询异步 Worker，超时则让测试明确失败。"""

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = manager.status(subagent_id).status
        if status in {
            WritableSubagentStatus.COMPLETED,
            WritableSubagentStatus.FAILED,
            WritableSubagentStatus.CANCELLED,
        }:
            return status
        time.sleep(0.02)
    raise AssertionError("subagent did not reach a terminal state")


def test_writable_subagent_commits_then_integrates(tmp_path: Path) -> None:
    """用一个工具回合覆盖异步执行、事件、提交、审查和集成。"""

    if shutil.which("git") is None:
        pytest.skip("git executable is required")
    _git(tmp_path, "init")
    (tmp_path / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "initial",
    )
    provider = MockProvider(
        responses=[
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={
                            "path": "worker.txt",
                            "content": "hello from worker\n",
                        },
                    )
                ]
            ),
            AssistantTurn(content="文件已创建并检查。"),
        ]
    )
    bus = MessageBus()
    manager = WritableSubagentManager(
        provider=provider,
        workspace=tmp_path,
        worktrees=WorktreeManager(tmp_path),
        message_bus=bus,
        max_workers=1,
    )

    task = manager.spawn("创建 worker.txt", max_steps=3)
    assert _wait_for_terminal(manager, task.id) is WritableSubagentStatus.COMPLETED
    completed = manager.status(task.id)
    assert completed.commit
    assert not (tmp_path / "worker.txt").exists()
    assert "worker.txt" in manager.diff(task.id)
    event_types = {
        event.event_type for event in bus.history(limit=20)
    }
    assert {"worktree.created", "worktree.committed", "subagent.completed"} <= (
        event_types
    )

    integrated = manager.integrate(task.id)
    manager.shutdown()

    assert integrated.status is WritableSubagentStatus.INTEGRATED
    assert "worktree.integrated" in {
        event.event_type for event in bus.history(limit=20)
    }
    assert (tmp_path / "worker.txt").read_text(
        encoding="utf-8"
    ) == "hello from worker\n"


def test_two_writable_subagents_use_independent_worktrees(
    tmp_path: Path,
) -> None:
    """验证两个 Worker 拥有不同目录和分支，并能依次集成互不冲突的结果。"""

    if shutil.which("git") is None:
        pytest.skip("git executable is required")
    _git(tmp_path, "init")
    (tmp_path / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "initial",
    )

    def responder(messages, tools, call_index):
        """根据各 Worker 的用户任务生成互不冲突的文件调用。"""

        task = next(
            message["content"]
            for message in messages
            if message.get("role") == "user"
        )
        if not any(message.get("role") == "tool" for message in messages):
            filename = "worker-a.txt" if "A" in task else "worker-b.txt"
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id=f"write-{filename}",
                        name="write_file",
                        arguments={"path": filename, "content": task},
                    )
                ]
            )
        return AssistantTurn(content=f"完成：{task}")

    bus = MessageBus()
    manager = WritableSubagentManager(
        provider=MockProvider(responder=responder),
        workspace=tmp_path,
        worktrees=WorktreeManager(tmp_path),
        message_bus=bus,
        max_workers=2,
    )

    first = manager.spawn("任务 A", max_steps=3)
    second = manager.spawn("任务 B", max_steps=3)
    assert first.id != second.id
    assert first.worktree_path != second.worktree_path
    assert first.branch != second.branch
    assert _wait_for_terminal(manager, first.id) is WritableSubagentStatus.COMPLETED
    assert _wait_for_terminal(manager, second.id) is WritableSubagentStatus.COMPLETED

    manager.integrate(first.id)
    manager.integrate(second.id)
    manager.shutdown()

    assert (tmp_path / "worker-a.txt").read_text(encoding="utf-8") == "任务 A"
    assert (tmp_path / "worker-b.txt").read_text(encoding="utf-8") == "任务 B"
    completed_sources = {
        event.source
        for event in bus.history(limit=100, event_type="subagent.completed")
    }
    assert completed_sources == {
        f"subagent:{first.id}",
        f"subagent:{second.id}",
    }


def test_parent_mock_loop_reviews_and_integrates_worker(
    tmp_path: Path,
) -> None:
    """验证主 Mock Agent 自主完成 spawn、等待、审查、事件查询和集成闭环。"""

    if shutil.which("git") is None:
        pytest.skip("git executable is required")
    _git(tmp_path, "init")
    (tmp_path / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "initial",
    )

    def responder(messages, tools, call_index):
        """根据系统角色和最后工具结果驱动主从两个独立循环。"""

        is_worker = "writable CodeForge worker" in messages[0]["content"]
        tool_messages = [
            message for message in messages if message.get("role") == "tool"
        ]
        if is_worker:
            if not tool_messages:
                return AssistantTurn(
                    tool_calls=[
                        ToolCall(
                            id="worker-write",
                            name="write_file",
                            arguments={
                                "path": "integrated.txt",
                                "content": "from writable worker\n",
                            },
                        )
                    ]
                )
            return AssistantTurn(content="Worker 已完成独立文件任务。")

        if not tool_messages:
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="parent-spawn",
                        name="subagent_spawn",
                        arguments={"task": "创建 integrated.txt", "max_steps": 3},
                    )
                ]
            )
        last_tool = tool_messages[-1]
        if last_tool["name"] == "subagent_spawn":
            subagent_id = json.loads(last_tool["content"])["id"]
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="parent-wait",
                        name="subagent_status",
                        arguments={
                            "subagent_id": subagent_id,
                            "wait_seconds": 10,
                        },
                    )
                ]
            )
        payload = None
        if last_tool["name"] == "subagent_status":
            payload = json.loads(last_tool["content"])
            assert payload["status"] == "completed"
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="parent-diff",
                        name="subagent_diff",
                        arguments={"subagent_id": payload["id"]},
                    )
                ]
            )
        subagent_id = next(
            json.loads(message["content"])["id"]
            for message in tool_messages
            if message["name"] == "subagent_spawn"
        )
        if last_tool["name"] == "subagent_diff":
            assert "integrated.txt" in last_tool["content"]
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="parent-events",
                        name="message_bus_events",
                        arguments={"limit": 20},
                    )
                ]
            )
        if last_tool["name"] == "message_bus_events":
            assert "subagent.completed" in last_tool["content"]
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="parent-integrate",
                        name="subagent_integrate",
                        arguments={"subagent_id": subagent_id},
                    )
                ]
            )
        if last_tool["name"] == "subagent_integrate":
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="parent-read",
                        name="read_file",
                        arguments={"path": "integrated.txt"},
                    )
                ]
            )
        assert last_tool["name"] == "read_file"
        assert "from writable worker" in last_tool["content"]
        return AssistantTurn(content="已审查并集成 Worker 的提交。")

    provider = MockProvider(responder=responder)
    bus = MessageBus()
    worktrees = WorktreeManager(tmp_path)
    manager = WritableSubagentManager(
        provider=provider,
        workspace=tmp_path,
        worktrees=worktrees,
        message_bus=bus,
        max_workers=1,
    )
    registry = build_default_registry(
        tmp_path,
        permission_policy=PermissionPolicy(default="allow"),
        writable_subagents=manager,
        message_bus=bus,
    )
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        system_prompt=build_system_prompt(tmp_path),
        max_steps=10,
    )

    result = loop.run("把独立文件任务交给可写 Subagent 并审查后集成")
    manager.shutdown()

    assert result.answer == "已审查并集成 Worker 的提交。"
    assert (tmp_path / "integrated.txt").read_text(
        encoding="utf-8"
    ) == "from writable worker\n"
    assert result.state.tool_calls == 6

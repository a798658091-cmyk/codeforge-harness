"""验证本地前端的事件、状态映射、静态资源和 HTTP 状态接口。

任务流位置：这些测试不访问真实 Provider，也不打开浏览器；它们确保 Web Runtime
能消费 Agent 原生事件与 AgentState，并向前端提供稳定的 JSON/SSE 数据模型。
"""

from __future__ import annotations

import json
import threading
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen

import pytest

import harness.web as web_module
from harness.agent.loop import AgentLoop
from harness.agent.state import AgentState
from harness.providers.base import AssistantTurn
from harness.providers.mock import MockProvider
from harness.runtime_events import RuntimeEventLogger
from harness.safety.workspace import Workspace
from harness.tools.base import ToolContext
from harness.tools.registry import ToolRegistry
from harness.web import (
    DashboardRuntime,
    EventStream,
    _extract_answer,
    _format_run_error,
    create_server,
)


def test_event_stream_returns_events_after_sequence() -> None:
    """验证 SSE 事件具有稳定递增序号并支持断点续读。"""

    stream = EventStream()

    first = stream.publish("model.started", {"step": 1})
    second = stream.publish("model.completed", {"step": 1})

    assert first == 1
    assert second == 2
    assert [event["id"] for event in stream.wait_after(1, timeout=0)] == [2]


def test_runtime_maps_agent_state_and_native_events(tmp_path: Path) -> None:
    """验证 Todo、累计 Token、单步 Token 和当前工具映射。"""

    runtime = DashboardRuntime(tmp_path, max_steps=12)
    state = AgentState(
        steps=2,
        tool_calls=3,
        tool_failures=1,
        prompt_tokens=120,
        completion_tokens=30,
        todos=[{"id": "one", "content": "检查配置", "status": "in_progress"}],
    )

    runtime._apply_agent_state(state)
    runtime._apply_runtime_event(
        {
            "type": "tool.started",
            "timestamp": "2026-08-05T00:00:00+00:00",
            "payload": {"tool": "read_file"},
        }
    )
    snapshot = runtime.snapshot()

    assert snapshot["steps"] == 2
    assert snapshot["tokens"]["total"] == 150
    assert snapshot["tokens"]["step_total"] == 150
    assert snapshot["todos"][0]["status"] == "in_progress"
    assert snapshot["current_operation"]["tool"] == "read_file"


def test_agent_loop_emits_model_usage_events(tmp_path: Path) -> None:
    """验证模型开始、用量完成和运行完成事件来自真实 Agent Loop 边界。"""

    events: list[tuple[str, dict[str, object]]] = []
    provider = MockProvider(
        [
            AssistantTurn(
                content="done",
                usage={"prompt_tokens": 10, "completion_tokens": 3},
            )
        ]
    )
    registry = ToolRegistry(ToolContext(workspace=Workspace(tmp_path)))
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        system_prompt="test",
        event_callback=lambda event_type, payload: events.append(
            (event_type, payload)
        ),
    )

    result = loop.run("finish")

    assert result.answer == "done"
    assert [event[0] for event in events] == [
        "run.started",
        "model.started",
        "model.completed",
        "run.completed",
    ]
    assert events[2][1]["prompt_tokens"] == 10


def test_runtime_event_logger_writes_jsonl(tmp_path: Path) -> None:
    """验证供 Web Runtime 读取的事件文件是标准 UTF-8 JSONL。"""

    path = tmp_path / "events.jsonl"
    logger = RuntimeEventLogger(path)

    logger.emit("tool.started", {"tool": "搜索"})

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["type"] == "tool.started"
    assert record["payload"]["tool"] == "搜索"


def test_http_server_serves_dashboard_and_state(tmp_path: Path) -> None:
    """验证页面静态资源和状态 API 可从随机本地端口访问。"""

    runtime = DashboardRuntime(tmp_path)
    server = create_server(runtime, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{address}/", timeout=3) as response:
            page = response.read().decode("utf-8")
        with urlopen(f"{address}/styles.css", timeout=3) as response:
            stylesheet = response.read().decode("utf-8")
        with urlopen(f"{address}/app.js", timeout=3) as response:
            script = response.read().decode("utf-8")
        with urlopen(f"{address}/api/state", timeout=3) as response:
            state = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert "CodeForge Chat" in page
    assert "新建对话" in page
    assert 'aria-label="任务栏"' in page
    assert "PLAN" not in page
    assert "NOW" not in page
    assert "TRACE" not in page
    assert "overflow-y: auto" in stylesheet
    assert "function renderConversation" in script
    assert state["status"] == "idle"
    assert state["workspace"] == str(tmp_path.resolve())


def test_extract_answer_removes_cli_metadata() -> None:
    """验证页面只显示最终回答，不混入 CLI 统计尾注。"""

    output = "任务完成。\n\n[steps=3, tools=2, failures=0]\n[session=demo]\n"

    assert _extract_answer(output) == "任务完成。"


def test_format_run_error_explains_step_limit() -> None:
    """验证步骤耗尽时页面显示可行动的中文原因而不是整段权限输出。"""

    stderr = "[permission:auto-approved] tool=shell\nagent stopped: agent exceeded max_steps=8"

    message = _format_run_error(stderr, 8)

    assert "最大步骤数 8" in message
    assert "缩小任务范围" in message


def test_runtime_appends_turn_and_resumes_same_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证第二次输入不会覆盖第一轮，并改用 --resume 保留模型上下文。"""

    commands: list[list[str]] = []

    class FinishedProcess:
        """提供无需真实子进程的已结束 Popen 最小替身。"""

        stdout = BytesIO()
        stderr = BytesIO()

        def poll(self) -> int:
            """始终报告子进程已经结束。"""

            return 0

        def terminate(self) -> None:
            """满足取消接口，但测试中无需执行任何动作。"""

    class IdleThread:
        """阻止测试启动真实监控线程。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """接受与 threading.Thread 相同的任意构造参数。"""

        def start(self) -> None:
            """保持空操作，让测试直接检查启动后的状态。"""

    def fake_popen(command: list[str], **kwargs: object) -> FinishedProcess:
        """记录 CLI 参数并返回已结束进程。"""

        commands.append(command)
        return FinishedProcess()

    monkeypatch.setattr(web_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(web_module.threading, "Thread", IdleThread)
    runtime = DashboardRuntime(tmp_path, max_steps=8)

    session_id = runtime.start("第一条要求")
    runtime._state["status"] = "completed"
    runtime._state["answer"] = "第一条回答"
    runtime._apply_agent_state(
        AgentState(
            steps=2,
            tool_calls=1,
            prompt_tokens=100,
            completion_tokens=20,
        )
    )
    resumed_id = runtime.start("第二条要求")
    runtime._apply_agent_state(
        AgentState(
            steps=3,
            tool_calls=2,
            prompt_tokens=160,
            completion_tokens=32,
        )
    )
    snapshot = runtime.snapshot()

    assert resumed_id == session_id
    assert snapshot["turns"][0]["prompt"] == "第一条要求"
    assert snapshot["turns"][0]["answer"] == "第一条回答"
    assert snapshot["prompt"] == "第二条要求"
    assert snapshot["steps"] == 1
    assert snapshot["tokens"]["total"] == 72
    assert "--session-id" in commands[0]
    assert "--resume" in commands[1]

"""提供无需前端构建工具的 CodeForge 本地实时控制台。

任务流位置：用户通过浏览器提交普通任务后，本模块启动现有 ``python -m harness``
CLI；后台监控线程从 SQLite 检查点、权限输出和 JSONL 审计中汇总 Todo、Token、
步骤及工具状态，再通过 SSE 推送给单页前端。该入口不改变原 CLI 的执行路径。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse

from harness.agent.state import AgentState
from harness.storage.sqlite import SQLiteSessionStore, SessionNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "webui"
PERMISSION_PATTERN = re.compile(
    r"\[permission:(?:auto-approved|approved)\]\s+tool=(?P<tool>\S+)"
)


def _utc_now() -> str:
    """生成适合 API 与浏览器解析的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EventStream:
    """保存有界实时事件并为多个 SSE 客户端提供阻塞等待。"""

    def __init__(self, *, max_events: int = 500) -> None:
        """创建条件变量、递增序号和有界事件队列。"""

        self._condition = threading.Condition()
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._sequence = 0

    def publish(self, event_type: str, payload: dict[str, Any]) -> int:
        """追加一个事件、分配序号并唤醒所有浏览器连接。"""

        with self._condition:
            self._sequence += 1
            event = {
                "id": self._sequence,
                "type": event_type,
                "timestamp": _utc_now(),
                "payload": payload,
            }
            self._events.append(event)
            self._condition.notify_all()
            return self._sequence

    def wait_after(
        self,
        sequence: int,
        *,
        timeout: float = 15.0,
    ) -> list[dict[str, Any]]:
        """等待序号之后的新事件，超时返回空列表供 SSE 发送心跳。"""

        with self._condition:
            available = [
                event for event in self._events if event["id"] > sequence
            ]
            if available:
                return available
            self._condition.wait(timeout)
            return [
                event for event in self._events if event["id"] > sequence
            ]


class DashboardRuntime:
    """管理一个本地任务进程并维护可供前端读取的实时快照。"""

    def __init__(self, workspace: Path, *, max_steps: int = 24) -> None:
        """绑定固定 Workspace，初始化状态锁和 SSE 事件流。"""

        resolved = workspace.expanduser().resolve()
        if not resolved.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {resolved}")
        self.workspace = resolved
        self.max_steps = max_steps
        self.events = EventStream()
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._cancel_requested = False
        self._state = self._new_state()
        self._turn_baseline = self._empty_counters()
        self._turn_initial_todos: list[dict[str, str]] = []
        self._turn_todos_changed = False
        self._turn_sequence = 0

    def _new_state(self) -> dict[str, Any]:
        """创建页面初始快照，字段在任务间保持稳定。"""

        return {
            "status": "idle",
            "workspace": str(self.workspace),
            "session_id": None,
            "turn_id": None,
            "turns": [],
            "prompt": "",
            "answer": "",
            "error": "",
            "started_at": None,
            "finished_at": None,
            "steps": 0,
            "max_steps": self.max_steps,
            "current_operation": None,
            "todos": [],
            "tokens": {
                "prompt": 0,
                "completion": 0,
                "total": 0,
                "step_prompt": 0,
                "step_completion": 0,
                "step_total": 0,
            },
            "tools": {"calls": 0, "failures": 0},
            "compactions": 0,
            "operations": [],
            "logs": [],
            "totals": self._empty_counters(),
        }

    @staticmethod
    def _empty_counters() -> dict[str, int]:
        """创建跨轮次累计指标或当前轮次基线所需的零值字典。"""

        return {
            "steps": 0,
            "tool_calls": 0,
            "tool_failures": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "compactions": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        """通过 JSON 往返生成不会泄漏内部可变对象的状态副本。"""

        with self._lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def _current_turn_snapshot(self) -> dict[str, Any]:
        """复制当前轮次的展示字段，供下一次输入到来时归档到会话流。"""

        keys = (
            "turn_id",
            "prompt",
            "answer",
            "error",
            "status",
            "started_at",
            "finished_at",
            "steps",
            "max_steps",
            "current_operation",
            "todos",
            "tokens",
            "tools",
            "compactions",
            "operations",
        )
        return json.loads(
            json.dumps(
                {key: self._state[key] for key in keys},
                ensure_ascii=False,
            )
        )

    def _latest_checkpoint_sequence(self, session_id: str) -> int:
        """取得本轮启动前的检查点序号，避免恢复时重复消费上一轮状态。"""

        database = self.workspace / ".codeforge" / "web" / "sessions.sqlite3"
        if not database.exists():
            return 0
        try:
            checkpoint = SQLiteSessionStore(database).load_latest_checkpoint(
                session_id
            )
        except (SessionNotFoundError, OSError):
            return 0
        return checkpoint.sequence

    def start(self, prompt: str) -> str:
        """把新输入追加为同一会话的新轮次，并启动自动审批的真实 CLI。"""

        normalized = prompt.strip()
        if not normalized:
            raise ValueError("task cannot be empty")
        if len(normalized) > 20_000:
            raise ValueError("task is too long")
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("another task is already running")
            resume = self._state["session_id"] is not None
            session_id = self._state["session_id"] or (
                f"web-{uuid.uuid4().hex[:12]}"
            )
            if self._state["prompt"]:
                self._state["turns"].append(self._current_turn_snapshot())
                self._state["turns"] = self._state["turns"][-50:]
            self._turn_sequence += 1
            turn_id = f"turn-{self._turn_sequence}"
            totals = self._state["totals"]
            self._turn_baseline = {
                key: int(totals.get(key, 0))
                for key in self._empty_counters()
            }
            self._turn_initial_todos = list(self._state.get("todos") or [])
            self._turn_todos_changed = False
            self._state.update(
                {
                    "status": "starting",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "prompt": normalized,
                    "answer": "",
                    "error": "",
                    "started_at": _utc_now(),
                    "finished_at": None,
                    "steps": 0,
                    "current_operation": None,
                    "todos": [],
                    "tokens": {
                        "prompt": 0,
                        "completion": 0,
                        "total": 0,
                        "step_prompt": 0,
                        "step_completion": 0,
                        "step_total": 0,
                    },
                    "tools": {"calls": 0, "failures": 0},
                    "compactions": 0,
                    "operations": [],
                    "logs": [],
                }
            )
            self._cancel_requested = False

        checkpoint_floor = self._latest_checkpoint_sequence(session_id)
        artifact_id = f"{session_id}-{turn_id}"
        command = self._build_command(
            session_id,
            artifact_id,
            normalized,
            resume=resume,
        )
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(PROJECT_ROOT), existing) if value
        )
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        creation_flags = (
            subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
        with self._lock:
            self._process = process
            self._state["status"] = "running"
        self.events.publish("run.started", self.snapshot())
        threading.Thread(
            target=self._watch_process,
            args=(process, session_id, artifact_id, checkpoint_floor),
            name=f"codeforge-web-{session_id}",
            daemon=True,
        ).start()
        return session_id

    def new_conversation(self) -> None:
        """在没有任务运行时清空页面会话，下一条输入将创建新的 SQLite 会话。"""

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("cannot start a new conversation while running")
            self._state = self._new_state()
            self._turn_baseline = self._empty_counters()
            self._turn_initial_todos = []
            self._turn_todos_changed = False
            self._turn_sequence = 0
        self.events.publish("conversation.started", self.snapshot())

    def cancel(self) -> bool:
        """终止当前 CLI 进程，并由监控线程完成最终状态归档。"""

        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False
            self._cancel_requested = True
            self._state["status"] = "cancelling"
            process.terminate()
        self.events.publish("run.cancelling", self.snapshot())
        return True

    def _build_command(
        self,
        session_id: str,
        artifact_id: str,
        prompt: str,
        *,
        resume: bool,
    ) -> list[str]:
        """构造新建或恢复同一会话的参数化 Harness CLI 命令。"""

        command = [
            sys.executable,
            "-m",
            "harness",
            "--workspace",
            str(self.workspace),
            "--yes",
            "--max-steps",
            str(self.max_steps),
            "--audit-log",
            f".codeforge/web/{artifact_id}.audit.jsonl",
            "--runtime-event-log",
            f".codeforge/web/{artifact_id}.events.jsonl",
            "--session-db",
            ".codeforge/web/sessions.sqlite3",
            "--memory-db",
            ".codeforge/memory.sqlite3",
        ]
        command.extend(
            ["--resume", session_id]
            if resume
            else ["--session-id", session_id]
        )
        command.append(prompt)
        return command

    def _watch_process(
        self,
        process: subprocess.Popen[bytes],
        session_id: str,
        artifact_id: str,
        checkpoint_floor: int,
    ) -> None:
        """并行读取输出，同时轮询检查点与审计记录直到进程结束。"""

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        stdout_thread = threading.Thread(
            target=self._pump_stream,
            args=(process.stdout, stdout_lines, "stdout"),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._pump_stream,
            args=(process.stderr, stderr_lines, "stderr"),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        database = self.workspace / ".codeforge" / "web" / "sessions.sqlite3"
        audit_path = (
            self.workspace
            / ".codeforge"
            / "web"
            / f"{artifact_id}.audit.jsonl"
        )
        event_path = (
            self.workspace
            / ".codeforge"
            / "web"
            / f"{artifact_id}.events.jsonl"
        )
        last_checkpoint = checkpoint_floor
        audit_offset = 0
        event_offset = 0
        while process.poll() is None:
            last_checkpoint = self._sync_checkpoint(
                database,
                session_id,
                last_checkpoint,
            )
            audit_offset = self._sync_audit(audit_path, audit_offset)
            event_offset = self._sync_runtime_events(event_path, event_offset)
            time.sleep(0.25)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        last_checkpoint = self._sync_checkpoint(
            database,
            session_id,
            last_checkpoint,
        )
        self._sync_audit(audit_path, audit_offset)
        self._sync_runtime_events(event_path, event_offset)
        return_code = process.wait()
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        with self._lock:
            cancelled = self._cancel_requested
            self._state["status"] = (
                "cancelled"
                if cancelled
                else "completed" if return_code == 0 else "failed"
            )
            self._state["answer"] = _extract_answer(stdout)
            self._state["error"] = (
                ""
                if return_code == 0
                else _format_run_error(stderr, self.max_steps)
            )
            self._state["finished_at"] = _utc_now()
            self._state["current_operation"] = None
            self._process = None
        self.events.publish("run.finished", self.snapshot())

    def _pump_stream(
        self,
        stream: BinaryIO | None,
        target: list[str],
        channel: str,
    ) -> None:
        """逐行解码子进程输出，并从权限行识别工具开始事件。"""

        if stream is None:
            return
        while True:
            raw_line = stream.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace")
            target.append(line)
            stripped = line.rstrip()
            if stripped:
                self._append_log(channel, stripped)
            match = PERMISSION_PATTERN.search(stripped)
            if match:
                tool = match.group("tool")
                with self._lock:
                    self._state["current_operation"] = {
                        "tool": tool,
                        "status": "running",
                        "started_at": _utc_now(),
                    }
                self.events.publish("tool.started", self.snapshot())

    def _append_log(self, channel: str, message: str) -> None:
        """追加有界运行日志并推送轻量日志事件。"""

        entry = {"timestamp": _utc_now(), "channel": channel, "message": message}
        with self._lock:
            self._state["logs"].append(entry)
            self._state["logs"] = self._state["logs"][-120:]
        self.events.publish("log", entry)

    def _sync_checkpoint(
        self,
        database: Path,
        session_id: str,
        last_sequence: int,
    ) -> int:
        """读取最新 SQLite 检查点并计算当前模型步骤的 Token 增量。"""

        if not database.exists():
            return last_sequence
        try:
            checkpoint = SQLiteSessionStore(database).load_latest_checkpoint(
                session_id
            )
        except (SessionNotFoundError, OSError):
            return last_sequence
        if checkpoint.sequence <= last_sequence:
            return last_sequence
        self._apply_agent_state(checkpoint.state)
        self.events.publish(
            "state.updated",
            {"reason": checkpoint.reason, "state": self.snapshot()},
        )
        return checkpoint.sequence

    def _apply_agent_state(self, state: AgentState) -> None:
        """把累计 AgentState 换算为当前轮次增量与整段会话总量。"""

        with self._lock:
            previous = self._state["tokens"]
            turn_prompt = max(
                0,
                state.prompt_tokens - self._turn_baseline["prompt_tokens"],
            )
            turn_completion = max(
                0,
                state.completion_tokens
                - self._turn_baseline["completion_tokens"],
            )
            prompt_delta = max(0, turn_prompt - previous["prompt"])
            completion_delta = max(
                0,
                turn_completion - previous["completion"],
            )
            if prompt_delta or completion_delta:
                previous["step_prompt"] = prompt_delta
                previous["step_completion"] = completion_delta
                previous["step_total"] = prompt_delta + completion_delta
            previous["prompt"] = turn_prompt
            previous["completion"] = turn_completion
            previous["total"] = turn_prompt + turn_completion
            self._state["steps"] = max(
                0,
                state.steps - self._turn_baseline["steps"],
            )
            self._set_current_todos(state.todos)
            self._state["tools"] = {
                "calls": max(
                    0,
                    state.tool_calls - self._turn_baseline["tool_calls"],
                ),
                "failures": max(
                    0,
                    state.tool_failures
                    - self._turn_baseline["tool_failures"],
                ),
            }
            self._state["compactions"] = max(
                0,
                state.compactions - self._turn_baseline["compactions"],
            )
            self._state["totals"] = {
                "steps": state.steps,
                "tool_calls": state.tool_calls,
                "tool_failures": state.tool_failures,
                "prompt_tokens": state.prompt_tokens,
                "completion_tokens": state.completion_tokens,
                "compactions": state.compactions,
            }

    def _set_current_todos(self, todos: list[dict[str, str]]) -> None:
        """只在本轮真正创建或修改 Todo 后展示，避免继承旧计划。"""

        normalized = list(todos)
        if not self._turn_todos_changed:
            if normalized == self._turn_initial_todos:
                return
            self._turn_todos_changed = True
        self._state["todos"] = normalized

    def _sync_audit(self, path: Path, offset: int) -> int:
        """增量读取审计 JSONL，将工具完成结果追加到操作时间线。"""

        if not path.exists():
            return offset
        try:
            with path.open("r", encoding="utf-8") as stream:
                stream.seek(offset)
                while True:
                    raw_line = stream.readline()
                    if not raw_line:
                        break
                    try:
                        record = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    self._apply_audit_record(record)
                return stream.tell()
        except OSError:
            return offset

    def _sync_runtime_events(self, path: Path, offset: int) -> int:
        """增量读取 Agent 原生事件，准确展示模型与工具的当前阶段。"""

        if not path.exists():
            return offset
        try:
            with path.open("r", encoding="utf-8") as stream:
                stream.seek(offset)
                while True:
                    raw_line = stream.readline()
                    if not raw_line:
                        break
                    try:
                        record = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    self._apply_runtime_event(record)
                return stream.tell()
        except OSError:
            return offset

    def _apply_runtime_event(self, record: dict[str, Any]) -> None:
        """将模型、工具和 Todo 事件映射到当前页面状态。"""

        event_type = str(record.get("type", ""))
        payload = record.get("payload") or {}
        with self._lock:
            if event_type == "model.started":
                self._state["current_operation"] = {
                    "tool": "model",
                    "status": "running",
                    "started_at": record.get("timestamp") or _utc_now(),
                }
            elif event_type == "model.completed":
                usage = payload.get("usage") or {}
                self._state["tokens"]["step_prompt"] = int(
                    usage.get("prompt_tokens", 0)
                )
                self._state["tokens"]["step_completion"] = int(
                    usage.get("completion_tokens", 0)
                )
                self._state["tokens"]["step_total"] = (
                    self._state["tokens"]["step_prompt"]
                    + self._state["tokens"]["step_completion"]
                )
                self._state["current_operation"] = None
            elif event_type == "tool.started":
                self._state["current_operation"] = {
                    "tool": str(payload.get("tool", "unknown")),
                    "status": "running",
                    "started_at": record.get("timestamp") or _utc_now(),
                }
            elif event_type == "tool.completed":
                current = self._state.get("current_operation") or {}
                if current.get("tool") == payload.get("tool"):
                    self._state["current_operation"] = None
            elif event_type == "todos.updated":
                self._set_current_todos(list(payload.get("todos") or []))
            elif event_type == "context.compacted":
                cumulative = int(
                    payload.get(
                        "compactions",
                        self._turn_baseline["compactions"],
                    )
                )
                self._state["compactions"] = max(
                    0,
                    cumulative - self._turn_baseline["compactions"],
                )
        self.events.publish(event_type or "runtime.event", self.snapshot())

    def _apply_audit_record(self, record: dict[str, Any]) -> None:
        """把脱敏审计记录转换为前端可读的工具完成事件。"""

        result = record.get("result") or {}
        operation = {
            "timestamp": record.get("timestamp") or _utc_now(),
            "tool": str(record.get("tool_name", "unknown")),
            "status": "success" if result.get("success") else "failed",
            "duration_ms": result.get("duration_ms", 0),
            "permission": (record.get("permission") or {}).get("decision"),
            "summary": str(result.get("content", ""))[:500],
            "error_type": result.get("error_type"),
        }
        with self._lock:
            self._state["operations"].append(operation)
            self._state["operations"] = self._state["operations"][-80:]
            current = self._state.get("current_operation") or {}
            if current.get("tool") == operation["tool"]:
                self._state["current_operation"] = None
        self.events.publish("tool.finished", operation)


class DashboardHandler(BaseHTTPRequestHandler):
    """处理静态资源、JSON API 和 Server-Sent Events 请求。"""

    runtime: DashboardRuntime
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """分发页面资源、状态快照和 SSE 事件流。"""

        path = urlparse(self.path).path
        if path == "/api/state":
            self._send_json(HTTPStatus.OK, self.runtime.snapshot())
        elif path == "/api/events":
            self._serve_events()
        elif path in {"/", "/index.html"}:
            self._send_static("chat.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._send_static("chat.js", "text/javascript; charset=utf-8")
        elif path == "/styles.css":
            self._send_static("chat.css", "text/css; charset=utf-8")
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        """接收任务启动和取消请求。"""

        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/run":
                session_id = self.runtime.start(str(payload.get("prompt", "")))
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {"session_id": session_id},
                )
            elif path == "/api/cancel":
                cancelled = self.runtime.cancel()
                self._send_json(HTTPStatus.OK, {"cancelled": cancelled})
            elif path == "/api/new":
                self.runtime.new_conversation()
                self._send_json(HTTPStatus.OK, {"started": True})
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (ValueError, RuntimeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        """仅在标准错误输出简洁的本地访问日志。"""

        sys.stderr.write(f"[web] {format % args}\n")

    def _read_json(self) -> dict[str, Any]:
        """读取有大小上限的 UTF-8 JSON 请求体。"""

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 100_000:
            raise ValueError("invalid request body length")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _send_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        """发送 UTF-8 JSON 响应并明确关闭普通 HTTP 连接。"""

        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str, content_type: str) -> None:
        """从固定白名单目录返回前端资源，避免任意路径读取。"""

        path = STATIC_ROOT / name
        if not path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "asset not found"})
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self) -> None:
        """持续发送实时事件，并用注释心跳保持浏览器连接。"""

        last_id = int(self.headers.get("Last-Event-ID", "0") or 0)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            initial = {
                "id": last_id,
                "type": "snapshot",
                "payload": self.runtime.snapshot(),
            }
            self._write_event(initial)
            while True:
                events = self.runtime.events.wait_after(last_id)
                if not events:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    self._write_event(event)
                    last_id = int(event["id"])
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _write_event(self, event: dict[str, Any]) -> None:
        """把单个事件编码为标准 SSE 数据块。"""

        payload = json.dumps(event["payload"], ensure_ascii=False)
        block = (
            f"id: {event.get('id', 0)}\n"
            f"event: {event.get('type', 'message')}\n"
            f"data: {payload}\n\n"
        )
        self.wfile.write(block.encode("utf-8"))
        self.wfile.flush()


def _extract_answer(stdout: str) -> str:
    """从 CLI 标准输出中去除尾部步骤、审计和会话元数据。"""

    markers = ("\n[steps=", "\n[audit=", "\n[session=")
    cut = len(stdout)
    for marker in markers:
        index = stdout.find(marker)
        if index >= 0:
            cut = min(cut, index)
    return stdout[:cut].strip()


def _format_run_error(stderr: str, max_steps: int) -> str:
    """把常见终止原因转换为页面可理解的提示，并限制原始错误长度。"""

    normalized = stderr.strip()
    if "agent exceeded max_steps=" in normalized:
        return (
            f"任务达到最大步骤数 {max_steps} 后终止。已成功执行的工具结果仍然"
            "保留，但模型没有在限制内结束任务；请缩小任务范围，或启动服务时"
            "提高 --max-steps。"
        )
    return normalized[-4000:]


def create_server(
    runtime: DashboardRuntime,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """创建绑定指定 Runtime 的线程化本地 HTTP Server。"""

    handler = type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {"runtime": runtime},
    )
    return ThreadingHTTPServer((host, port), handler)


def build_parser() -> argparse.ArgumentParser:
    """创建本地前端服务的命令行参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="python -m harness.web",
        description="Run the local CodeForge live dashboard.",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-steps", type=int, default=24)
    return parser


def main(argv: list[str] | None = None) -> int:
    """启动本地服务，打印访问地址并在 Ctrl+C 时安全退出。"""

    args = build_parser().parse_args(argv)
    runtime = DashboardRuntime(args.workspace, max_steps=args.max_steps)
    server = create_server(runtime, host=args.host, port=args.port)
    address = f"http://{args.host}:{server.server_address[1]}"
    print(f"CodeForge dashboard: {address}")
    print(f"Workspace: {runtime.workspace}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        runtime.cancel()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DashboardRuntime",
    "EventStream",
    "_format_run_error",
    "create_server",
    "main",
]

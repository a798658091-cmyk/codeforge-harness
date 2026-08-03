"""验证 SQLite 会话检查点、工作区筛选和 Agent Loop 恢复。

任务流位置：模拟一次任务退出后由 --resume 读取最新 AgentState，再追加新的用户
消息继续请求 Mock Provider，证明历史与 Todo 均可跨进程保存。
"""

from pathlib import Path
from copy import deepcopy

import pytest

from harness.agent.loop import AgentLoop
from harness.agent.state import AgentState
from harness import cli
from harness.cli import build_parser
from harness.config import ConfigurationError, Settings
from harness.providers.base import AssistantTurn
from harness.providers.mock import MockProvider
from harness.storage.sqlite import SQLiteSessionStore, SessionNotFoundError
from harness.tasks.todo import TodoList
from harness.tools import build_default_registry


class _FakeCLIProvider:
    """替代 CLI 真实 Provider，记录消息并返回离线答案。"""

    requests: list[list[dict]] = []

    def __init__(self, **kwargs) -> None:
        """接受真实 Provider 构造参数但不访问网络。"""

        self.options = kwargs

    def complete(self, messages, tools) -> AssistantTurn:
        """保存一次 CLI 请求并返回包含序号的确定性回答。"""

        self.requests.append(deepcopy(messages))
        return AssistantTurn(content=f"offline-answer-{len(self.requests)}")


def test_sqlite_store_saves_and_loads_latest_checkpoint(
    workspace: Path,
) -> None:
    """验证检查点序号递增且最新状态可以无损恢复。"""

    store = SQLiteSessionStore(workspace / ".codeforge" / "sessions.sqlite3")
    session = store.create_session(workspace, session_id="session-a")
    state = AgentState(
        messages=[{"role": "user", "content": "第一轮"}],
        todos=[{"id": "one", "content": "任务", "status": "pending"}],
    )

    first = store.save_checkpoint(session.id, state, reason="user_message")
    state.steps = 1
    second = store.save_checkpoint(session.id, state, reason="completed")
    restored = store.load_latest_checkpoint(session.id)

    assert first.sequence == 1
    assert second.sequence == 2
    assert restored.reason == "completed"
    assert restored.state.steps == 1
    assert restored.state.todos[0]["id"] == "one"
    assert store.latest_session(workspace).id == "session-a"


def test_sqlite_store_reports_missing_session(workspace: Path) -> None:
    """验证恢复未知会话时返回专用异常。"""

    store = SQLiteSessionStore(workspace / "sessions.sqlite3")

    with pytest.raises(SessionNotFoundError):
        store.load_latest_checkpoint("missing")


def test_agent_loop_resumes_messages_and_todos_from_checkpoint(
    workspace: Path,
) -> None:
    """验证恢复运行会保留旧历史、刷新系统提示并追加新任务。"""

    initial = AgentState(
        messages=[
            {"role": "system", "content": "old system"},
            {"role": "user", "content": "旧任务"},
            {"role": "assistant", "content": "旧回答"},
        ],
        todos=[{"id": "old", "content": "继续处理", "status": "pending"}],
    )

    def responder(messages, tools, call_index):
        """检查恢复上下文后返回确定性答案。"""

        assert messages[0]["content"] == "new system"
        assert any(message.get("content") == "旧回答" for message in messages)
        assert messages[-1]["content"] == "新任务"
        return AssistantTurn(content="恢复成功")

    checkpoints: list[tuple[str, AgentState]] = []
    loop = AgentLoop(
        provider=MockProvider(responder=responder),
        registry=build_default_registry(
            workspace,
            todo_list=TodoList(),
        ),
        system_prompt="new system",
        checkpoint_callback=lambda state, reason: checkpoints.append(
            (reason, AgentState.from_dict(state.to_dict()))
        ),
    )

    result = loop.run("新任务", initial_state=initial)

    assert result.answer == "恢复成功"
    assert result.state.todos[0]["id"] == "old"
    assert checkpoints[-1][0] == "completed"


def test_resume_closes_tool_calls_interrupted_before_checkpoint(
    workspace: Path,
) -> None:
    """验证恢复不会重放未知副作用，而会为未完成调用补充错误结果。"""

    initial = AgentState(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "write files"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "finished",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "interrupted",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "finished",
                "name": "read_file",
                "content": "done",
            },
        ]
    )

    def responder(messages, tools, call_index):
        """确认缺失结果已闭合且没有重复执行旧工具。"""

        recovered = [
            message
            for message in messages
            if message.get("tool_call_id") == "interrupted"
        ]
        assert len(recovered) == 1
        assert "tool_error:interrupted" in recovered[0]["content"]
        return AssistantTurn(content="safe")

    reasons: list[str] = []
    loop = AgentLoop(
        provider=MockProvider(responder=responder),
        registry=build_default_registry(workspace),
        system_prompt="system",
        checkpoint_callback=lambda state, reason: reasons.append(reason),
    )

    result = loop.run("continue", initial_state=initial)

    assert result.answer == "safe"
    assert "resume_recovery" in reasons


def test_settings_resolves_session_database_inside_workspace(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证会话数据库使用 workspace 沙箱且支持环境变量配置。"""

    monkeypatch.setenv("CODEFORGE_MODEL", "test-model")
    monkeypatch.setenv("CODEFORGE_API_KEY", "test-key")
    monkeypatch.setenv("CODEFORGE_SESSION_DB", ".state/history.sqlite3")

    settings = Settings.from_env(workspace=workspace)

    assert settings.session_db == (
        workspace / ".state" / "history.sqlite3"
    ).resolve()
    with pytest.raises(ConfigurationError):
        Settings.from_env(
            workspace=workspace,
            session_db=workspace.parent / "outside.sqlite3",
        )


def test_cli_parser_accepts_resume_and_skill_options() -> None:
    """验证用户可通过 CLI 恢复最近会话并预加载多个技能。"""

    args = build_parser().parse_args(
        [
            "--resume",
            "latest",
            "--skill",
            "review",
            "继续任务",
        ]
    )

    assert args.resume == "latest"
    assert args.skill == ["review"]
    assert args.prompt == ["继续任务"]


def test_cli_persists_user_input_and_resumes_session(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验证 CLI 位置参数会保存，并能通过 --resume 恢复到下一轮。"""

    _FakeCLIProvider.requests = []
    monkeypatch.setenv("CODEFORGE_MODEL", "test-model")
    monkeypatch.setenv("CODEFORGE_API_KEY", "test-key")
    monkeypatch.setattr(cli, "OpenAICompatibleProvider", _FakeCLIProvider)

    first_code = cli.main(
        [
            "--workspace",
            str(workspace),
            "--session-id",
            "cli-demo",
            "第一轮用户输入",
        ]
    )
    second_code = cli.main(
        [
            "--workspace",
            str(workspace),
            "--resume",
            "cli-demo",
            "第二轮用户输入",
        ]
    )
    output = capsys.readouterr().out

    assert first_code == 0
    assert second_code == 0
    assert "[session=cli-demo]" in output
    second_messages = _FakeCLIProvider.requests[1]
    assert any(
        message.get("content") == "第一轮用户输入"
        for message in second_messages
    )
    assert second_messages[-1]["content"] == "第二轮用户输入"


def test_cli_reads_prompt_from_interactive_input(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证未提供位置参数时 CodeForge task 提示会读取用户输入。"""

    _FakeCLIProvider.requests = []
    monkeypatch.setenv("CODEFORGE_MODEL", "test-model")
    monkeypatch.setenv("CODEFORGE_API_KEY", "test-key")
    monkeypatch.setattr(cli, "OpenAICompatibleProvider", _FakeCLIProvider)
    monkeypatch.setattr("builtins.input", lambda prompt: "终端交互输入")

    exit_code = cli.main(
        ["--workspace", str(workspace), "--no-session"]
    )

    assert exit_code == 0
    assert _FakeCLIProvider.requests[0][-1]["content"] == "终端交互输入"

"""验证只读 Subagent 的工具白名单、独立循环和越权阻断。

任务流位置：主 Agent 的 delegate_readonly 工具进入独立 Agent Loop；测试使用
Mock Provider 证明子 Agent 能读取文件，但看不到或无法执行任何写入与 Shell。
"""

import json
from pathlib import Path

from harness.context.memory import MemoryStore
from harness.context.skills import SkillRegistry
from harness.delegation.subagent import ReadonlySubagentRunner
from harness.providers.base import AssistantTurn, ToolCall
from harness.providers.mock import MockProvider
from harness.safety.permissions import PermissionPolicy
from harness.tools import build_default_registry


def test_readonly_subagent_reads_file_with_restricted_schemas(
    workspace: Path,
) -> None:
    """验证子 Agent 的模型请求只包含只读工具并能完成调查。"""

    provider = MockProvider(
        responses=[
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="read-1",
                        name="read_file",
                        arguments={"path": "src/sample.py"},
                    )
                ]
            ),
            AssistantTurn(content="greet 返回 hello。"),
        ]
    )
    runner = ReadonlySubagentRunner(
        provider=provider,
        workspace=workspace,
        skill_registry=SkillRegistry(),
        memory_store=MemoryStore(workspace / "memory.sqlite3"),
    )

    result = runner.run("调查 greet 的行为")

    tool_names = {
        schema["function"]["name"]
        for schema in provider.requests[0]["tools"]
    }
    assert result.answer == "greet 返回 hello。"
    assert result.tool_calls == 1
    assert tool_names == {
        "read_file",
        "search",
        "list_skills",
        "read_skill",
        "memory_search",
    }
    assert not tool_names & {
        "write_file",
        "edit_file",
        "shell",
        "background_start",
        "delegate_readonly",
    }


def test_readonly_subagent_rejects_unlisted_write_call(
    workspace: Path,
) -> None:
    """验证模型即使凭空请求写工具，也只会收到 unknown_tool 错误。"""

    def responder(messages, tools, call_index):
        """首轮尝试越权写入，次轮检查错误并结束。"""

        if call_index == 0:
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="bad-write",
                        name="write_file",
                        arguments={"path": "forbidden.txt", "content": "x"},
                    )
                ]
            )
        assert "tool_error:unknown_tool" in messages[-1]["content"]
        return AssistantTurn(content="写入不可用。")

    runner = ReadonlySubagentRunner(
        provider=MockProvider(responder=responder),
        workspace=workspace,
    )

    result = runner.run("尝试写文件")

    assert result.answer == "写入不可用。"
    assert result.tool_failures == 1
    assert not (workspace / "forbidden.txt").exists()


def test_parent_registry_returns_structured_subagent_result(
    workspace: Path,
) -> None:
    """验证主 Agent 通过委派工具收到带只读模式和指标的 JSON。"""

    runner = ReadonlySubagentRunner(
        provider=MockProvider(
            responses=[AssistantTurn(content="调查完成")]
        ),
        workspace=workspace,
    )
    registry = build_default_registry(
        workspace,
        subagent_runner=runner,
        permission_policy=PermissionPolicy(default="allow"),
    )

    dispatched = registry.dispatch(
        "delegate_readonly",
        {"task": "查看项目结构", "max_steps": 3},
    )
    payload = json.loads(dispatched.content)

    assert dispatched.success is True
    assert payload["answer"] == "调查完成"
    assert payload["mode"] == "read_only"

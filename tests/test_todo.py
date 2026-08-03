"""验证 Todo 工具的原子更新、约束和 AgentState 同步。

任务流位置：覆盖“模型工具调用 → TodoList → Agent Loop 状态 → 检查点数据”的
Day 3 计划管理路径，不访问真实模型或网络。
"""

from pathlib import Path

from harness.agent.loop import AgentLoop
from harness.providers.base import AssistantTurn, ToolCall
from harness.providers.mock import MockProvider
from harness.safety.permissions import PermissionPolicy
from harness.tasks.todo import TodoList
from harness.tools import build_default_registry


def test_todo_tools_replace_and_read_list(workspace: Path) -> None:
    """验证模型可原子写入并重新读取同一份 Todo 清单。"""

    registry = build_default_registry(
        workspace,
        permission_policy=PermissionPolicy(default="allow"),
    )
    written = registry.dispatch(
        "todo_write",
        {
            "items": [
                {"id": "inspect", "content": "阅读代码", "status": "completed"},
                {"id": "test", "content": "运行测试", "status": "in_progress"},
            ]
        },
    )
    read = registry.dispatch("todo_read", {})

    assert written.success is True
    assert read.success is True
    assert '"id": "test"' in read.content
    assert '"status": "in_progress"' in read.content


def test_todo_rejects_duplicate_ids_and_multiple_active_items(
    workspace: Path,
) -> None:
    """验证不一致的完整清单不会覆盖已有 Todo 状态。"""

    todo_list = TodoList(
        [{"id": "safe", "content": "保留", "status": "pending"}]
    )
    registry = build_default_registry(
        workspace,
        todo_list=todo_list,
        permission_policy=PermissionPolicy(default="allow"),
    )

    duplicate = registry.dispatch(
        "todo_write",
        {
            "items": [
                {"id": "same", "content": "A", "status": "pending"},
                {"id": "same", "content": "B", "status": "pending"},
            ]
        },
    )
    multiple_active = registry.dispatch(
        "todo_write",
        {
            "items": [
                {"id": "a", "content": "A", "status": "in_progress"},
                {"id": "b", "content": "B", "status": "in_progress"},
            ]
        },
    )

    assert duplicate.success is False
    assert multiple_active.success is False
    assert todo_list.snapshot()[0]["id"] == "safe"


def test_agent_loop_copies_todo_state_after_tool_call(workspace: Path) -> None:
    """验证 Todo 工具执行结果会进入最终 AgentState。"""

    provider = MockProvider(
        responses=[
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="todo-1",
                        name="todo_write",
                        arguments={
                            "items": [
                                {
                                    "id": "implement",
                                    "content": "完成 Day 3",
                                    "status": "in_progress",
                                }
                            ]
                        },
                    )
                ]
            ),
            AssistantTurn(content="计划已记录。"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=build_default_registry(
            workspace,
            permission_policy=PermissionPolicy(default="allow"),
        ),
        system_prompt="test",
    )

    result = loop.run("记录计划")

    assert result.state.todos == [
        {
            "id": "implement",
            "content": "完成 Day 3",
            "status": "in_progress",
        }
    ]

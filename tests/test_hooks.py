"""验证 PreToolUse 和 PostToolUse Hook 的顺序、拒绝与错误隔离。

任务流位置：通过 Tool Registry 注册 Hook，确认权限允许后先运行前置 Hook，
工具执行完成后再运行后置 Hook，并检查两类 Hook 的不同失败语义。
"""

from pathlib import Path

from harness.safety.hooks import HookManager, PreToolUseResult
from harness.safety.permissions import PermissionPolicy
from harness.tools import build_default_registry


def test_hooks_run_before_and_after_tool(workspace: Path) -> None:
    """验证前置和后置 Hook 围绕一次成功工具执行按顺序运行。"""

    events: list[str] = []

    def before(event):
        """记录前置阶段并检查工具调用快照。"""

        events.append(f"pre:{event.tool_name}")
        assert event.workspace == workspace
        return None

    def after(event) -> None:
        """记录后置阶段并检查成功结果。"""

        events.append(f"post:{event.tool_name}:{event.success}")

    hooks = HookManager(
        pre_tool_use=[before],
        post_tool_use=[after],
    )
    registry = build_default_registry(workspace, hooks=hooks)

    result = registry.dispatch("read_file", {"path": "src/sample.py"})

    assert result.success
    assert events == ["pre:read_file", "post:read_file:True"]


def test_pre_tool_use_can_reject_before_execution(workspace: Path) -> None:
    """验证 PreToolUse 拒绝结果会阻止具体工具产生副作用。"""

    def reject_write(event) -> PreToolUseResult:
        """拒绝所有传入的写文件调用。"""

        return PreToolUseResult(allow=False, reason="writes disabled")

    registry = build_default_registry(
        workspace,
        hooks=HookManager(pre_tool_use=[reject_write]),
        permission_policy=PermissionPolicy(default="allow"),
    )

    result = registry.dispatch(
        "write_file",
        {"path": "hook-blocked.txt", "content": "blocked"},
    )

    assert result.error_type == "pre_hook_rejected"
    assert "writes disabled" in result.content
    assert not (workspace / "hook-blocked.txt").exists()


def test_post_tool_use_failure_does_not_hide_tool_result(
    workspace: Path,
) -> None:
    """验证后置 Hook 异常被记录，但成功工具结果保持成功。"""

    def broken_post(event) -> None:
        """模拟后置观察器自身发生异常。"""

        raise RuntimeError("observer failed")

    registry = build_default_registry(
        workspace,
        hooks=HookManager(post_tool_use=[broken_post]),
    )

    result = registry.dispatch("read_file", {"path": "src/sample.py"})

    assert result.success
    assert len(result.hook_errors) == 1
    assert "observer failed" in result.hook_errors[0]


def test_pre_tool_use_exception_fails_closed(workspace: Path) -> None:
    """验证前置 Hook 意外异常时工具不会继续执行。"""

    def broken_pre(event):
        """模拟前置安全检查自身发生异常。"""

        raise RuntimeError("guard failed")

    registry = build_default_registry(
        workspace,
        hooks=HookManager(pre_tool_use=[broken_pre]),
        permission_policy=PermissionPolicy(default="allow"),
    )

    result = registry.dispatch(
        "write_file",
        {"path": "pre-error.txt", "content": "blocked"},
    )

    assert result.error_type == "pre_hook_error"
    assert not (workspace / "pre-error.txt").exists()

"""验证 Tool Registry 的参数拒绝、未知工具处理和 schema 导出。

任务流位置：位于模型 ToolCall 与具体工具执行之间，确认非法调用会转换成结构化
失败数据，而合法注册信息能够提供给 Provider 作为 function schema。
"""

from harness.tools.registry import ToolRegistry


def test_registry_rejects_missing_required_arguments(
    registry: ToolRegistry,
) -> None:
    """验证注册表拒绝缺少必填字段的工具参数。"""

    result = registry.dispatch("read_file", {})

    assert result.success is False
    assert result.error_type == "validation_error"
    assert "path" in result.content


def test_registry_rejects_extra_arguments(registry: ToolRegistry) -> None:
    """验证严格参数模型拒绝工具 schema 之外的额外字段。"""

    result = registry.dispatch(
        "read_file",
        {"path": "src/sample.py", "unexpected": True},
    )

    assert result.success is False
    assert result.error_type == "validation_error"
    assert "extra_forbidden" in result.content


def test_registry_returns_unknown_tool_as_data(
    registry: ToolRegistry,
) -> None:
    """验证未知工具被转换成结构化失败结果而不是抛出异常。"""

    result = registry.dispatch("does_not_exist", {})

    assert result.success is False
    assert result.error_type == "unknown_tool"
    assert result.duration_ms >= 0


def test_registry_exports_openai_function_schemas(
    registry: ToolRegistry,
) -> None:
    """验证注册表完整导出七种默认工具的 function schemas。"""

    schemas = registry.schemas()
    names = [schema["function"]["name"] for schema in schemas]

    assert names == [
        "read_file",
        "write_file",
        "edit_file",
        "search",
        "apply_patch",
        "shell",
        "run_tests",
    ]
    assert all(schema["type"] == "function" for schema in schemas)

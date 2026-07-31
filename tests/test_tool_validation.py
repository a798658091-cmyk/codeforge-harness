from harness.tools.registry import ToolRegistry


def test_registry_rejects_missing_required_arguments(
    registry: ToolRegistry,
) -> None:
    result = registry.dispatch("read_file", {})

    assert result.success is False
    assert result.error_type == "validation_error"
    assert "path" in result.content


def test_registry_rejects_extra_arguments(registry: ToolRegistry) -> None:
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
    result = registry.dispatch("does_not_exist", {})

    assert result.success is False
    assert result.error_type == "unknown_tool"
    assert result.duration_ms >= 0


def test_registry_exports_openai_function_schemas(
    registry: ToolRegistry,
) -> None:
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

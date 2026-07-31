from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Iterable

from pydantic import ValidationError

from harness.tools.base import (
    BaseTool,
    ToolContext,
    ToolError,
    ToolOutput,
)


@dataclass(frozen=True)
class ToolExecutionResult:
    tool_name: str
    success: bool
    content: str
    duration_ms: float
    error_type: str | None = None


class ToolRegistry:
    """Validates and dispatches every tool through one narrow boundary."""

    def __init__(
        self,
        context: ToolContext,
        tools: Iterable[BaseTool] | None = None,
    ) -> None:
        self.context = context
        self._tools: dict[str, BaseTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.openai_schema() for tool in self._tools.values()]

    def dispatch(
        self,
        name: str,
        raw_arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        started = perf_counter()
        tool = self._tools.get(name)
        if tool is None:
            return self._result(
                name=name,
                started=started,
                success=False,
                content=f"unknown tool: {name}",
                error_type="unknown_tool",
            )
        try:
            arguments = tool.arguments_model.model_validate(raw_arguments)
            outcome = tool.execute(arguments, self.context)
            if isinstance(outcome, ToolOutput):
                return self._result(
                    name=name,
                    started=started,
                    success=outcome.success,
                    content=outcome.content,
                    error_type=None if outcome.success else "tool_error",
                )
            return self._result(
                name=name,
                started=started,
                success=True,
                content=str(outcome),
            )
        except ValidationError as exc:
            return self._result(
                name=name,
                started=started,
                success=False,
                content=exc.json(),
                error_type="validation_error",
            )
        except (ToolError, OSError, ValueError) as exc:
            return self._result(
                name=name,
                started=started,
                success=False,
                content=str(exc),
                error_type=type(exc).__name__,
            )
        except Exception as exc:  # keep model loop alive on tool bugs
            return self._result(
                name=name,
                started=started,
                success=False,
                content=f"unexpected tool failure: {type(exc).__name__}: {exc}",
                error_type="internal_error",
            )

    @staticmethod
    def _result(
        *,
        name: str,
        started: float,
        success: bool,
        content: str,
        error_type: str | None = None,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            tool_name=name,
            success=success,
            content=content,
            duration_ms=round((perf_counter() - started) * 1000, 3),
            error_type=error_type,
        )

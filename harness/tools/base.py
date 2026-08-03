"""定义工具参数、执行上下文、统一输出和抽象工具接口。

任务流位置：位于 Tool Registry 与所有具体工具实现之间；注册表依赖这些公共
类型完成 schema 导出、参数校验和执行结果归一化。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

from harness.safety.workspace import Workspace

if TYPE_CHECKING:
    from harness.tasks.todo import TodoList


class ToolArguments(BaseModel):
    """所有工具参数模型的严格校验基类。"""

    model_config = ConfigDict(extra="forbid")


class ToolError(RuntimeError):
    """工具可预期业务失败使用的统一异常。"""

    pass


@dataclass(frozen=True)
class ToolContext:
    """向具体工具传递 workspace 等共享执行依赖。"""

    workspace: Workspace
    todo_list: TodoList | None = None


@dataclass(frozen=True)
class ToolOutput:
    """表示工具主动返回的文本内容和成功状态。"""

    content: str
    success: bool = True


class BaseTool(ABC):
    """定义工具名称、描述、参数模型和执行方法的抽象基类。"""

    name: ClassVar[str]
    description: ClassVar[str]
    arguments_model: ClassVar[type[ToolArguments]]

    def openai_schema(self) -> dict[str, Any]:
        """把工具元数据转换为 OpenAI function-calling schema。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.arguments_model.model_json_schema(),
            },
        }

    @abstractmethod
    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str | ToolOutput:
        """在给定上下文中执行经过校验的工具参数。"""

        raise NotImplementedError

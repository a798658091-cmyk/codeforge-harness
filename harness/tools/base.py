from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from harness.safety.workspace import Workspace


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolContext:
    workspace: Workspace


@dataclass(frozen=True)
class ToolOutput:
    content: str
    success: bool = True


class BaseTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    arguments_model: ClassVar[type[ToolArguments]]

    def openai_schema(self) -> dict[str, Any]:
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
        raise NotImplementedError

from __future__ import annotations

import json
from typing import Any

from harness.providers.base import AssistantTurn, ModelProvider, ToolCall


class OpenAICompatibleProvider(ModelProvider):
    """OpenAI chat-completions adapter, also usable with DeepSeek."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_tokens: int = 4096,
        client: Any | None = None,
    ) -> None:
        if not model:
            raise ValueError("model is required")
        if not api_key and client is None:
            raise ValueError("api_key is required")
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        response = self.client.chat.completions.create(**request)
        choice = response.choices[0]
        message = choice.message
        normalized_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"model returned invalid JSON for tool {call.function.name}"
                ) from exc
            if not isinstance(arguments, dict):
                raise ValueError(
                    f"tool arguments for {call.function.name} must be an object"
                )
            normalized_calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=arguments,
                )
            )

        usage = {}
        if response.usage is not None:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }
        return AssistantTurn(
            content=message.content or "",
            tool_calls=normalized_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )

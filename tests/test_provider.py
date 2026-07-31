from __future__ import annotations

from types import SimpleNamespace

from harness.providers.openai_compatible import OpenAICompatibleProvider


class FakeCompletions:
    def __init__(self) -> None:
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        tool_call = SimpleNamespace(
            id="call-7",
            function=SimpleNamespace(
                name="read_file",
                arguments='{"path": "README.md"}',
            ),
        )
        message = SimpleNamespace(content=None, tool_calls=[tool_call])
        choice = SimpleNamespace(
            message=message,
            finish_reason="tool_calls",
        )
        usage = SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=3,
            total_tokens=14,
        )
        return SimpleNamespace(choices=[choice], usage=usage)


def test_openai_compatible_provider_normalizes_response() -> None:
    completions = FakeCompletions()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    provider = OpenAICompatibleProvider(
        model="test-model",
        api_key="",
        client=client,
    )

    turn = provider.complete(
        [{"role": "user", "content": "read"}],
        [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"path": "README.md"}
    assert turn.usage["total_tokens"] == 14
    assert completions.request["model"] == "test-model"
    assert completions.request["tool_choice"] == "auto"

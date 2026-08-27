from __future__ import annotations

import unittest
from types import SimpleNamespace

from code_agent.model import ModelEventType
from code_agent.openrouter import (
    OpenRouterConfig,
    OpenRouterConfigurationError,
    OpenRouterProvider,
)


class _FakeCompletions:
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self.chunks = chunks
        self.request: dict[str, object] | None = None

    def create(self, **kwargs: object) -> list[SimpleNamespace]:
        self.request = kwargs
        return self.chunks


class _FakeClient:
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self.completions = _FakeCompletions(chunks)
        self.chat = SimpleNamespace(completions=self.completions)


def _chunk(
    *,
    text: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
    finish_reason: str | None = None,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    choices = []
    if text is not None or tool_calls is not None or finish_reason is not None:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(choices=choices, usage=usage)


class OpenRouterConfigTests(unittest.TestCase):
    def test_environment_configuration_uses_fixed_model(self) -> None:
        config = OpenRouterConfig.from_env({"OPENROUTER_API_KEY": "secret"})

        self.assertEqual(config.model, "google/gemini-3.7-flash")
        self.assertEqual(config.context_budget, 32_000)
        self.assertNotIn("secret", repr(config))

    def test_missing_api_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(OpenRouterConfigurationError, "not set"):
            OpenRouterConfig.from_env({})


class OpenRouterProviderTests(unittest.TestCase):
    def test_stream_normalizes_text_tool_calls_and_usage(self) -> None:
        first_tool_delta = SimpleNamespace(
            index=0,
            id="call-1",
            function=SimpleNamespace(name="read_file", arguments='{"path":'),
        )
        second_tool_delta = SimpleNamespace(
            index=0,
            id=None,
            function=SimpleNamespace(name=None, arguments='"README.md"}'),
        )
        chunks = [
            _chunk(text="Checking "),
            _chunk(text="the repository."),
            _chunk(tool_calls=[first_tool_delta]),
            _chunk(tool_calls=[second_tool_delta], finish_reason="tool_calls"),
            _chunk(
                usage=SimpleNamespace(
                    prompt_tokens=100,
                    completion_tokens=20,
                    total_tokens=120,
                )
            ),
        ]
        client = _FakeClient(chunks)
        provider = OpenRouterProvider(
            OpenRouterConfig(api_key="secret"),
            client=client,
        )

        events = list(
            provider.stream(
                [{"role": "user", "content": "Inspect the repository"}],
                [{"type": "function", "function": {"name": "read_file"}}],
            )
        )

        text = "".join(
            event.text or ""
            for event in events
            if event.event_type is ModelEventType.TEXT_DELTA
        )
        tool_call = next(
            event.tool_call
            for event in events
            if event.event_type is ModelEventType.TOOL_CALL
        )
        usage = next(
            event.usage
            for event in events
            if event.event_type is ModelEventType.USAGE
        )
        self.assertEqual(text, "Checking the repository.")
        self.assertEqual(tool_call.arguments, {"path": "README.md"})
        self.assertEqual(usage.total_tokens, 120)
        self.assertEqual(events[-1].finish_reason, "tool_calls")
        self.assertEqual(client.completions.request["stream"], True)


if __name__ == "__main__":
    unittest.main()


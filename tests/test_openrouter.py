from __future__ import annotations

import unittest
from types import SimpleNamespace

from openai import OpenAIError

from code_agent.model import ModelEventType
from code_agent.openrouter import (
    OpenRouterConfig,
    OpenRouterConfigurationError,
    OpenRouterProvider,
    OpenRouterRequestError,
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


class _SequenceCompletions:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _request_error(status_code: int | None) -> OpenAIError:
    error = OpenAIError("synthetic request failure")
    error.status_code = status_code
    return error


def _stream_then_error(
    chunks: list[SimpleNamespace],
    error: OpenAIError,
):
    yield from chunks
    raise error


class _SummaryCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.request: dict[str, object] | None = None

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.request = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def _chunk(
    *,
    text: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
    reasoning_details: list[object] | None = None,
    finish_reason: str | None = None,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    choices = []
    if (
        text is not None
        or tool_calls is not None
        or reasoning_details is not None
        or finish_reason is not None
    ):
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=text,
                    tool_calls=tool_calls,
                    reasoning_details=reasoning_details,
                ),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(choices=choices, usage=usage)


class OpenRouterConfigTests(unittest.TestCase):
    def test_environment_configuration_uses_fixed_model(self) -> None:
        config = OpenRouterConfig.from_env({"OPENROUTER_API_KEY": "secret"})

        self.assertEqual(config.model, "google/gemini-3.7-flash")
        self.assertEqual(config.context_budget, 32_000)
        self.assertEqual(config.max_steps, 16)
        self.assertEqual(config.max_retries, 2)
        self.assertEqual(config.reasoning_effort, "medium")
        self.assertNotIn("secret", repr(config))

    def test_missing_api_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(OpenRouterConfigurationError, "not set"):
            OpenRouterConfig.from_env({})

    def test_max_steps_is_bounded(self) -> None:
        for value in ("not-an-integer", "3", "65"):
            with self.subTest(value=value):
                with self.assertRaises(OpenRouterConfigurationError):
                    OpenRouterConfig.from_env(
                        {
                            "OPENROUTER_API_KEY": "secret",
                            "LCTI_MAX_STEPS": value,
                        }
                    )

    def test_openrouter_retries_are_bounded(self) -> None:
        for value in ("not-an-integer", "-1", "6"):
            with self.subTest(value=value):
                with self.assertRaises(OpenRouterConfigurationError):
                    OpenRouterConfig.from_env(
                        {
                            "OPENROUTER_API_KEY": "secret",
                            "LCTI_OPENROUTER_RETRIES": value,
                        }
                    )

    def test_reasoning_effort_is_validated(self) -> None:
        for value in ("none", "xhigh", "invalid"):
            with self.subTest(value=value):
                with self.assertRaises(OpenRouterConfigurationError):
                    OpenRouterConfig.from_env(
                        {
                            "OPENROUTER_API_KEY": "secret",
                            "LCTI_REASONING_EFFORT": value,
                        }
                    )


class OpenRouterProviderTests(unittest.TestCase):
    def test_stream_emits_only_reasoning_summaries_and_preserves_all_details(
        self,
    ) -> None:
        chunks = [
            _chunk(
                reasoning_details=[
                    SimpleNamespace(
                        type="reasoning.summary",
                        summary="先确认任务约束。",
                        id="summary-1",
                        format="google-gemini-v1",
                        index=0,
                    )
                ]
            ),
            _chunk(
                reasoning_details=[
                    SimpleNamespace(
                        type="reasoning.text",
                        text="raw internal reasoning",
                        signature="signature-1",
                        id="text-1",
                        format="google-gemini-v1",
                        index=1,
                    ),
                    {
                        "type": "reasoning.encrypted",
                        "data": "encrypted-data",
                        "id": "encrypted-1",
                        "format": "google-gemini-v1",
                        "index": 2,
                    },
                ]
            ),
            _chunk(text="完成。", finish_reason="stop"),
        ]
        client = _FakeClient(chunks)
        provider = OpenRouterProvider(
            OpenRouterConfig(api_key="secret", reasoning_effort="medium"),
            client=client,
        )

        events = list(provider.stream([{"role": "user", "content": "go"}], []))

        reasoning = [
            event
            for event in events
            if event.event_type is ModelEventType.REASONING_DELTA
        ]
        self.assertEqual(len(reasoning), 2)
        self.assertEqual(reasoning[0].text, "先确认任务约束。")
        self.assertEqual(reasoning[0].reasoning_kind, "summary")
        self.assertEqual(reasoning[1].text, "raw internal reasoning")
        self.assertEqual(reasoning[1].reasoning_kind, "provider_text")
        details = [detail for event in reasoning for detail in event.reasoning_details]
        self.assertEqual(
            [detail["type"] for detail in details],
            ["reasoning.summary", "reasoning.text", "reasoning.encrypted"],
        )
        self.assertEqual(details[1]["text"], "raw internal reasoning")
        self.assertEqual(
            client.completions.request["extra_body"]["reasoning"],
            {"effort": "medium", "exclude": False},
        )

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

    def test_retries_retryable_request_before_any_output(self) -> None:
        completions = _SequenceCompletions(
            [
                _request_error(429),
                [_chunk(text="Recovered."), _chunk(finish_reason="stop")],
            ]
        )
        sleeps: list[float] = []
        provider = OpenRouterProvider(
            OpenRouterConfig(api_key="secret", max_retries=2),
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            sleep_fn=sleeps.append,
        )

        events = list(provider.stream([{"role": "user", "content": "go"}], []))

        self.assertEqual(len(completions.requests), 2)
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(events[0].text, "Recovered.")
        self.assertEqual(events[-1].finish_reason, "stop")

    def test_does_not_retry_non_retryable_request(self) -> None:
        completions = _SequenceCompletions([_request_error(400)])
        sleeps: list[float] = []
        provider = OpenRouterProvider(
            OpenRouterConfig(api_key="secret", max_retries=2),
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            sleep_fn=sleeps.append,
        )

        with self.assertRaisesRegex(OpenRouterRequestError, "HTTP 400"):
            list(provider.stream([{"role": "user", "content": "go"}], []))

        self.assertEqual(len(completions.requests), 1)
        self.assertEqual(sleeps, [])

    def test_retry_budget_is_finite_and_error_is_sanitized(self) -> None:
        completions = _SequenceCompletions(
            [_request_error(503), _request_error(503), _request_error(503)]
        )
        sleeps: list[float] = []
        provider = OpenRouterProvider(
            OpenRouterConfig(api_key="secret", max_retries=2),
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            sleep_fn=sleeps.append,
        )

        with self.assertRaises(OpenRouterRequestError) as raised:
            list(provider.stream([{"role": "user", "content": "go"}], []))

        self.assertEqual(len(completions.requests), 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertIn("HTTP 503", str(raised.exception))
        self.assertNotIn("synthetic request failure", str(raised.exception))

    def test_does_not_retry_after_partial_text_was_emitted(self) -> None:
        completions = _SequenceCompletions(
            [
                _stream_then_error(
                    [_chunk(text="partial")],
                    _request_error(429),
                ),
                [_chunk(text="duplicate"), _chunk(finish_reason="stop")],
            ]
        )
        sleeps: list[float] = []
        provider = OpenRouterProvider(
            OpenRouterConfig(api_key="secret", max_retries=2),
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            sleep_fn=sleeps.append,
        )

        with self.assertRaisesRegex(OpenRouterRequestError, "HTTP 429"):
            list(provider.stream([{"role": "user", "content": "go"}], []))

        self.assertEqual(len(completions.requests), 1)
        self.assertEqual(sleeps, [])

    def test_finish_reason_error_is_explicit_and_empty_attempt_retries(self) -> None:
        completions = _SequenceCompletions(
            [
                [_chunk(finish_reason="error")],
                [_chunk(text="Recovered."), _chunk(finish_reason="stop")],
            ]
        )
        sleeps: list[float] = []
        provider = OpenRouterProvider(
            OpenRouterConfig(api_key="secret", max_retries=1),
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            sleep_fn=sleeps.append,
        )

        events = list(provider.stream([{"role": "user", "content": "go"}], []))

        self.assertEqual(len(completions.requests), 2)
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(events[0].text, "Recovered.")

    def test_finish_reason_error_after_output_is_not_retried(self) -> None:
        completions = _SequenceCompletions(
            [[_chunk(text="partial"), _chunk(finish_reason="error")]]
        )
        provider = OpenRouterProvider(
            OpenRouterConfig(api_key="secret", max_retries=2),
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            sleep_fn=lambda seconds: None,
        )

        with self.assertRaisesRegex(OpenRouterRequestError, "finish_reason=error"):
            list(provider.stream([{"role": "user", "content": "go"}], []))

        self.assertEqual(len(completions.requests), 1)

    def test_summary_request_retries_before_parsing(self) -> None:
        content = (
            '{"version":1,"objective":"continue","completed":[],'
            '"decisions":[],"files":[],"identifiers":[],"commands":[],'
            '"exit_codes":[],"open_errors":[],"next_actions":[],'
            '"event_ids":[]}'
        )
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=content),
                )
            ]
        )
        completions = _SequenceCompletions([_request_error(503), response])
        sleeps: list[float] = []
        provider = OpenRouterProvider(
            OpenRouterConfig(api_key="secret", max_retries=1),
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            sleep_fn=sleeps.append,
        )

        summary = provider.summarize_context(
            [{"role": "user", "content": "continue"}]
        )

        self.assertEqual(summary["version"], 1)
        self.assertEqual(len(completions.requests), 2)
        self.assertEqual(sleeps, [0.5])

    def test_summary_sends_fixed_schema_and_requires_json_object(self) -> None:
        content = (
            '{"version":1,"objective":"continue","completed":[],'
            '"decisions":[],"files":[],"identifiers":[],"commands":[],'
            '"exit_codes":[],"open_errors":[],"next_actions":[],'
            '"event_ids":[]}'
        )
        completions = _SummaryCompletions(content)
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        provider = OpenRouterProvider(OpenRouterConfig(api_key="secret"), client=client)

        summary = provider.summarize_context([{"role": "user", "content": "continue"}])

        self.assertEqual(summary["version"], 1)
        self.assertEqual(
            completions.request["response_format"],
            {"type": "json_object"},
        )
        request_content = completions.request["messages"][1]["content"]
        self.assertIn('"required_schema"', request_content)
        self.assertIn('"additionalProperties":false', request_content)
        self.assertFalse(completions.request["stream"])


if __name__ == "__main__":
    unittest.main()

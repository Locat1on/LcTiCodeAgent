"""OpenRouter Chat Completions provider with local tool-call parsing."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from openai import OpenAI, OpenAIError

from .context import CompactionStrategy
from .model import (
    ModelEvent,
    ModelEventType,
    ModelUsage,
    ToolCallAccumulator,
)
from .summary import SUMMARY_SCHEMA, SUMMARY_SYSTEM_PROMPT


DEFAULT_MODEL = "google/gemini-3.7-flash"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
RETRY_BASE_SECONDS = 0.5
RETRYABLE_STATUS_CODES = {408, 409, 429}
REASONING_EFFORTS = {"minimal", "low", "medium", "high"}
PLAIN_SUMMARY_PROMPT = """Summarize the older coding-agent conversation for continuation.
Preserve the user objective, constraints, files, identifiers, completed work, command
outcomes, errors, and next actions. Return concise plain text. Do not use JSON.
"""


class OpenRouterConfigurationError(ValueError):
    pass


class OpenRouterRequestError(RuntimeError):
    pass


class _ProviderFinishError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OpenRouterConfig:
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    context_budget: int = 32_000
    timeout_seconds: float = 60.0
    max_steps: int = 16
    max_retries: int = 2
    reasoning_effort: str = "medium"
    compaction_strategy: CompactionStrategy = CompactionStrategy.VALIDATED

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OpenRouterConfig:
        values = env if env is not None else os.environ
        api_key = values.get("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise OpenRouterConfigurationError(
                "OPENROUTER_API_KEY is not set; provide it through the environment"
            )
        model = values.get("LCTI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        base_url = values.get("LCTI_BASE_URL", DEFAULT_BASE_URL).strip()
        if not base_url.startswith("https://"):
            raise OpenRouterConfigurationError("LCTI_BASE_URL must use HTTPS")
        try:
            context_budget = int(values.get("LCTI_CONTEXT_BUDGET", "32000"))
        except ValueError as error:
            raise OpenRouterConfigurationError(
                "LCTI_CONTEXT_BUDGET must be an integer"
            ) from error
        if not 4_096 <= context_budget <= 1_048_576:
            raise OpenRouterConfigurationError(
                "LCTI_CONTEXT_BUDGET must be between 4096 and 1048576"
            )
        try:
            max_steps = int(values.get("LCTI_MAX_STEPS", "16"))
        except ValueError as error:
            raise OpenRouterConfigurationError(
                "LCTI_MAX_STEPS must be an integer"
            ) from error
        if not 4 <= max_steps <= 64:
            raise OpenRouterConfigurationError(
                "LCTI_MAX_STEPS must be between 4 and 64"
            )
        try:
            max_retries = int(values.get("LCTI_OPENROUTER_RETRIES", "2"))
        except ValueError as error:
            raise OpenRouterConfigurationError(
                "LCTI_OPENROUTER_RETRIES must be an integer"
            ) from error
        if not 0 <= max_retries <= 5:
            raise OpenRouterConfigurationError(
                "LCTI_OPENROUTER_RETRIES must be between 0 and 5"
            )
        reasoning_effort = values.get("LCTI_REASONING_EFFORT", "medium").strip()
        if reasoning_effort not in REASONING_EFFORTS:
            raise OpenRouterConfigurationError(
                "LCTI_REASONING_EFFORT must be minimal, low, medium, or high"
            )
        try:
            compaction_strategy = CompactionStrategy(
                values.get("LCTI_COMPACTION_STRATEGY", "validated").strip()
            )
        except ValueError as error:
            raise OpenRouterConfigurationError(
                "LCTI_COMPACTION_STRATEGY must be none, drop_oldest, "
                "plain_summary, or validated"
            ) from error
        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url.rstrip("/"),
            context_budget=context_budget,
            max_steps=max_steps,
            max_retries=max_retries,
            reasoning_effort=reasoning_effort,
            compaction_strategy=compaction_strategy,
        )


class OpenRouterProvider:
    def __init__(
        self,
        config: OpenRouterConfig,
        *,
        client: Any | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._sleep = sleep_fn
        self._client = client or OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )

    def stream(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> Iterator[ModelEvent]:
        for attempt in range(self.config.max_retries + 1):
            accumulator = ToolCallAccumulator()
            finish_reason: str | None = None
            output_emitted = False
            try:
                stream = self._client.chat.completions.create(
                    model=self.config.model,
                    messages=list(messages),
                    tools=list(tools),
                    tool_choice="auto",
                    temperature=0.2,
                    stream=True,
                    stream_options={"include_usage": True},
                    extra_body={
                        "provider": {
                            "require_parameters": True,
                            "data_collection": "deny",
                        },
                        "reasoning": {
                            "effort": self.config.reasoning_effort,
                            "exclude": False,
                        },
                    },
                )
                for chunk in stream:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        output_emitted = True
                        yield ModelEvent(
                            ModelEventType.USAGE,
                            usage=ModelUsage(
                                prompt_tokens=usage.prompt_tokens,
                                completion_tokens=usage.completion_tokens,
                                total_tokens=usage.total_tokens,
                            ),
                        )
                    for choice in chunk.choices:
                        delta = choice.delta
                        raw_reasoning = getattr(delta, "reasoning", None)
                        raw_details = getattr(delta, "reasoning_details", None) or []
                        details = tuple(
                            _normalize_reasoning_detail(detail)
                            for detail in raw_details
                        )
                        if raw_reasoning or details:
                            summary = "".join(
                                str(detail.get("summary") or "")
                                for detail in details
                                if detail.get("type") == "reasoning.summary"
                            )
                            provider_text = "".join(
                                str(detail.get("text") or "")
                                for detail in details
                                if detail.get("type") == "reasoning.text"
                            )
                            visible_reasoning = summary or provider_text
                            reasoning_kind = (
                                "summary"
                                if summary
                                else "provider_text"
                                if provider_text
                                else None
                            )
                            output_emitted = True
                            yield ModelEvent(
                                ModelEventType.REASONING_DELTA,
                                text=visible_reasoning or None,
                                reasoning=(
                                    str(raw_reasoning) if raw_reasoning else None
                                ),
                                reasoning_kind=reasoning_kind,
                                reasoning_details=details,
                            )
                        if delta.content:
                            output_emitted = True
                            yield ModelEvent(
                                ModelEventType.TEXT_DELTA,
                                text=delta.content,
                            )
                        for tool_call in delta.tool_calls or []:
                            function = tool_call.function
                            accumulator.add(
                                tool_call.index,
                                call_id=tool_call.id,
                                name_fragment=function.name if function else None,
                                arguments_fragment=(
                                    function.arguments if function else None
                                ),
                            )
                        if choice.finish_reason:
                            finish_reason = choice.finish_reason
                if finish_reason == "error":
                    raise _ProviderFinishError("finish_reason=error")
            except (OpenAIError, _ProviderFinishError) as error:
                if self._should_retry(error, attempt, output_emitted):
                    self._sleep(RETRY_BASE_SECONDS * (2**attempt))
                    continue
                raise self._normalized_error("request", error) from error

            for tool_call in accumulator.finish():
                yield ModelEvent(ModelEventType.TOOL_CALL, tool_call=tool_call)
            yield ModelEvent(
                ModelEventType.COMPLETED,
                finish_reason=finish_reason or "stop",
            )
            return

    def summarize_context(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        """Request one non-streaming JSON summary for strict local validation."""

        response = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "required_schema": SUMMARY_SCHEMA,
                                    "source_messages": list(messages),
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                    stream=False,
                    extra_body={
                        "provider": {
                            "require_parameters": True,
                            "data_collection": "deny",
                        }
                    },
                )
                choice = response.choices[0]
                if getattr(choice, "finish_reason", None) == "error":
                    raise _ProviderFinishError("finish_reason=error")
                break
            except (OpenAIError, _ProviderFinishError) as error:
                if self._should_retry(error, attempt, output_emitted=False):
                    self._sleep(RETRY_BASE_SECONDS * (2**attempt))
                    continue
                raise self._normalized_error("summary request", error) from error
        try:
            parsed = json.loads(response.choices[0].message.content)
        except (AttributeError, IndexError, TypeError, ValueError) as error:
            raise OpenRouterRequestError(
                "OpenRouter summary response was not valid JSON"
            ) from error
        if not isinstance(parsed, dict):
            raise OpenRouterRequestError("OpenRouter summary response was not an object")
        return parsed

    def summarize_context_plain(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> str:
        response = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": PLAIN_SUMMARY_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"source_messages": list(messages)},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    ],
                    temperature=0,
                    stream=False,
                    extra_body={
                        "provider": {
                            "require_parameters": True,
                            "data_collection": "deny",
                        }
                    },
                )
                choice = response.choices[0]
                if getattr(choice, "finish_reason", None) == "error":
                    raise _ProviderFinishError("finish_reason=error")
                break
            except (OpenAIError, _ProviderFinishError) as error:
                if self._should_retry(error, attempt, output_emitted=False):
                    self._sleep(RETRY_BASE_SECONDS * (2**attempt))
                    continue
                raise self._normalized_error("plain summary request", error) from error
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as error:
            raise OpenRouterRequestError(
                "OpenRouter plain summary response was malformed"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise OpenRouterRequestError("OpenRouter plain summary response was empty")
        return content.strip()

    def _should_retry(
        self,
        error: OpenAIError | _ProviderFinishError,
        attempt: int,
        output_emitted: bool,
    ) -> bool:
        if output_emitted or attempt >= self.config.max_retries:
            return False
        if isinstance(error, _ProviderFinishError):
            return True
        status = getattr(error, "status_code", None)
        return (
            status is None
            or status in RETRYABLE_STATUS_CODES
            or isinstance(status, int)
            and status >= 500
        )

    @staticmethod
    def _normalized_error(
        operation: str,
        error: OpenAIError | _ProviderFinishError,
    ) -> OpenRouterRequestError:
        if isinstance(error, _ProviderFinishError):
            return OpenRouterRequestError(
                f"OpenRouter {operation} ended with finish_reason=error"
            )
        status = getattr(error, "status_code", None)
        suffix = f" (HTTP {status})" if status else ""
        return OpenRouterRequestError(
            f"OpenRouter {operation} failed: {type(error).__name__}{suffix}"
        )


def _normalize_reasoning_detail(detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict):
        return dict(detail)
    model_dump = getattr(detail, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, dict):
            return dumped
    values = vars(detail) if hasattr(detail, "__dict__") else {}
    return {
        key: value
        for key, value in values.items()
        if isinstance(key, str)
    }

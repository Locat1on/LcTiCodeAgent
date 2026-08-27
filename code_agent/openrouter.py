"""OpenRouter Chat Completions provider with local tool-call parsing."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI, OpenAIError

from .model import (
    ModelEvent,
    ModelEventType,
    ModelUsage,
    ToolCallAccumulator,
)


DEFAULT_MODEL = "google/gemini-3.7-flash"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterConfigurationError(ValueError):
    pass


class OpenRouterRequestError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OpenRouterConfig:
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    context_budget: int = 32_000
    timeout_seconds: float = 60.0

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
        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url.rstrip("/"),
            context_budget=context_budget,
        )


class OpenRouterProvider:
    def __init__(
        self,
        config: OpenRouterConfig,
        *,
        client: Any | None = None,
    ) -> None:
        self.config = config
        self._client = client or OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )

    def stream(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> Iterator[ModelEvent]:
        accumulator = ToolCallAccumulator()
        finish_reason: str | None = None
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
                    }
                },
            )
            for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
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
                    if delta.content:
                        yield ModelEvent(ModelEventType.TEXT_DELTA, text=delta.content)
                    for tool_call in delta.tool_calls or []:
                        function = tool_call.function
                        accumulator.add(
                            tool_call.index,
                            call_id=tool_call.id,
                            name_fragment=function.name if function else None,
                            arguments_fragment=function.arguments if function else None,
                        )
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
        except OpenAIError as error:
            status = getattr(error, "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise OpenRouterRequestError(
                f"OpenRouter request failed: {type(error).__name__}{suffix}"
            ) from error

        for tool_call in accumulator.finish():
            yield ModelEvent(ModelEventType.TOOL_CALL, tool_call=tool_call)
        yield ModelEvent(
            ModelEventType.COMPLETED,
            finish_reason=finish_reason or "stop",
        )


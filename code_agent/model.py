"""Provider-neutral model events and streamed tool-call assembly."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ModelEventType(StrEnum):
    TEXT_DELTA = "text.delta"
    TOOL_CALL = "tool.call"
    USAGE = "usage"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


@dataclass(frozen=True, slots=True)
class ModelUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class ModelEvent:
    event_type: ModelEventType
    text: str | None = None
    tool_call: ModelToolCall | None = None
    usage: ModelUsage | None = None
    finish_reason: str | None = None


class ToolCallParseError(ValueError):
    pass


@dataclass(slots=True)
class _ToolCallBuffer:
    call_id: str = ""
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)


class ToolCallAccumulator:
    """Reconstruct function calls whose fields arrive across stream chunks."""

    def __init__(self) -> None:
        self._calls: dict[int, _ToolCallBuffer] = {}

    def add(
        self,
        index: int,
        *,
        call_id: str | None = None,
        name_fragment: str | None = None,
        arguments_fragment: str | None = None,
    ) -> None:
        if index < 0:
            raise ToolCallParseError("tool call index must be non-negative")
        buffer = self._calls.setdefault(index, _ToolCallBuffer())
        if call_id:
            if buffer.call_id and buffer.call_id != call_id:
                raise ToolCallParseError(f"conflicting ids for tool call {index}")
            buffer.call_id = call_id
        if name_fragment:
            buffer.name_parts.append(name_fragment)
        if arguments_fragment:
            buffer.argument_parts.append(arguments_fragment)

    def finish(self) -> list[ModelToolCall]:
        calls: list[ModelToolCall] = []
        seen_ids: set[str] = set()
        for index, buffer in sorted(self._calls.items()):
            name = "".join(buffer.name_parts)
            raw_arguments = "".join(buffer.argument_parts) or "{}"
            if not buffer.call_id:
                raise ToolCallParseError(f"tool call {index} has no id")
            if buffer.call_id in seen_ids:
                raise ToolCallParseError(
                    f"duplicate tool call id: {buffer.call_id}"
                )
            seen_ids.add(buffer.call_id)
            if not name:
                raise ToolCallParseError(f"tool call {index} has no function name")
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as error:
                raise ToolCallParseError(
                    f"tool call {index} contains invalid JSON arguments"
                ) from error
            if not isinstance(arguments, dict):
                raise ToolCallParseError(
                    f"tool call {index} arguments must be a JSON object"
                )
            calls.append(
                ModelToolCall(
                    call_id=buffer.call_id,
                    name=name,
                    arguments=arguments,
                    raw_arguments=raw_arguments,
                )
            )
        return calls


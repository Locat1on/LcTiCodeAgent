"""Deterministic projection of session-log events back into agent context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .context import ContextManager
from .events import AgentEvent, EventType


INTERRUPTED_TOOL_RESULT = (
    "tool execution was interrupted before a result was recorded; "
    "session restored - re-run the tool call if still needed"
)


class RestoreError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Projection:
    context: ContextManager
    used_tokens: int
    interrupted_tool_calls: int


@dataclass(frozen=True, slots=True)
class RestoreReport:
    events_replayed: int
    context_items: int
    estimated_tokens: int
    used_tokens: int
    interrupted_tool_calls: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "events_replayed": self.events_replayed,
            "context_items": self.context_items,
            "estimated_tokens": self.estimated_tokens,
            "used_tokens": self.used_tokens,
            "interrupted_tool_calls": self.interrupted_tool_calls,
        }


def project_session(
    events: list[AgentEvent],
    system_prompt: str,
) -> Projection:
    if not events:
        raise RestoreError("session log is empty")
    if events[0].event_type is not EventType.SESSION_STARTED:
        raise RestoreError("session log does not start with session.started")
    session_id = events[0].session_id
    for index, event in enumerate(events):
        if event.session_id != session_id:
            raise RestoreError(
                f"event at position {index} belongs to a different session"
            )

    context = ContextManager(system_prompt)
    used_tokens = 0
    interrupted = 0
    arguments_by_call: dict[str, dict[str, Any]] = {}
    declared: dict[str, dict[str, Any]] = {}
    results_seen: set[str] = set()

    for index, event in enumerate(events):
        try:
            if event.event_type is EventType.USER_MESSAGE:
                context.add_user(event.payload["text"])
            elif event.event_type is EventType.ASSISTANT_MESSAGE:
                calls = event.payload.get("tool_calls")
                context.add_assistant(event.payload.get("text", ""), calls)
                for call in calls or []:
                    declared[call["id"]] = {
                        "name": call["function"]["name"],
                        "arguments": _parse_raw_arguments(
                            call["function"].get("arguments")
                        ),
                    }
            elif event.event_type is EventType.TOOL_REQUESTED:
                arguments_by_call[
                    event.payload["call_id"]
                ] = event.payload.get("arguments") or {}
            elif event.event_type in (
                EventType.TOOL_COMPLETED,
                EventType.TOOL_FAILED,
            ):
                call_id = event.payload["call_id"]
                if call_id not in declared:
                    raise RestoreError(
                        "tool result references a call that no assistant message "
                        "declared; the log predates the resumable format or is corrupt"
                    )
                context.add_tool(
                    call_id=call_id,
                    tool_name=event.payload["name"],
                    arguments=arguments_by_call.get(call_id),
                    content=json.dumps(
                        {
                            "ok": event.event_type is EventType.TOOL_COMPLETED,
                            "result": event.payload["content"],
                        },
                        ensure_ascii=False,
                    ),
                    source_event_id=event.event_id,
                )
                results_seen.add(call_id)
            elif event.event_type is EventType.CONTEXT_CLEARED:
                context = ContextManager(system_prompt)
                used_tokens = 0
                arguments_by_call = {}
                declared = {}
                results_seen = set()
            elif event.event_type is EventType.CONTEXT_USAGE:
                used_tokens = int(event.payload["used_tokens"])
        except RestoreError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RestoreError(
                f"malformed {event.event_type.value} event "
                f"at position {index}: {error}"
            ) from error

    for call_id, info in declared.items():
        if call_id in results_seen:
            continue
        context.add_tool(
            call_id=call_id,
            tool_name=info["name"],
            arguments=arguments_by_call.get(call_id, info["arguments"]),
            content=json.dumps(
                {"ok": False, "result": INTERRUPTED_TOOL_RESULT},
                ensure_ascii=False,
            ),
            source_event_id=None,
        )
        interrupted += 1

    context.refresh_state()
    return Projection(
        context=context,
        used_tokens=used_tokens,
        interrupted_tool_calls=interrupted,
    )


def _parse_raw_arguments(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}

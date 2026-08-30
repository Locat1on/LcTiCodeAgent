"""Shared event protocol for the agent, terminal UI, and session log."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class EventType(StrEnum):
    SESSION_STARTED = "session.started"
    SESSION_RESUMED = "session.resumed"
    USER_MESSAGE = "user.message"
    ASSISTANT_DELTA = "assistant.delta"
    ASSISTANT_REASONING_DELTA = "assistant.reasoning_delta"
    ASSISTANT_MESSAGE = "assistant.message"
    TOOL_REQUESTED = "tool.requested"
    TOOL_APPROVAL_REQUIRED = "tool.approval_required"
    TOOL_APPROVAL_DECIDED = "tool.approval_decided"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    CONTEXT_USAGE = "context.usage"
    CONTEXT_CLEARED = "context.cleared"
    CONTEXT_COMPACTION_STARTED = "context.compaction_started"
    CONTEXT_COMPACTION_COMPLETED = "context.compaction_completed"
    TURN_COMPLETED = "turn.completed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    event_id: str
    session_id: str
    event_type: EventType
    timestamp: str
    payload: dict[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    step_id: str | None = None

    @classmethod
    def create(
        cls,
        event_type: EventType,
        session_id: str,
        payload: dict[str, Any] | None = None,
        *,
        turn_id: str | None = None,
        step_id: str | None = None,
    ) -> AgentEvent:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        return cls(
            event_id=str(uuid4()),
            session_id=session_id,
            event_type=event_type,
            timestamp=datetime.now(UTC).isoformat(),
            payload=payload or {},
            turn_id=turn_id,
            step_id=step_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentEvent:
        return cls(
            event_id=data["event_id"],
            session_id=data["session_id"],
            turn_id=data.get("turn_id"),
            step_id=data.get("step_id"),
            event_type=EventType(data["event_type"]),
            timestamp=data["timestamp"],
            payload=data.get("payload", {}),
        )

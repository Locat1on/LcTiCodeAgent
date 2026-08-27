"""Append-only JSONL persistence for agent events."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from .events import AgentEvent


class SessionLog:
    def __init__(
        self,
        root: Path,
        session_id: str | None = None,
    ) -> None:
        self.root = root.resolve()
        self.session_id = session_id or str(uuid4())
        self.path = self.root / f"{self.session_id}.jsonl"
        self._event_count = 0

    @property
    def event_count(self) -> int:
        return self._event_count

    def append(self, event: AgentEvent) -> None:
        if event.session_id != self.session_id:
            raise ValueError("event belongs to a different session")

        self.root.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(event.to_dict(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.write("\n")
        self._event_count += 1

    def load(self) -> list[AgentEvent]:
        if not self.path.exists():
            return []

        events: list[AgentEvent] = []
        with self.path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    events.append(AgentEvent.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    raise ValueError(
                        f"invalid session event at line {line_number}: {error}"
                    ) from error
        self._event_count = len(events)
        return events


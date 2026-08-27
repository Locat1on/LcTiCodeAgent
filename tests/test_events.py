from __future__ import annotations

import unittest

from code_agent.events import AgentEvent, EventType


class AgentEventTests(unittest.TestCase):
    def test_event_round_trip_preserves_protocol_fields(self) -> None:
        event = AgentEvent.create(
            EventType.TOOL_COMPLETED,
            "session-1",
            {"name": "search_text", "duration_ms": 8},
            turn_id="turn-1",
            step_id="step-1",
        )

        restored = AgentEvent.from_dict(event.to_dict())

        self.assertEqual(restored, event)
        self.assertEqual(restored.event_type, EventType.TOOL_COMPLETED)

    def test_empty_session_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "session_id"):
            AgentEvent.create(EventType.ERROR, "  ")


if __name__ == "__main__":
    unittest.main()


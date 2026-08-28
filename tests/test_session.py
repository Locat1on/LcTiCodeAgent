from __future__ import annotations

import unittest

from code_agent.events import AgentEvent, EventType
from code_agent.session import SessionLog
from tests.helpers import test_directory


class SessionLogTests(unittest.TestCase):
    def test_append_and_load_jsonl_events(self) -> None:
        with test_directory() as directory:
            log = SessionLog(directory, session_id="session-1")
            first = AgentEvent.create(
                EventType.USER_MESSAGE,
                log.session_id,
                {"text": "你好"},
                turn_id="turn-1",
            )
            second = AgentEvent.create(
                EventType.TURN_COMPLETED,
                log.session_id,
                {"reason": "assistant_response"},
                turn_id="turn-1",
            )

            log.append(first)
            log.append(second)

            self.assertEqual(log.load(), [first, second])
            self.assertEqual(log.event_count, 2)
            self.assertEqual(len(log.path.read_text(encoding="utf-8").splitlines()), 2)

    def test_event_from_another_session_is_rejected(self) -> None:
        with test_directory() as directory:
            log = SessionLog(directory, session_id="session-1")
            event = AgentEvent.create(EventType.ERROR, "session-2")

            with self.assertRaisesRegex(ValueError, "different session"):
                log.append(event)

    def test_recall_returns_matching_event(self) -> None:
        with test_directory() as directory:
            log = SessionLog(directory, session_id="session-1")
            first = AgentEvent.create(EventType.USER_MESSAGE, log.session_id, {"text": "a"})
            second = AgentEvent.create(
                EventType.TOOL_COMPLETED,
                log.session_id,
                {"name": "read_file", "content": "1: line"},
            )
            log.append(first)
            log.append(second)

            self.assertEqual(log.recall(second.event_id), second)

    def test_recall_returns_none_for_unknown_id(self) -> None:
        with test_directory() as directory:
            log = SessionLog(directory, session_id="session-1")
            log.append(AgentEvent.create(EventType.USER_MESSAGE, log.session_id))

            self.assertIsNone(log.recall("missing-event"))
            empty = SessionLog(directory, session_id="session-empty")
            self.assertIsNone(empty.recall("missing-event"))

if __name__ == "__main__":
    unittest.main()

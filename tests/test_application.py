from __future__ import annotations

import io
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from rich.console import Console

from code_agent.cli import Application
from code_agent.events import AgentEvent, EventType
from code_agent.restore import RestoreReport
from code_agent.ui import TerminalUI
from tests.helpers import test_directory


class _ResumableAgent:
    mode = "scripted"
    model = "simulator"
    sandbox = "simulation"
    context_limit = 32_000
    used_tokens = 0

    def __init__(self) -> None:
        self.restored_events: list[AgentEvent] | None = None

    def respond(
        self,
        user_text: str,
        session_id: str,
        turn_id: str,
    ) -> Iterator[AgentEvent]:
        yield AgentEvent.create(
            EventType.ASSISTANT_MESSAGE,
            session_id,
            {"text": "完成", "finish_reason": "stop", "tool_calls": None},
            turn_id=turn_id,
        )
        yield AgentEvent.create(
            EventType.TURN_COMPLETED,
            session_id,
            {"reason": "stop"},
            turn_id=turn_id,
        )

    def restore(self, events: list[AgentEvent]) -> RestoreReport:
        self.restored_events = list(events)
        return RestoreReport(
            events_replayed=len(events),
            context_items=3,
            estimated_tokens=120,
            used_tokens=90,
            interrupted_tool_calls=0,
        )

    def clear_context(self) -> None:
        self.used_tokens = 0

    def compact_context(self, session_id: str) -> Iterator[AgentEvent]:
        return iter(())

    def context_stats(self) -> dict[str, Any]:
        return {
            "used_tokens": self.used_tokens,
            "limit_tokens": self.context_limit,
            "items": 3,
            "layers": {},
        }


class ApplicationTests(unittest.TestCase):
    def test_demo_turn_is_rendered_and_persisted(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=100)

        with test_directory() as directory:
            app = Application(Path.cwd(), directory, TerminalUI(console))
            app.start()
            app.run_turn("检查注册功能")
            events = app.log.load()

        rendered = output.getvalue()
        self.assertIn("LcTiCodeAgent", rendered)
        self.assertIn("list_files", rendered)
        self.assertIn("context", rendered)
        self.assertEqual(events[0].event_type, EventType.SESSION_STARTED)
        self.assertEqual(events[-1].event_type, EventType.TURN_COMPLETED)

    def test_compact_context_publishes_event_and_renders(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=100)

        with test_directory() as directory:
            app = Application(Path.cwd(), directory, TerminalUI(console))
            app.start()
            app.compact_context()
            events = app.log.load()

        rendered = output.getvalue()
        self.assertIn("nothing pruned", rendered)
        self.assertEqual(
            events[-1].event_type,
            EventType.CONTEXT_COMPACTION_COMPLETED,
        )
        self.assertFalse(events[-1].payload["changed"])

    def test_show_context_renders_layered_report(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=100)

        with test_directory() as directory:
            app = Application(Path.cwd(), directory, TerminalUI(console))
            app.start()
            app.run_turn("检查注册功能")
            app.show_context()

        rendered = output.getvalue()
        self.assertIn("Context", rendered)
        self.assertIn("Model usage", rendered)
        self.assertIn("Pinned", rendered)
        self.assertIn("Recent", rendered)
        self.assertIn("Evidence", rendered)

    def test_recall_event_displays_original_log_event(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=100)
        with test_directory() as directory:
            app = Application(Path.cwd(), directory, TerminalUI(console))
            app.start()
            event = AgentEvent.create(
                EventType.TOOL_COMPLETED,
                app.log.session_id,
                {"name": "read_file", "content": "original evidence"},
            )
            app.log.append(event)

            recalled = app.recall_event(event.event_id)

        self.assertEqual(recalled, event)
        self.assertIn("original evidence", output.getvalue())

    def test_resume_replays_log_and_continues_same_session(self) -> None:
        first_output = io.StringIO()
        resume_output = io.StringIO()

        with test_directory() as directory:
            first = Application(
                Path.cwd(),
                directory,
                TerminalUI(Console(file=first_output, force_terminal=False, width=100)),
            )
            first.start()
            first.run_turn("第一轮")
            original_count = first.log.event_count

            agent = _ResumableAgent()
            resumed = Application(
                Path.cwd(),
                directory,
                TerminalUI(
                    Console(file=resume_output, force_terminal=False, width=100)
                ),
                agent=agent,
                session_id=first.log.session_id,
            )
            resumed.start()
            resumed.run_turn("第二轮")
            events = resumed.log.load()

        self.assertIsNotNone(agent.restored_events)
        self.assertEqual(len(agent.restored_events), original_count)
        self.assertEqual(events[0].event_type, EventType.SESSION_STARTED)
        resumed_events = [
            event for event in events if event.event_type is EventType.SESSION_RESUMED
        ]
        self.assertEqual(len(resumed_events), 1)
        self.assertEqual(resumed_events[0].payload["events_replayed"], original_count)
        self.assertEqual(resumed_events[0].payload["context_items"], 3)
        self.assertEqual(events[-1].event_type, EventType.TURN_COMPLETED)
        self.assertEqual(
            [event.event_type for event in events],
            [event.event_type for event in agent.restored_events]
            + [
                EventType.SESSION_RESUMED,
                EventType.USER_MESSAGE,
                EventType.ASSISTANT_MESSAGE,
                EventType.TURN_COMPLETED,
            ],
        )
        self.assertIn("Resumed session", resume_output.getvalue())

    def test_resume_rejects_missing_and_invalid_sessions(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=100)

        with test_directory() as directory:
            with self.assertRaisesRegex(ValueError, "session log not found"):
                Application(
                    Path.cwd(),
                    directory,
                    TerminalUI(console),
                    agent=_ResumableAgent(),
                    session_id="missing-session",
                ).start()

            with self.assertRaisesRegex(ValueError, "invalid session id"):
                Application(
                    Path.cwd(),
                    directory,
                    TerminalUI(console),
                    agent=_ResumableAgent(),
                    session_id="../escape",
                ).start()

if __name__ == "__main__":
    unittest.main()

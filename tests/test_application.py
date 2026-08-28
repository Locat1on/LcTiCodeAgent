from __future__ import annotations

import io
import unittest
from pathlib import Path

from rich.console import Console

from code_agent.cli import Application
from code_agent.events import EventType
from code_agent.ui import TerminalUI
from tests.helpers import test_directory


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

if __name__ == "__main__":
    unittest.main()

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

if __name__ == "__main__":
    unittest.main()

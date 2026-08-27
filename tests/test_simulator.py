from __future__ import annotations

import unittest

from code_agent.events import EventType
from code_agent.simulator import SimulatedAgent


class SimulatedAgentTests(unittest.TestCase):
    def test_turn_contains_tools_context_and_completion(self) -> None:
        agent = SimulatedAgent()

        events = list(agent.respond("检查注册功能", "session-1", "turn-1"))
        event_types = [event.event_type for event in events]

        self.assertIn(EventType.TOOL_REQUESTED, event_types)
        self.assertIn(EventType.TOOL_COMPLETED, event_types)
        self.assertIn(EventType.CONTEXT_USAGE, event_types)
        self.assertEqual(event_types[-1], EventType.TURN_COMPLETED)
        self.assertTrue(all(event.session_id == "session-1" for event in events))
        self.assertTrue(all(event.turn_id == "turn-1" for event in events))

    def test_clear_context_resets_usage(self) -> None:
        agent = SimulatedAgent()
        list(agent.respond("检查项目", "session-1", "turn-1"))

        agent.clear_context()

        self.assertEqual(agent.used_tokens, 0)


if __name__ == "__main__":
    unittest.main()


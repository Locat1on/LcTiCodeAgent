from __future__ import annotations

import unittest
from types import SimpleNamespace

from code_agent.events import EventType
from code_agent.live_agent import LiveAgent
from code_agent.model import (
    ModelEvent,
    ModelEventType,
    ModelToolCall,
    ModelUsage,
)
from code_agent.tools import ToolRegistry
from tests.helpers import test_directory


class _FakeProvider:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            context_budget=32_000,
            model="google/gemini-3.7-flash",
        )
        self.requests: list[list[dict[str, object]]] = []

    def stream(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ):
        self.requests.append(messages.copy())
        if len(self.requests) == 1:
            yield ModelEvent(
                ModelEventType.TOOL_CALL,
                tool_call=ModelToolCall(
                    call_id="call-1",
                    name="list_files",
                    arguments={"path": ".", "depth": 1},
                    raw_arguments='{"path":".","depth":1}',
                ),
            )
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="tool_calls")
            return
        yield ModelEvent(ModelEventType.TEXT_DELTA, text="Repository inspected.")
        yield ModelEvent(
            ModelEventType.USAGE,
            usage=ModelUsage(200, 20, 220),
        )
        yield ModelEvent(ModelEventType.COMPLETED, finish_reason="stop")


class LiveAgentTests(unittest.TestCase):
    def test_tool_result_is_returned_to_model_before_final_text(self) -> None:
        with test_directory() as workspace:
            (workspace / "README.md").write_text("demo", encoding="utf-8")
            provider = _FakeProvider()
            agent = LiveAgent(provider, ToolRegistry(workspace))

            events = list(agent.respond("Inspect it", "session-1", "turn-1"))

        event_types = [event.event_type for event in events]
        second_request = provider.requests[1]
        self.assertIn(EventType.TOOL_COMPLETED, event_types)
        self.assertIn(EventType.ASSISTANT_DELTA, event_types)
        self.assertEqual(event_types[-1], EventType.TURN_COMPLETED)
        self.assertTrue(any(message["role"] == "tool" for message in second_request))
        self.assertEqual(agent.used_tokens, 220)


if __name__ == "__main__":
    unittest.main()

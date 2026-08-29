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


class _CompactionProvider:
    """Two-turn script: each turn reads a large file, then answers.

    Turn 1's tool result becomes evidence once turn 2 arrives, so the
    threshold check at turn 2's second step prunes it while leaving the
    current turn's fresh read untouched.
    """

    def __init__(self, budget: int) -> None:
        self.config = SimpleNamespace(
            context_budget=budget,
            model="google/gemini-3.7-flash",
        )
        self.requests: list[list[dict[str, object]]] = []

    def stream(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ):
        self.requests.append(messages.copy())
        call_number = len(self.requests)
        if call_number in {1, 3}:
            yield ModelEvent(
                ModelEventType.TOOL_CALL,
                tool_call=ModelToolCall(
                    call_id=f"call-{call_number}",
                    name="read_file",
                    arguments={"path": "big.py"},
                    raw_arguments='{"path":"big.py"}',
                ),
            )
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="tool_calls")
            return
        yield ModelEvent(ModelEventType.TEXT_DELTA, text="Done.")
        if call_number == 2:
            yield ModelEvent(ModelEventType.USAGE, usage=ModelUsage(200, 20, 220))
        yield ModelEvent(ModelEventType.COMPLETED, finish_reason="stop")


class _StructuredSummaryProvider:
    def __init__(self, budget: int) -> None:
        self.config = SimpleNamespace(
            context_budget=budget,
            model="google/gemini-3.7-flash",
        )
        self.summary_requests: list[list[dict[str, object]]] = []

    def stream(self, messages, tools):
        yield ModelEvent(ModelEventType.TEXT_DELTA, text="Done.")
        yield ModelEvent(ModelEventType.COMPLETED, finish_reason="stop")

    def summarize_context(self, messages):
        self.summary_requests.append(list(messages))
        return {
            "version": 1,
            "objective": "Keep the previous task context",
            "completed": [],
            "decisions": [],
            "files": [],
            "identifiers": [],
            "commands": [],
            "exit_codes": [],
            "open_errors": [],
            "next_actions": [],
            "event_ids": [],
        }


def _write_big_file(workspace) -> None:
    lines = "\n".join(
        f"def compute_value_for_item(index={index}):" for index in range(250)
    )
    (workspace / "big.py").write_text(lines + "\n", encoding="utf-8")


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

    def test_threshold_compaction_prunes_old_evidence_and_keeps_pairing(self) -> None:
        with test_directory() as workspace:
            _write_big_file(workspace)
            provider = _CompactionProvider(budget=6_666)
            agent = LiveAgent(provider, ToolRegistry(workspace))

            turn_one = list(agent.respond("Read the file", "session-1", "turn-1"))
            turn_two = list(agent.respond("Read it again", "session-1", "turn-2"))

        turn_one_types = [event.event_type for event in turn_one]
        self.assertNotIn(EventType.CONTEXT_COMPACTION_STARTED, turn_one_types)
        self.assertNotIn(EventType.CONTEXT_COMPACTION_COMPLETED, turn_one_types)

        compaction_events = [
            event
            for event in turn_two
            if event.event_type
            in {
                EventType.CONTEXT_COMPACTION_STARTED,
                EventType.CONTEXT_COMPACTION_COMPLETED,
            }
        ]
        self.assertEqual(
            [event.event_type for event in compaction_events],
            [
                EventType.CONTEXT_COMPACTION_STARTED,
                EventType.CONTEXT_COMPACTION_COMPLETED,
            ],
        )
        completed = compaction_events[1]
        self.assertEqual(completed.payload["trigger"], "threshold")
        self.assertTrue(completed.payload["changed"])
        self.assertEqual(completed.payload["items_pruned"], 1)
        self.assertEqual(completed.payload["rules"], {"read_file": 1})
        self.assertEqual(len(completed.payload["pruned_event_ids"]), 1)

        final_request = provider.requests[3]
        tool_messages = {
            message["tool_call_id"]: message
            for message in final_request
            if message["role"] == "tool"
        }
        self.assertIn("[pruned read_file", tool_messages["call-1"]["content"])
        self.assertNotIn("[pruned", tool_messages["call-3"]["content"])

        assistant_call_ids = {
            call["id"]
            for message in final_request
            if message["role"] == "assistant" and message.get("tool_calls")
            for call in message["tool_calls"]
        }
        self.assertEqual(assistant_call_ids, set(tool_messages))
        self.assertEqual(agent.used_tokens, 220)

    def test_manual_compact_context_reports_without_threshold(self) -> None:
        with test_directory() as workspace:
            _write_big_file(workspace)
            provider = _CompactionProvider(budget=32_000)
            agent = LiveAgent(provider, ToolRegistry(workspace))
            list(agent.respond("Read the file", "session-1", "turn-1"))
            list(agent.respond("Read it again", "session-1", "turn-2"))

            events = list(agent.compact_context("session-1"))

        self.assertEqual(
            [event.event_type for event in events],
            [
                EventType.CONTEXT_COMPACTION_STARTED,
                EventType.CONTEXT_COMPACTION_COMPLETED,
            ],
        )
        self.assertEqual(events[0].payload["trigger"], "manual")
        completed = events[1]
        self.assertEqual(completed.payload["trigger"], "manual")
        self.assertTrue(completed.payload["changed"])
        self.assertEqual(completed.payload["rules"], {"read_file": 1})

    def test_structured_summary_triggers_at_75_percent_and_targets_50(self) -> None:
        with test_directory() as workspace:
            provider = _StructuredSummaryProvider(budget=4_096)
            agent = LiveAgent(provider, ToolRegistry(workspace))
            old_context = "retain task context " * 700
            list(agent.respond(old_context, "session-1", "turn-1"))

            events = list(agent.respond("continue", "session-1", "turn-2"))

        completed = [
            event
            for event in events
            if event.event_type is EventType.CONTEXT_COMPACTION_COMPLETED
        ]
        structured = [
            event
            for event in completed
            if event.payload.get("strategy") == "validated_structured_summary"
        ]
        self.assertEqual(len(structured), 1)
        self.assertEqual(structured[0].payload["validation"], "passed")
        self.assertTrue(structured[0].payload["target_met"])
        self.assertLessEqual(agent._context.estimated_tokens, 2_048)
        self.assertEqual(len(provider.summary_requests), 1)

    def test_structured_summary_rejects_ungrounded_fact_without_rewriting(self) -> None:
        with test_directory() as workspace:
            provider = _StructuredSummaryProvider(budget=4_096)
            agent = LiveAgent(provider, ToolRegistry(workspace))
            list(agent.respond("old context " * 1_000, "session-1", "turn-1"))
            provider.summarize_context = lambda messages: {
                "version": 1,
                "objective": "Modify invented.py",
                "completed": [],
                "decisions": [],
                "files": [],
                "identifiers": [],
                "commands": [],
                "exit_codes": [],
                "open_errors": [],
                "next_actions": [],
                "event_ids": [],
            }

            events = list(agent.respond("continue", "session-1", "turn-2"))

        structured = [
            event
            for event in events
            if event.event_type is EventType.CONTEXT_COMPACTION_COMPLETED
            and event.payload.get("strategy") == "validated_structured_summary"
        ]
        self.assertEqual(structured[0].payload["validation"], "rejected")
        self.assertFalse(structured[0].payload["changed"])


if __name__ == "__main__":
    unittest.main()

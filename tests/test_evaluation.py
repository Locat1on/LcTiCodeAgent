from __future__ import annotations

import unittest

from code_agent.evaluation import collect_context_metrics
from code_agent.events import AgentEvent, EventType


class ContextMetricsTests(unittest.TestCase):
    def test_collects_comparable_metrics_from_events(self) -> None:
        session_id = "metrics-session"
        events = [
            AgentEvent.create(
                EventType.CONTEXT_USAGE,
                session_id,
                {"prompt_tokens": 100, "completion_tokens": 20},
            ),
            AgentEvent.create(
                EventType.TOOL_REQUESTED,
                session_id,
                {"name": "read_file", "arguments": {"path": "app.py"}},
            ),
            AgentEvent.create(
                EventType.TOOL_REQUESTED,
                session_id,
                {"name": "read_file", "arguments": {"path": "app.py"}},
            ),
            AgentEvent.create(
                EventType.CONTEXT_COMPACTION_COMPLETED,
                session_id,
                {
                    "changed": True,
                    "before_tokens": 1_000,
                    "after_tokens": 400,
                    "validation": "passed",
                    "pruned_event_ids": ["event-1", "event-2"],
                },
            ),
            AgentEvent.create(
                EventType.TURN_COMPLETED,
                session_id,
                {"reason": "stop"},
            ),
        ]

        metrics = collect_context_metrics(events, strategy="validated")

        self.assertEqual(metrics.compression_ratio, 0.4)
        self.assertEqual(metrics.total_tokens_removed, 600)
        self.assertEqual(metrics.prompt_tokens, 100)
        self.assertEqual(metrics.completion_tokens, 20)
        self.assertEqual(metrics.tool_calls, 2)
        self.assertEqual(metrics.repeated_reads, 1)
        self.assertEqual(metrics.recoverable_events, 2)
        self.assertEqual(metrics.validation_passed, 1)
        self.assertEqual(metrics.final_turn_reason, "stop")


if __name__ == "__main__":
    unittest.main()

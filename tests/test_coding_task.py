from __future__ import annotations

import hashlib
import json
import re
import unittest
from types import SimpleNamespace

from code_agent.events import EventType
from code_agent.live_agent import LiveAgent
from code_agent.model import ModelEvent, ModelEventType, ModelToolCall
from code_agent.tools import ToolRegistry
from tests.helpers import test_directory


class _ScriptedCodingProvider:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            context_budget=32_000,
            model="scripted-test-model",
        )
        self.step = 0

    def stream(self, messages, tools):
        read_digest = self._sha256_from_read(messages, "read-1")
        calls = [
            self._call("read-1", "read_file", {"path": "calculator.py"}),
            self._call("read-2", "read_file", {"path": "tests/test_calculator.py"}),
            self._call(
                "test-1",
                "run_command",
                {
                    "argv": [
                        "python",
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                        "-v",
                    ]
                },
            ),
            self._call(
                "edit-1",
                "replace_in_file",
                {
                    "path": "calculator.py",
                    "old_text": "    return sum(values) / len(values)",
                    "new_text": (
                        "    if not values:\n"
                        "        return 0.0\n"
                        "    return sum(values) / len(values)"
                    ),
                    "expected_sha256": read_digest,
                },
            ),
            self._call(
                "test-2",
                "run_command",
                {
                    "argv": [
                        "python",
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                        "-v",
                    ]
                },
            ),
        ]
        if self.step < len(calls):
            yield ModelEvent(ModelEventType.TOOL_CALL, tool_call=calls[self.step])
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="tool_calls")
        else:
            yield ModelEvent(
                ModelEventType.TEXT_DELTA,
                text="Fixed calculator.py and verified both tests pass.",
            )
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="stop")
        self.step += 1

    @staticmethod
    def _call(call_id: str, name: str, arguments: dict) -> ModelToolCall:
        raw_arguments = json.dumps(arguments)
        return ModelToolCall(call_id, name, arguments, raw_arguments)

    @staticmethod
    def _sha256_from_read(messages, tool_call_id: str) -> str:
        for message in messages:
            if message.get("role") != "tool":
                continue
            if message.get("tool_call_id") != tool_call_id:
                continue
            try:
                result = json.loads(message["content"])["result"]
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            match = re.search(r"^sha256: ([0-9a-f]{64})$", result, re.MULTILINE)
            if match:
                return match.group(1)
        return ""


class CodingTaskTests(unittest.TestCase):
    def test_agent_reads_edits_and_verifies_without_modifying_tests(self) -> None:
        with test_directory() as workspace:
            tests = workspace / "tests"
            tests.mkdir()
            source = workspace / "calculator.py"
            test_file = tests / "test_calculator.py"
            source.write_text(
                "def average(values):\n"
                "    return sum(values) / len(values)\n",
                encoding="utf-8",
            )
            test_file.write_text(
                "import unittest\n"
                "from calculator import average\n\n"
                "class AverageTests(unittest.TestCase):\n"
                "    def test_values(self):\n"
                "        self.assertEqual(average([2, 4]), 3)\n\n"
                "    def test_empty(self):\n"
                "        self.assertEqual(average([]), 0.0)\n",
                encoding="utf-8",
            )
            original_test_hash = self._hash(test_file)
            agent = LiveAgent(
                _ScriptedCodingProvider(),
                ToolRegistry(workspace),
            )

            events = list(agent.respond("Fix average", "session-1", "turn-1"))
            updated_source = source.read_text(encoding="utf-8")
            final_test_hash = self._hash(test_file)

        run_events = [
            event
            for event in events
            if event.payload.get("name") == "run_command"
            and event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_FAILED}
        ]
        self.assertIn("if not values", updated_source)
        self.assertEqual(original_test_hash, final_test_hash)
        self.assertEqual(
            [event.event_type for event in run_events],
            [EventType.TOOL_FAILED, EventType.TOOL_COMPLETED],
        )
        self.assertEqual(events[-1].event_type, EventType.TURN_COMPLETED)

    @staticmethod
    def _hash(path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()

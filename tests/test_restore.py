from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from code_agent.events import AgentEvent, EventType
from code_agent.live_agent import LiveAgent
from code_agent.model import ModelEvent, ModelEventType, ModelToolCall, ModelUsage
from code_agent.restore import (
    INTERRUPTED_TOOL_RESULT,
    RestoreError,
    project_session,
)
from code_agent.session import SessionLog
from code_agent.tools import ToolRegistry
from tests.helpers import test_directory


class _TextProvider:
    """Streams one final text response per call; usable for any turn."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(
            context_budget=32_000,
            model="scripted-test-model",
        )

    def stream(self, messages, tools):
        yield ModelEvent(ModelEventType.TEXT_DELTA, text="好的。")
        yield ModelEvent(ModelEventType.COMPLETED, finish_reason="stop")
        yield ModelEvent(
            ModelEventType.USAGE,
            usage=ModelUsage(200, 10, 210),
        )


class _TwoTurnProvider:
    """Tool-heavy first turn (read/edit/run) then a text-only second turn."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(
            context_budget=32_000,
            model="scripted-test-model",
        )
        self.step = 0

    def stream(self, messages, tools):
        if self.step == 0:
            yield ModelEvent(
                ModelEventType.TOOL_CALL,
                tool_call=self._call("read-1", "read_file", {"path": "app.py"}),
            )
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="tool_calls")
        elif self.step == 1:
            digest = self._sha256_from_read(messages, "read-1")
            yield ModelEvent(
                ModelEventType.TOOL_CALL,
                tool_call=self._call(
                    "edit-1",
                    "replace_in_file",
                    {
                        "path": "app.py",
                        "old_text": "    return sum(values) / len(values)",
                        "new_text": (
                            "    if not values:\n"
                            "        return 0.0\n"
                            "    return sum(values) / len(values)"
                        ),
                        "expected_sha256": digest,
                    },
                ),
            )
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="tool_calls")
        elif self.step == 2:
            yield ModelEvent(
                ModelEventType.TOOL_CALL,
                tool_call=self._call(
                    "run-1",
                    "run_command",
                    {"argv": ["python", "-m", "unittest", "discover", "-v"]},
                ),
            )
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="tool_calls")
        elif self.step == 3:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="已修复除零问题。")
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="stop")
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="第二回合完成。")
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="stop")
        yield ModelEvent(
            ModelEventType.USAGE,
            usage=ModelUsage(400, 40, 440 + 260 * self.step),
        )
        self.step += 1

    @staticmethod
    def _call(call_id: str, name: str, arguments: dict) -> ModelToolCall:
        return ModelToolCall(call_id, name, arguments, json.dumps(arguments))

    @staticmethod
    def _sha256_from_read(messages, tool_call_id: str) -> str:
        import re

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


class _FetchAgainProvider:
    """Re-requests an ASK tool after restore, then answers with text."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(
            context_budget=32_000,
            model="scripted-test-model",
        )
        self.step = 0

    def stream(self, messages, tools):
        if self.step == 0:
            yield ModelEvent(
                ModelEventType.TOOL_CALL,
                tool_call=ModelToolCall(
                    "call-2",
                    "fetch_url",
                    {"url": "https://example.com"},
                    '{"url": "https://example.com"}',
                ),
            )
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="tool_calls")
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="被拒绝。")
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="stop")
        self.step += 1


class _CountingApproval:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request) -> bool:
        self.calls += 1
        return False


def _run_session(agent: LiveAgent, log: SessionLog, prompts: list[str]) -> None:
    log.append(
        AgentEvent.create(
            EventType.SESSION_STARTED,
            log.session_id,
            {"workspace": "unused", "mode": "live", "model": "scripted-test-model"},
        )
    )
    for prompt in prompts:
        turn_id = f"turn-{prompt}"
        log.append(
            AgentEvent.create(
                EventType.USER_MESSAGE,
                log.session_id,
                {"text": prompt},
                turn_id=turn_id,
            )
        )
        for event in agent.respond(prompt, log.session_id, turn_id):
            log.append(event)


class RestoreTests(unittest.TestCase):
    def test_restore_rebuilds_identical_context_from_recorded_log(self) -> None:
        with test_directory() as workspace:
            source = workspace / "app.py"
            source.write_text(
                "def average(values):\n"
                '    """计算平均值。"""\n'
                "    return sum(values) / len(values)\n",
                encoding="utf-8",
            )
            log = SessionLog(workspace / "sessions")
            original = LiveAgent(_TwoTurnProvider(), ToolRegistry(workspace))
            _run_session(original, log, ["修复 average", "继续"])

            restored = LiveAgent(_TextProvider(), ToolRegistry(workspace))
            report = restored.restore(log.load())

        self.assertEqual(
            original._context.messages(),
            restored._context.messages(),
        )
        original_memory = original._context.working_memory
        restored_memory = restored._context.working_memory
        self.assertEqual(
            original_memory.modified_files,
            restored_memory.modified_files,
        )
        self.assertEqual(
            original_memory.verified_commands,
            restored_memory.verified_commands,
        )
        self.assertEqual(original_memory.open_errors, restored_memory.open_errors)
        self.assertEqual(original.used_tokens, restored.used_tokens)
        self.assertEqual(report.interrupted_tool_calls, 0)
        self.assertEqual(report.events_replayed, log.event_count)

    def test_restored_context_supports_a_follow_up_turn(self) -> None:
        with test_directory() as workspace:
            log = SessionLog(workspace / "sessions")
            _run_session(
                LiveAgent(_TextProvider(), ToolRegistry(workspace)),
                log,
                ["第一轮"],
            )
            restored = LiveAgent(_TextProvider(), ToolRegistry(workspace))
            restored.restore(log.load())
            turn_events = list(restored.respond("继续", log.session_id, "turn-2"))

        self.assertEqual(turn_events[-1].event_type, EventType.TURN_COMPLETED)

    def test_restore_synthesizes_missing_tool_result(self) -> None:
        session_id = "interrupted-session"
        events = [
            AgentEvent.create(
                EventType.SESSION_STARTED,
                session_id,
                {"workspace": "unused"},
            ),
            AgentEvent.create(EventType.USER_MESSAGE, session_id, {"text": "读取"}),
            AgentEvent.create(
                EventType.ASSISTANT_MESSAGE,
                session_id,
                {
                    "text": "",
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "app.py"}',
                            },
                        }
                    ],
                },
            ),
            AgentEvent.create(
                EventType.TOOL_REQUESTED,
                session_id,
                {
                    "call_id": "call-1",
                    "name": "read_file",
                    "arguments": {"path": "app.py"},
                },
            ),
        ]

        with test_directory() as workspace:
            agent = LiveAgent(_TextProvider(), ToolRegistry(workspace))
            report = agent.restore(events)
        messages = agent._context.messages()

        self.assertEqual(report.interrupted_tool_calls, 1)
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual(messages[-1]["tool_call_id"], "call-1")
        envelope = json.loads(messages[-1]["content"])
        self.assertFalse(envelope["ok"])
        self.assertIn("re-run", envelope["result"])

    def test_restore_synthesizes_call_without_requested_event(self) -> None:
        session_id = "crash-before-requested"
        events = [
            AgentEvent.create(
                EventType.SESSION_STARTED,
                session_id,
                {"workspace": "unused"},
            ),
            AgentEvent.create(EventType.USER_MESSAGE, session_id, {"text": "读取"}),
            AgentEvent.create(
                EventType.ASSISTANT_MESSAGE,
                session_id,
                {
                    "text": "",
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "app.py", "start_line": 5}',
                            },
                        }
                    ],
                },
            ),
        ]

        with test_directory() as workspace:
            agent = LiveAgent(_TextProvider(), ToolRegistry(workspace))
            report = agent.restore(events)

        tool_items = [
            item for item in agent._context._items if item.role == "tool"
        ]
        self.assertEqual(report.interrupted_tool_calls, 1)
        self.assertEqual(len(tool_items), 1)
        self.assertEqual(tool_items[0].tool_name, "read_file")
        self.assertEqual(tool_items[0].arguments, {"path": "app.py", "start_line": 5})
        self.assertEqual(
            json.loads(tool_items[0].content)["result"],
            INTERRUPTED_TOOL_RESULT,
        )

    def test_restore_does_not_resurrect_approvals(self) -> None:
        session_id = "approval-session"
        events = [
            AgentEvent.create(
                EventType.SESSION_STARTED,
                session_id,
                {"workspace": "unused"},
            ),
            AgentEvent.create(EventType.USER_MESSAGE, session_id, {"text": "抓取"}),
            AgentEvent.create(
                EventType.ASSISTANT_MESSAGE,
                session_id,
                {
                    "text": "",
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "fetch_url",
                                "arguments": '{"url": "https://example.com"}',
                            },
                        }
                    ],
                },
            ),
            AgentEvent.create(
                EventType.TOOL_REQUESTED,
                session_id,
                {
                    "call_id": "call-1",
                    "name": "fetch_url",
                    "arguments": {"url": "https://example.com"},
                },
            ),
            AgentEvent.create(
                EventType.TOOL_APPROVAL_REQUIRED,
                session_id,
                {
                    "call_id": "call-1",
                    "name": "fetch_url",
                    "request_id": "request-1",
                    "reason": "outbound HTTPS request leaves the local workspace",
                    "arguments": {"url": "https://example.com"},
                    "context": {"operation": "fetch_url"},
                },
            ),
            AgentEvent.create(
                EventType.TOOL_APPROVAL_DECIDED,
                session_id,
                {"call_id": "call-1", "request_id": "request-1", "approved": True},
            ),
        ]

        with test_directory() as workspace:
            approval = _CountingApproval()
            agent = LiveAgent(
                _FetchAgainProvider(),
                ToolRegistry(workspace),
                approval_handler=approval,
            )
            report = agent.restore(events)
            turn_events = list(
                agent.respond("再抓一次", session_id, "turn-2")
            )

        self.assertEqual(report.interrupted_tool_calls, 1)
        self.assertEqual(approval.calls, 1)
        failed = [
            event
            for event in turn_events
            if event.event_type is EventType.TOOL_FAILED
        ]
        self.assertEqual(len(failed), 1)
        self.assertIn("permission denied by user", failed[0].payload["error"])

    def test_restore_rejects_invalid_logs(self) -> None:
        with test_directory() as workspace:
            agent = LiveAgent(_TextProvider(), ToolRegistry(workspace))
            with self.assertRaises(RestoreError) as empty:
                agent.restore([])
            self.assertIn("empty", str(empty.exception))

            user_first = [
                AgentEvent.create(
                    EventType.USER_MESSAGE,
                    "session-a",
                    {"text": "hi"},
                )
            ]
            with self.assertRaises(RestoreError) as wrong_start:
                agent.restore(user_first)
            self.assertIn("session.started", str(wrong_start.exception))

            mismatched = [
                AgentEvent.create(
                    EventType.SESSION_STARTED,
                    "session-a",
                    {"workspace": "unused"},
                ),
                AgentEvent.create(
                    EventType.USER_MESSAGE,
                    "session-b",
                    {"text": "hi"},
                ),
            ]
            with self.assertRaises(RestoreError) as wrong_session:
                agent.restore(mismatched)
            self.assertIn("different session", str(wrong_session.exception))

    def test_restore_rejects_tool_result_without_declared_call(self) -> None:
        session_id = "legacy-session"
        events = [
            AgentEvent.create(
                EventType.SESSION_STARTED,
                session_id,
                {"workspace": "unused"},
            ),
            AgentEvent.create(EventType.USER_MESSAGE, session_id, {"text": "读取"}),
            AgentEvent.create(
                EventType.ASSISTANT_MESSAGE,
                session_id,
                {"text": "", "finish_reason": "tool_calls"},
            ),
            AgentEvent.create(
                EventType.TOOL_REQUESTED,
                session_id,
                {
                    "call_id": "call-1",
                    "name": "read_file",
                    "arguments": {"path": "app.py"},
                },
            ),
            AgentEvent.create(
                EventType.TOOL_COMPLETED,
                session_id,
                {
                    "call_id": "call-1",
                    "name": "read_file",
                    "content": "sha256: 0\n1: line",
                },
            ),
        ]

        with test_directory() as workspace:
            agent = LiveAgent(_TextProvider(), ToolRegistry(workspace))
            with self.assertRaises(RestoreError) as raised:
                agent.restore(events)
        self.assertIn("predates the resumable format", str(raised.exception))

    def test_restore_rejects_malformed_payload(self) -> None:
        session_id = "malformed-session"
        events = [
            AgentEvent.create(
                EventType.SESSION_STARTED,
                session_id,
                {"workspace": "unused"},
            ),
            AgentEvent.create(
                EventType.ASSISTANT_MESSAGE,
                session_id,
                {
                    "text": "",
                    "finish_reason": "tool_calls",
                    "tool_calls": [{"id": "call-1"}],
                },
            ),
        ]

        with test_directory() as workspace:
            agent = LiveAgent(_TextProvider(), ToolRegistry(workspace))
            with self.assertRaises(RestoreError) as raised:
                agent.restore(events)
        self.assertIn("malformed assistant.message", str(raised.exception))

    def test_restore_after_context_cleared_drops_old_messages(self) -> None:
        session_id = "cleared-session"
        events = [
            AgentEvent.create(
                EventType.SESSION_STARTED,
                session_id,
                {"workspace": "unused"},
            ),
            AgentEvent.create(EventType.USER_MESSAGE, session_id, {"text": "第一轮"}),
            AgentEvent.create(
                EventType.ASSISTANT_MESSAGE,
                session_id,
                {
                    "text": "",
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": '{"argv": ["python"]}',
                            },
                        }
                    ],
                },
            ),
            AgentEvent.create(
                EventType.TOOL_REQUESTED,
                session_id,
                {
                    "call_id": "call-1",
                    "name": "run_command",
                    "arguments": {"argv": ["python"]},
                },
            ),
            AgentEvent.create(
                EventType.TOOL_COMPLETED,
                session_id,
                {
                    "call_id": "call-1",
                    "name": "run_command",
                    "content": '{"argv": ["python"], "exit_code": 0}',
                },
            ),
            AgentEvent.create(
                EventType.CONTEXT_USAGE,
                session_id,
                {"used_tokens": 900, "limit_tokens": 32_000},
            ),
            AgentEvent.create(EventType.CONTEXT_CLEARED, session_id, {}),
            AgentEvent.create(EventType.USER_MESSAGE, session_id, {"text": "第二轮"}),
            AgentEvent.create(
                EventType.ASSISTANT_MESSAGE,
                session_id,
                {"text": "完成", "finish_reason": "stop", "tool_calls": None},
            ),
        ]

        with test_directory() as workspace:
            agent = LiveAgent(_TextProvider(), ToolRegistry(workspace))
            report = agent.restore(events)
        messages = agent._context.messages()

        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "assistant"],
        )
        self.assertEqual(messages[1]["content"], "第二轮")
        self.assertEqual(report.used_tokens, 0)
        self.assertEqual(agent._context.working_memory.verified_commands, [])

    def test_restore_twice_over_log_with_resumed_event(self) -> None:
        with test_directory() as workspace:
            log = SessionLog(workspace / "sessions")
            _run_session(
                LiveAgent(_TextProvider(), ToolRegistry(workspace)),
                log,
                ["第一轮"],
            )
            log.append(
                AgentEvent.create(
                    EventType.SESSION_RESUMED,
                    log.session_id,
                    {"events_replayed": log.event_count},
                )
            )
            first = LiveAgent(_TextProvider(), ToolRegistry(workspace))
            first.restore(log.load())
            log.append(
                AgentEvent.create(
                    EventType.SESSION_RESUMED,
                    log.session_id,
                    {"events_replayed": log.event_count},
                )
            )
            second = LiveAgent(_TextProvider(), ToolRegistry(workspace))
            report = second.restore(log.load())

        self.assertEqual(report.interrupted_tool_calls, 0)
        self.assertEqual(
            first._context.messages(),
            second._context.messages(),
        )


if __name__ == "__main__":
    unittest.main()

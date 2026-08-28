from __future__ import annotations

import unittest
import subprocess
from types import SimpleNamespace

from code_agent.events import EventType
from code_agent.live_agent import LiveAgent
from code_agent.model import ModelEvent, ModelEventType, ModelToolCall
from code_agent.security import PermissionDecision, PermissionPolicy, RiskClass
from code_agent.tools import ToolRegistry
from tests.helpers import test_directory


class _CountingFetcher:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, url, **kwargs):
        self.calls += 1
        return {"url": url, "status": 200, "body": "ok", "bytes": 2}


class _FetchProvider:
    def __init__(self) -> None:
        self.config = SimpleNamespace(context_budget=32_000, model="test-model")
        self.step = 0

    def stream(self, messages, tools):
        if self.step == 0:
            call = ModelToolCall(
                "fetch-1",
                "fetch_url",
                {"url": "https://example.com"},
                '{"url":"https://example.com"}',
            )
            yield ModelEvent(ModelEventType.TOOL_CALL, tool_call=call)
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="tool_calls")
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="done")
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="stop")
        self.step += 1


class _GitCommitProvider:
    def __init__(self) -> None:
        self.config = SimpleNamespace(context_budget=32_000, model="test-model")
        self.step = 0

    def stream(self, messages, tools):
        if self.step == 0:
            arguments = {"files": ["example.py"], "message": "update example"}
            call = ModelToolCall(
                "commit-1",
                "git_commit",
                arguments,
                '{"files":["example.py"],"message":"update example"}',
            )
            yield ModelEvent(ModelEventType.TOOL_CALL, tool_call=call)
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="tool_calls")
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="done")
            yield ModelEvent(ModelEventType.COMPLETED, finish_reason="stop")
        self.step += 1


class SecurityPipelineTests(unittest.TestCase):
    def test_permission_policy_has_allow_ask_and_deny_paths(self) -> None:
        policy = PermissionPolicy()

        self.assertEqual(
            policy.evaluate("read_file").decision,
            PermissionDecision.ALLOW,
        )
        self.assertEqual(
            policy.evaluate("search_text").decision,
            PermissionDecision.ALLOW,
        )
        self.assertEqual(
            policy.evaluate("search_text").risk,
            RiskClass.READ,
        )
        self.assertEqual(
            policy.evaluate("fetch_url").decision,
            PermissionDecision.ASK,
        )
        unknown = policy.evaluate("dangerous_unknown_tool")
        self.assertEqual(unknown.decision, PermissionDecision.DENY)
        self.assertEqual(unknown.risk, RiskClass.UNKNOWN)

    def test_denied_approval_prevents_network_side_effect(self) -> None:
        with test_directory() as workspace:
            fetcher = _CountingFetcher()
            agent = LiveAgent(
                _FetchProvider(),
                ToolRegistry(workspace, url_fetcher=fetcher),
                approval_handler=lambda request: False,
            )

            events = list(agent.respond("Fetch docs", "session-1", "turn-1"))

        event_types = [event.event_type for event in events]
        self.assertEqual(fetcher.calls, 0)
        self.assertIn(EventType.TOOL_APPROVAL_REQUIRED, event_types)
        self.assertIn(EventType.TOOL_APPROVAL_DECIDED, event_types)
        self.assertIn(EventType.TOOL_FAILED, event_types)
        decision = next(
            event
            for event in events
            if event.event_type is EventType.TOOL_APPROVAL_DECIDED
        )
        self.assertFalse(decision.payload["approved"])

    def test_approved_network_action_executes_once(self) -> None:
        with test_directory() as workspace:
            fetcher = _CountingFetcher()
            agent = LiveAgent(
                _FetchProvider(),
                ToolRegistry(workspace, url_fetcher=fetcher),
                approval_handler=lambda request: True,
            )

            events = list(agent.respond("Fetch docs", "session-1", "turn-1"))

        self.assertEqual(fetcher.calls, 1)
        self.assertTrue(
            any(event.event_type is EventType.TOOL_COMPLETED for event in events)
        )

    def test_approval_handler_failure_denies_action(self) -> None:
        def broken_handler(request):
            raise RuntimeError("approval UI unavailable")

        with test_directory() as workspace:
            fetcher = _CountingFetcher()
            agent = LiveAgent(
                _FetchProvider(),
                ToolRegistry(workspace, url_fetcher=fetcher),
                approval_handler=broken_handler,
            )

            events = list(agent.respond("Fetch docs", "session-1", "turn-1"))

        self.assertEqual(fetcher.calls, 0)
        decision = next(
            event
            for event in events
            if event.event_type is EventType.TOOL_APPROVAL_DECIDED
        )
        self.assertFalse(decision.payload["approved"])

    def test_state_change_after_approval_blocks_commit(self) -> None:
        with test_directory() as workspace:
            self._git(workspace, "init", "-q", "-b", "main")
            self._git(workspace, "config", "user.name", "Test User")
            self._git(workspace, "config", "user.email", "test@example.com")
            source = workspace / "example.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(workspace, "add", "example.py")
            self._git(workspace, "commit", "-q", "-m", "initial")
            source.write_text("value = 2\n", encoding="utf-8")

            def mutate_during_approval(request):
                source.write_text("value = 3\n", encoding="utf-8")
                return True

            agent = LiveAgent(
                _GitCommitProvider(),
                ToolRegistry(workspace),
                approval_handler=mutate_during_approval,
            )
            events = list(agent.respond("Commit change", "session-1", "turn-1"))
            log = self._git_output(workspace, "log", "--oneline")

        failures = [
            event
            for event in events
            if event.event_type is EventType.TOOL_FAILED
        ]
        self.assertEqual(len(log.splitlines()), 1)
        self.assertTrue(
            any("state changed" in event.payload["error"] for event in failures)
        )

    @staticmethod
    def _git(workspace, *arguments) -> None:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)

    @staticmethod
    def _git_output(workspace, *arguments) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
        return completed.stdout


if __name__ == "__main__":
    unittest.main()

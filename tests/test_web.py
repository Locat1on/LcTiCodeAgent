from __future__ import annotations

import threading
import unittest
from collections.abc import Iterator
from pathlib import Path

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from code_agent.events import AgentEvent, EventType
from code_agent.security import (
    ApprovalHandler,
    ApprovalRequest,
    PermissionDecision,
    PermissionRule,
    RiskClass,
)
from code_agent.web import ApprovalBroker, create_web_app
from tests.helpers import test_directory


class _ApprovalAgent:
    mode = "approval-test"
    model = "scripted"
    sandbox = "workspace-policy"
    context_limit = 32_000
    used_tokens = 0

    def __init__(self, approval_handler: ApprovalHandler) -> None:
        self.approval_handler = approval_handler

    def respond(
        self,
        user_text: str,
        session_id: str,
        turn_id: str,
    ) -> Iterator[AgentEvent]:
        rule = PermissionRule(
            RiskClass.EXTERNAL_WRITE,
            PermissionDecision.ASK,
            "pushing changes an external repository",
        )
        request = ApprovalRequest.create(
            "git_push",
            rule,
            {"remote": "origin", "branch": "main"},
            {
                "remote": "origin",
                "remote_url": "https://github.com/Locat1on/LcTiCodeAgent.git",
                "branch": "main",
                "head": "0624de8",
                "commit_count": 1,
                "commits": ["0624de8 reliability"],
                "changed_files": ["code_agent/openrouter.py"],
                "secret_scan": "passed",
                "force": False,
            },
        )
        yield AgentEvent.create(
            EventType.TOOL_APPROVAL_REQUIRED,
            session_id,
            {
                "request_id": request.request_id,
                "name": request.tool_name,
                "risk": request.risk.value,
                "reason": request.reason,
                "arguments": request.arguments,
                "context": request.context,
            },
            turn_id=turn_id,
        )
        approved = self.approval_handler(request)
        yield AgentEvent.create(
            EventType.TOOL_APPROVAL_DECIDED,
            session_id,
            {
                "request_id": request.request_id,
                "name": request.tool_name,
                "approved": approved,
            },
            turn_id=turn_id,
        )
        yield AgentEvent.create(
            EventType.TURN_COMPLETED,
            session_id,
            {"reason": "stop"},
            turn_id=turn_id,
        )

    def restore(self, events):
        return None

    def clear_context(self) -> None:
        return None

    def compact_context(self, session_id):
        return iter(())

    def context_stats(self):
        return {
            "items": 0,
            "estimated_tokens": 0,
            "used_tokens": 0,
            "limit_tokens": self.context_limit,
            "layers": {},
            "working_memory": {},
        }


class ApprovalBrokerTests(unittest.TestCase):
    def test_prepared_request_resolves_once(self) -> None:
        broker = ApprovalBroker(timeout_seconds=1)
        request = ApprovalRequest.create(
            "git_push",
            PermissionRule(
                RiskClass.EXTERNAL_WRITE,
                PermissionDecision.ASK,
                "external write",
            ),
            {"remote": "origin"},
            {"head": "abc"},
        )
        broker.prepare(request.request_id)
        result: list[bool] = []
        worker = threading.Thread(target=lambda: result.append(broker.wait(request)))
        worker.start()

        self.assertTrue(broker.resolve(request.request_id, True))
        self.assertFalse(broker.resolve(request.request_id, False))
        worker.join(timeout=2)

        self.assertEqual(result, [True])

    def test_disconnect_denies_pending_request(self) -> None:
        broker = ApprovalBroker(timeout_seconds=1)
        request = ApprovalRequest.create(
            "fetch_url",
            PermissionRule(RiskClass.NETWORK, PermissionDecision.ASK, "network"),
            {"url": "https://example.com"},
            {},
        )
        broker.prepare(request.request_id)
        result: list[bool] = []
        worker = threading.Thread(target=lambda: result.append(broker.wait(request)))
        worker.start()

        broker.deny_all()
        worker.join(timeout=2)

        self.assertEqual(result, [False])


class WebApplicationTests(unittest.TestCase):
    def test_serves_cartoon_ui_and_static_assets(self) -> None:
        with test_directory() as directory:
            app = create_web_app(directory, directory / "sessions")
            with TestClient(app) as client:
                page = client.get("/")
                css = client.get("/static/app.css")
                script = client.get("/static/app.js")
                mascot = client.get("/static/mascot.png")

        self.assertEqual(page.status_code, 200)
        self.assertIn("LcTiCodeAgent", page.text)
        self.assertIn("工具执行和证据", page.text)
        self.assertIn("--paper", css.text)
        self.assertIn("new WebSocket", script.text)
        self.assertEqual(mascot.status_code, 200)
        self.assertEqual(mascot.headers["content-type"], "image/png")
        self.assertIn("default-src 'self'", page.headers["content-security-policy"])
        self.assertEqual(page.headers["x-content-type-options"], "nosniff")

    def test_websocket_streams_existing_agent_events_and_persists_log(self) -> None:
        with test_directory() as directory:
            session_root = directory / "sessions"
            app = create_web_app(directory, session_root)
            with TestClient(app) as client:
                with client.websocket_connect("/ws") as websocket:
                    ready = websocket.receive_json()
                    session_id = ready["session_id"]
                    websocket.send_json({"type": "run", "text": "检查注册功能"})
                    event_types: list[str] = []
                    while EventType.TURN_COMPLETED.value not in event_types:
                        message = websocket.receive_json()
                        if message["type"] == "event":
                            event_types.append(message["event"]["event_type"])

                sessions = client.get("/api/sessions").json()["sessions"]

            log_path = session_root / f"{session_id}.jsonl"
            lines = log_path.read_text(encoding="utf-8").splitlines()

        self.assertIn(EventType.USER_MESSAGE.value, event_types)
        self.assertIn(EventType.TOOL_REQUESTED.value, event_types)
        self.assertIn(EventType.CONTEXT_USAGE.value, event_types)
        self.assertIn(EventType.TURN_COMPLETED.value, event_types)
        self.assertGreater(len(lines), 5)
        self.assertEqual(sessions[0]["session_id"], session_id)
        self.assertEqual(sessions[0]["title"], "检查注册功能")

    def test_rejects_non_local_websocket_origin(self) -> None:
        with test_directory() as directory:
            app = create_web_app(directory, directory / "sessions")
            with TestClient(app) as client:
                with self.assertRaises(WebSocketDisconnect) as raised:
                    with client.websocket_connect(
                        "/ws",
                        headers={"origin": "https://evil.example"},
                    ):
                        pass

        self.assertEqual(raised.exception.code, 1008)

    def test_websocket_approval_resolves_synchronous_agent_callback(self) -> None:
        with test_directory() as directory:
            app = create_web_app(
                directory,
                directory / "sessions",
                agent_factory=lambda handler: _ApprovalAgent(handler),
            )
            with TestClient(app) as client:
                with client.websocket_connect("/ws") as websocket:
                    websocket.receive_json()
                    websocket.send_json({"type": "run", "text": "push"})
                    approval = None
                    while approval is None:
                        message = websocket.receive_json()
                        if (
                            message["type"] == "event"
                            and message["event"]["event_type"]
                            == EventType.TOOL_APPROVAL_REQUIRED.value
                        ):
                            approval = message["event"]
                    websocket.send_json(
                        {
                            "type": "approval",
                            "request_id": approval["payload"]["request_id"],
                            "approved": True,
                        }
                    )
                    decided = None
                    while decided is None:
                        message = websocket.receive_json()
                        if (
                            message["type"] == "event"
                            and message["event"]["event_type"]
                            == EventType.TOOL_APPROVAL_DECIDED.value
                        ):
                            decided = message["event"]

        self.assertTrue(decided["payload"]["approved"])

    def test_stop_denies_pending_approval_and_preserves_decision_event(self) -> None:
        with test_directory() as directory:
            app = create_web_app(
                directory,
                directory / "sessions",
                agent_factory=lambda handler: _ApprovalAgent(handler),
            )
            with TestClient(app) as client:
                with client.websocket_connect("/ws") as websocket:
                    websocket.receive_json()
                    websocket.send_json({"type": "run", "text": "push"})
                    while True:
                        message = websocket.receive_json()
                        if (
                            message["type"] == "event"
                            and message["event"]["event_type"]
                            == EventType.TOOL_APPROVAL_REQUIRED.value
                        ):
                            break
                    websocket.send_json({"type": "stop"})
                    received: list[dict] = []
                    while True:
                        message = websocket.receive_json()
                        if message["type"] != "event":
                            continue
                        received.append(message["event"])
                        if message["event"]["event_type"] == EventType.TURN_COMPLETED.value:
                            break

        decisions = [
            event
            for event in received
            if event["event_type"] == EventType.TOOL_APPROVAL_DECIDED.value
        ]
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0]["payload"]["approved"])
        self.assertEqual(received[-1]["payload"]["reason"], "user_cancelled")

    def test_session_can_be_resumed_in_a_new_websocket(self) -> None:
        with test_directory() as directory:
            app = create_web_app(directory, directory / "sessions")
            with TestClient(app) as client:
                with client.websocket_connect("/ws") as first:
                    session_id = first.receive_json()["session_id"]
                    first.send_json({"type": "run", "text": "第一轮"})
                    while True:
                        message = first.receive_json()
                        if (
                            message["type"] == "event"
                            and message["event"]["event_type"]
                            == EventType.TURN_COMPLETED.value
                        ):
                            break

                with client.websocket_connect(
                    f"/ws?session_id={session_id}"
                ) as resumed:
                    ready = resumed.receive_json()

        history_types = [event["event_type"] for event in ready["history"]]
        self.assertEqual(ready["session_id"], session_id)
        self.assertIn(EventType.SESSION_RESUMED.value, history_types)
        self.assertIn(EventType.USER_MESSAGE.value, history_types)


if __name__ == "__main__":
    unittest.main()

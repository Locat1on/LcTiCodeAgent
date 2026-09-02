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
from code_agent.session import SessionLog
from code_agent.web import ApprovalBroker, _live_provider_setup, create_web_app
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


class _ReasoningAgent(_ApprovalAgent):
    def respond(self, user_text, session_id, turn_id):
        yield AgentEvent.create(
            EventType.ASSISTANT_REASONING_DELTA,
            session_id,
            {"text": "先检查约束。", "kind": "summary"},
            turn_id=turn_id,
        )
        yield AgentEvent.create(
            EventType.ASSISTANT_MESSAGE,
            session_id,
            {
                "text": "完成。",
                "finish_reason": "stop",
                "tool_calls": None,
                "reasoning_summary": "先检查约束。",
                "reasoning": "raw reasoning",
                "reasoning_details": [
                    {
                        "type": "reasoning.encrypted",
                        "data": "opaque",
                    }
                ],
            },
            turn_id=turn_id,
        )
        yield AgentEvent.create(
            EventType.TURN_COMPLETED,
            session_id,
            {"reason": "stop"},
            turn_id=turn_id,
        )


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
    def test_live_web_can_start_without_environment_credentials(self) -> None:
        with test_directory() as directory:
            factory, options, default_provider, credentials = (
                _live_provider_setup(directory, env={})
            )

            self.assertEqual(default_provider, "openrouter")
            self.assertFalse(any(option["configured"] for option in options))
            credentials["openrouter"] = "memory-only-secret"
            agent = factory(
                "openrouter",
                lambda request: False,
                "google/gemini-3.7-flash",
            )

        self.assertEqual(agent.model, "google/gemini-3.7-flash")
        self.assertNotIn("memory-only-secret", repr(agent.provider.config))

    def test_web_api_key_is_stored_only_in_process_memory(self) -> None:
        credentials: dict[str, str] = {}
        options = [
            {
                "id": "google",
                "label": "Google Gemini",
                "configured": False,
                "api_key_env": "GEMINI_API_KEY",
                "default_model": "gemini-3.7-flash",
                "models": ["gemini-3.7-flash"],
                "credential_entry_supported": True,
            }
        ]
        with test_directory() as directory:
            session_root = directory / "sessions"
            app = create_web_app(
                directory,
                session_root,
                provider_factory=lambda provider, handler, model: _ApprovalAgent(handler),
                provider_options=options,
                default_provider="google",
                provider_credentials=credentials,
            )
            with TestClient(app) as client:
                configured = client.post(
                    "/api/providers/google/credential",
                    json={
                        "api_key": "web-memory-secret",
                        "model": "gemini-3.7-flash",
                    },
                )
                catalog = client.get("/api/providers").json()
                with client.websocket_connect(
                    "/ws?provider=google&model=gemini-3.7-flash"
                ) as websocket:
                    ready = websocket.receive_json()

            session_text = "".join(
                path.read_text(encoding="utf-8")
                for path in session_root.glob("*.jsonl")
            )

        self.assertEqual(configured.status_code, 200)
        self.assertEqual(configured.json()["stored"], "process_memory")
        self.assertEqual(credentials, {"google": "web-memory-secret"})
        self.assertTrue(catalog["providers"][0]["configured"])
        self.assertEqual(ready["provider"], "google")
        self.assertNotIn("web-memory-secret", configured.text)
        self.assertNotIn("web-memory-secret", repr(catalog))
        self.assertNotIn("web-memory-secret", session_text)

    def test_provider_credential_rejects_non_local_origin(self) -> None:
        options = [
            {
                "id": "google",
                "label": "Google Gemini",
                "configured": False,
                "api_key_env": "GEMINI_API_KEY",
                "default_model": "gemini-3.7-flash",
                "models": ["gemini-3.7-flash"],
                "credential_entry_supported": True,
            }
        ]
        with test_directory() as directory:
            app = create_web_app(
                directory,
                directory / "sessions",
                provider_factory=lambda provider, handler, model: _ApprovalAgent(handler),
                provider_options=options,
                default_provider="google",
            )
            with TestClient(app) as client:
                response = client.post(
                    "/api/providers/google/credential",
                    headers={"origin": "https://evil.example"},
                    json={"api_key": "secret", "model": "gemini-3.7-flash"},
                )
                invalid_key = client.post(
                    "/api/providers/google/credential",
                    json={
                        "api_key": "line-one\nline-two",
                        "model": "gemini-3.7-flash",
                    },
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(invalid_key.status_code, 400)
        self.assertEqual(app.state.runtime.provider_credentials, {})

    def test_web_provider_selection_uses_server_side_configuration(self) -> None:
        calls: list[tuple[str, str | None]] = []
        options = [
            {
                "id": "google",
                "label": "Google Gemini",
                "configured": True,
                "api_key_env": "GEMINI_API_KEY",
                "default_model": "gemini-3.7-flash",
                "models": ["gemini-3.7-flash"],
            },
            {
                "id": "deepseek",
                "label": "DeepSeek",
                "configured": False,
                "api_key_env": "DEEPSEEK_API_KEY",
                "default_model": "deepseek-v4-flash",
                "models": ["deepseek-v4-flash"],
            },
        ]

        def provider_factory(provider_id, approval_handler, model):
            calls.append((provider_id, model))
            return _ApprovalAgent(approval_handler)

        with test_directory() as directory:
            app = create_web_app(
                directory,
                directory / "sessions",
                provider_factory=provider_factory,
                provider_options=options,
                default_provider="google",
            )
            with TestClient(app) as client:
                providers = client.get("/api/providers").json()
                with client.websocket_connect(
                    "/ws?provider=google&model=gemini-3.7-flash"
                ) as websocket:
                    ready = websocket.receive_json()

        self.assertEqual(calls, [("google", "gemini-3.7-flash")])
        self.assertEqual(ready["provider"], "google")
        self.assertEqual(providers["default_provider"], "google")
        self.assertNotIn("secret", repr(providers).lower())

    def test_web_rejects_unconfigured_provider(self) -> None:
        options = [
            {
                "id": "deepseek",
                "label": "DeepSeek",
                "configured": False,
                "api_key_env": "DEEPSEEK_API_KEY",
                "default_model": "deepseek-v4-flash",
                "models": ["deepseek-v4-flash"],
            }
        ]
        with test_directory() as directory:
            app = create_web_app(
                directory,
                directory / "sessions",
                provider_factory=lambda provider, handler, model: _ApprovalAgent(handler),
                provider_options=options,
                default_provider="deepseek",
            )
            with TestClient(app) as client:
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/ws?provider=deepseek"):
                        pass

    def test_session_can_be_renamed_and_soft_deleted(self) -> None:
        with test_directory() as directory:
            session_root = directory / "sessions"
            app = create_web_app(directory, session_root)
            with TestClient(app) as client:
                with client.websocket_connect("/ws") as websocket:
                    session_id = websocket.receive_json()["session_id"]

                renamed = client.patch(
                    f"/api/sessions/{session_id}",
                    json={"title": "重命名后的会话"},
                )
                sessions = client.get("/api/sessions").json()["sessions"]
                deleted = client.delete(f"/api/sessions/{session_id}")

            original = session_root / f"{session_id}.jsonl"
            trashed = list((session_root / ".trash").glob(f"{session_id}-*.jsonl"))
            original_exists = original.exists()

        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(sessions[0]["title"], "重命名后的会话")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["recoverable"])
        self.assertFalse(original_exists)
        self.assertEqual(len(trashed), 1)

    def test_active_session_cannot_be_deleted(self) -> None:
        with test_directory() as directory:
            app = create_web_app(directory, directory / "sessions")
            with TestClient(app) as client:
                with client.websocket_connect("/ws") as websocket:
                    session_id = websocket.receive_json()["session_id"]
                    response = client.delete(f"/api/sessions/{session_id}")

        self.assertEqual(response.status_code, 409)
        self.assertIn("active session", response.json()["error"])

    def test_websocket_suggests_files_and_context_references(self) -> None:
        with test_directory() as directory:
            (directory / "app.py").write_text("value = 1\n", encoding="utf-8")
            (directory / ".env").write_text("SECRET=value\n", encoding="utf-8")
            app = create_web_app(directory, directory / "sessions")
            with TestClient(app) as client:
                with client.websocket_connect("/ws") as websocket:
                    websocket.receive_json()
                    websocket.send_json(
                        {
                            "type": "suggest",
                            "trigger": "@",
                            "query": "app",
                            "request_id": "files-1",
                        }
                    )
                    files = websocket.receive_json()
                    websocket.send_json(
                        {
                            "type": "suggest",
                            "trigger": "#",
                            "query": "git",
                            "request_id": "context-1",
                        }
                    )
                    contexts = websocket.receive_json()

        self.assertEqual(files["request_id"], "files-1")
        self.assertEqual([item["value"] for item in files["items"]], ["app.py"])
        self.assertEqual(
            [item["value"] for item in contexts["items"]],
            ["git-status", "git-diff"],
        )

    def test_referenced_run_hides_model_text_from_browser_but_logs_it(self) -> None:
        with test_directory() as directory:
            (directory / "app.py").write_text("value = 1\n", encoding="utf-8")
            session_root = directory / "sessions"
            app = create_web_app(directory, session_root)
            with TestClient(app) as client:
                with client.websocket_connect("/ws") as websocket:
                    ready = websocket.receive_json()
                    websocket.send_json(
                        {
                            "type": "run",
                            "text": "检查文件",
                            "references": [{"kind": "file", "value": "app.py"}],
                        }
                    )
                    browser_user = None
                    while True:
                        message = websocket.receive_json()
                        if message["type"] != "event":
                            continue
                        event = message["event"]
                        if event["event_type"] == EventType.USER_MESSAGE:
                            browser_user = event
                        if event["event_type"] == EventType.TURN_COMPLETED:
                            break

            log = SessionLog(session_root, session_id=ready["session_id"])
            logged_user = next(
                event
                for event in log.load()
                if event.event_type is EventType.USER_MESSAGE
            )

        self.assertEqual(browser_user["payload"]["references"][0]["value"], "app.py")
        self.assertNotIn("model_text", browser_user["payload"])
        self.assertIn("@app.py", logged_user.payload["model_text"])
        self.assertEqual(logged_user.payload["text"], "检查文件")

    def test_browser_receives_summary_but_not_raw_reasoning_details(self) -> None:
        with test_directory() as directory:
            app = create_web_app(
                directory,
                directory / "sessions",
                agent_factory=lambda handler: _ReasoningAgent(handler),
            )
            with TestClient(app) as client:
                with client.websocket_connect("/ws") as websocket:
                    websocket.receive_json()
                    websocket.send_json({"type": "run", "text": "think"})
                    received: list[dict] = []
                    while True:
                        message = websocket.receive_json()
                        if message["type"] != "event":
                            continue
                        received.append(message["event"])
                        if message["event"]["event_type"] == EventType.TURN_COMPLETED:
                            break

        reasoning = next(
            event
            for event in received
            if event["event_type"] == EventType.ASSISTANT_REASONING_DELTA
        )
        assistant = next(
            event
            for event in received
            if event["event_type"] == EventType.ASSISTANT_MESSAGE
        )
        self.assertEqual(reasoning["payload"]["text"], "先检查约束。")
        self.assertEqual(assistant["payload"]["reasoning_summary"], "先检查约束。")
        self.assertNotIn("reasoning", assistant["payload"])
        self.assertNotIn("reasoning_details", assistant["payload"])

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
        self.assertIn('id="providerApiKeyInput"', page.text)
        self.assertIn('type="password"', page.text)
        self.assertIn("思考摘要", script.text)
        self.assertIn("等待模型配置", script.text)
        self.assertNotIn("localStorage", script.text)
        self.assertNotIn("sessionStorage", script.text)
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

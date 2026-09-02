"""Local Starlette UI that streams the existing AgentEvent protocol."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .events import AgentEvent, EventType
from .git_tools import GitInspector, GitToolError
from .restore import RestoreReport
from .references import ReferenceError, WorkspaceReferences
from .security import ApprovalHandler, ApprovalRequest
from .session import SessionLog
from .simulator import SimulatedAgent


SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9-]+")
MAX_HISTORY_EVENTS = 500
MAX_USER_TEXT = 20_000
MAX_API_KEY_LENGTH = 4_096
APPROVAL_TIMEOUT_SECONDS = 300.0


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self' "
            "ws://127.0.0.1:* ws://localhost:* ws://[::1]:*; "
            "img-src 'self'; style-src 'self'; script-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response


class WebAgent(Protocol):
    mode: str
    model: str
    sandbox: str
    context_limit: int
    used_tokens: int

    def respond(
        self,
        user_text: str,
        session_id: str,
        turn_id: str,
    ) -> Iterator[AgentEvent]: ...

    def restore(self, events: list[AgentEvent]) -> RestoreReport | None: ...

    def clear_context(self) -> None: ...

    def compact_context(self, session_id: str) -> Iterator[AgentEvent]: ...

    def context_stats(self) -> dict[str, Any]: ...


AgentFactory = Callable[[ApprovalHandler], WebAgent]
ProviderAgentFactory = Callable[[str, ApprovalHandler, str | None], WebAgent]


@dataclass(slots=True)
class _PendingApproval:
    event: threading.Event
    decision: bool | None = None


class ApprovalBroker:
    """Bridge a synchronous approval callback to WebSocket decisions."""

    def __init__(self, timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingApproval] = {}

    def prepare(self, request_id: str) -> None:
        with self._lock:
            self._pending.setdefault(
                request_id,
                _PendingApproval(threading.Event()),
            )

    def wait(self, request: ApprovalRequest) -> bool:
        with self._lock:
            pending = self._pending.setdefault(
                request.request_id,
                _PendingApproval(threading.Event()),
            )
        completed = pending.event.wait(self.timeout_seconds)
        with self._lock:
            current = self._pending.pop(request.request_id, pending)
        return bool(completed and current.decision)

    def resolve(self, request_id: str, approved: bool) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.decision is not None:
                return False
            pending.decision = bool(approved)
            pending.event.set()
            return True

    def deny_all(self) -> None:
        with self._lock:
            pending = list(self._pending.values())
            for item in pending:
                item.decision = False
                item.event.set()


class WebSession:
    def __init__(
        self,
        workspace: Path,
        session_root: Path,
        agent_factory: AgentFactory,
        *,
        session_id: str | None = None,
        provider_id: str = "simulation",
    ) -> None:
        self.workspace = workspace.resolve()
        self.log = SessionLog(session_root, session_id=session_id)
        self.approvals = ApprovalBroker()
        self.agent = agent_factory(self.approvals.wait)
        self.provider_id = provider_id
        self.references = WorkspaceReferences(self.workspace)
        self._cancel = threading.Event()
        self._resumed = session_id is not None

    def start(self) -> dict[str, Any]:
        if self._resumed:
            events = self.log.load()
            if not events:
                raise ValueError("session log is empty")
            report = self.agent.restore(events)
            payload: dict[str, Any] = {"events_replayed": len(events)}
            if report is not None:
                payload.update(report.to_payload())
            resumed = AgentEvent.create(
                EventType.SESSION_RESUMED,
                self.log.session_id,
                payload,
            )
            self.log.append(resumed)
            history = [*events, resumed]
        else:
            started = AgentEvent.create(
                EventType.SESSION_STARTED,
                self.log.session_id,
                {
                    "workspace": str(self.workspace),
                    "mode": self.agent.mode,
                    "provider": self.provider_id,
                    "model": self.agent.model,
                    "sandbox": self.agent.sandbox,
                    "context_limit": self.agent.context_limit,
                },
            )
            self.log.append(started)
            history = [started]
        return {
            "type": "ready",
            "session_id": self.log.session_id,
            "workspace": str(self.workspace),
            "mode": self.agent.mode,
            "provider": self.provider_id,
            "model": self.agent.model,
            "reasoning_effort": getattr(
                self.agent,
                "reasoning_effort",
                None,
            ),
            "sandbox": self.agent.sandbox,
            "branch": _branch_name(self.workspace),
            "history": [
                _browser_event(event) for event in history[-MAX_HISTORY_EVENTS:]
            ],
            "context": self.agent.context_stats(),
        }

    def run_turn(
        self,
        text: str,
        references: list[dict[str, str]],
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        self._cancel.clear()
        try:
            model_text = self.references.compose_model_text(
                text,
                references,
                context_stats=self.agent.context_stats(),
                log=self.log,
            )
        except ReferenceError as error:
            self._enqueue(loop, queue, _protocol_error(str(error)))
            return
        turn_id = str(uuid4())
        user_event = AgentEvent.create(
            EventType.USER_MESSAGE,
            self.log.session_id,
            {
                "text": text,
                "references": references,
                "model_text": model_text,
            },
            turn_id=turn_id,
        )
        self._publish(user_event, loop, queue)
        iterator = self.agent.respond(model_text, self.log.session_id, turn_id)
        evidence_events = {
            EventType.TOOL_APPROVAL_DECIDED,
            EventType.TOOL_COMPLETED,
            EventType.TOOL_FAILED,
        }
        try:
            for event in iterator:
                if self._cancel.is_set():
                    if event.event_type in evidence_events:
                        self._publish(event, loop, queue)
                        continue
                    close = getattr(iterator, "close", None)
                    if callable(close):
                        close()
                    cancelled = AgentEvent.create(
                        EventType.TURN_COMPLETED,
                        self.log.session_id,
                        {"reason": "user_cancelled"},
                        turn_id=turn_id,
                    )
                    self._publish(cancelled, loop, queue)
                    break
                self._publish(event, loop, queue)
        except Exception as error:
            failure = AgentEvent.create(
                EventType.ERROR,
                self.log.session_id,
                {
                    "message": f"Web turn failed: {type(error).__name__}",
                    "kind": type(error).__name__,
                },
                turn_id=turn_id,
            )
            self._publish(failure, loop, queue)
            completed = AgentEvent.create(
                EventType.TURN_COMPLETED,
                self.log.session_id,
                {"reason": "web_error"},
                turn_id=turn_id,
            )
            self._publish(completed, loop, queue)
        finally:
            self.references.refresh()
            self._enqueue(
                loop,
                queue,
                {"type": "snapshot", "context": self.agent.context_stats()},
            )

    def compact(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        for event in self.agent.compact_context(self.log.session_id):
            self._publish(event, loop, queue)
        self._enqueue(
            loop,
            queue,
            {"type": "snapshot", "context": self.agent.context_stats()},
        )

    def clear(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        self.agent.clear_context()
        event = AgentEvent.create(
            EventType.CONTEXT_CLEARED,
            self.log.session_id,
            {"used_tokens": 0},
        )
        self._publish(event, loop, queue)
        self._enqueue(
            loop,
            queue,
            {"type": "snapshot", "context": self.agent.context_stats()},
        )

    def recall(self, event_id: str) -> dict[str, Any]:
        event = self.log.recall(event_id)
        return {
            "type": "recalled",
            "event_id": event_id,
            "event": event.to_dict() if event else None,
        }

    def suggest(self, trigger: str, query: str, request_id: str) -> dict[str, Any]:
        return {
            "type": "suggestions",
            "request_id": request_id,
            "trigger": trigger,
            "items": self.references.suggest(trigger, query),
        }

    def cancel(self) -> None:
        self._cancel.set()
        self.approvals.deny_all()

    def _publish(
        self,
        event: AgentEvent,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        if event.event_type is EventType.TOOL_APPROVAL_REQUIRED:
            request_id = str(event.payload.get("request_id", ""))
            if request_id:
                self.approvals.prepare(request_id)
        self.log.append(event)
        self._enqueue(
            loop,
            queue,
            {"type": "event", "event": _browser_event(event)},
        )

    @staticmethod
    def _enqueue(
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[dict[str, Any] | None],
        message: dict[str, Any],
    ) -> None:
        asyncio.run_coroutine_threadsafe(queue.put(message), loop).result()


class WebRuntime:
    def __init__(
        self,
        workspace: Path,
        session_root: Path,
        provider_factory: ProviderAgentFactory,
        provider_options: list[dict[str, Any]],
        default_provider: str,
        provider_credentials: dict[str, str] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.session_root = session_root.resolve()
        self.provider_factory = provider_factory
        self.provider_options = provider_options
        self.default_provider = default_provider
        self.provider_credentials = provider_credentials if provider_credentials is not None else {}
        self._active_lock = threading.Lock()
        self._active_sessions: dict[str, int] = {}

    def create_session(
        self,
        session_id: str | None,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> WebSession:
        if session_id is not None and not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("invalid session id")
        if session_id is not None:
            path = self.session_root / f"{session_id}.jsonl"
            if not path.is_file():
                raise ValueError("session log not found")
            events = SessionLog(self.session_root, session_id=session_id).load()
            if not events:
                raise ValueError("session log is empty")
            started = events[0].payload
            provider_id = str(started.get("provider") or self.default_provider)
            model = str(started.get("model") or model or "") or None
        selected_provider = provider_id or self.default_provider
        option = next(
            (
                item
                for item in self.provider_options
                if item["id"] == selected_provider and item["configured"]
            ),
            None,
        )
        if option is None:
            raise ValueError("model provider is not configured")

        def build(approval_handler: ApprovalHandler) -> WebAgent:
            return self.provider_factory(selected_provider, approval_handler, model)

        return WebSession(
            self.workspace,
            self.session_root,
            build,
            session_id=session_id,
            provider_id=selected_provider,
        )

    def set_provider_credential(
        self,
        provider_id: str,
        api_key: str,
        model: str,
    ) -> dict[str, Any]:
        option = next(
            (item for item in self.provider_options if item["id"] == provider_id),
            None,
        )
        if option is None or not option.get("api_key_env"):
            raise ValueError("model provider does not accept an API key")
        if not option.get("credential_entry_supported", True):
            raise ValueError("provider base URL must be configured through the environment")
        normalized_key = api_key.strip()
        if (
            not normalized_key
            or len(normalized_key) > MAX_API_KEY_LENGTH
            or any(character in normalized_key for character in "\r\n\0")
        ):
            raise ValueError("API key must contain between 1 and 4096 safe characters")
        normalized_model = model.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", normalized_model):
            raise ValueError("model name is invalid")
        self.provider_credentials[provider_id] = normalized_key
        option["configured"] = True
        option["default_model"] = normalized_model
        option["models"] = list(
            dict.fromkeys((normalized_model, *option.get("models", [])))
        )
        return {
            "id": provider_id,
            "configured": True,
            "stored": "process_memory",
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        if not self.session_root.exists():
            return []
        sessions: list[dict[str, Any]] = []
        for path in self.session_root.glob("*.jsonl"):
            session_id = path.stem
            if not SESSION_ID_PATTERN.fullmatch(session_id):
                continue
            try:
                log = SessionLog(self.session_root, session_id=session_id)
                events = log.load()
            except ValueError:
                continue
            title = "新会话"
            provider = str(events[0].payload.get("provider") or self.default_provider)
            model = str(events[0].payload.get("model") or "")
            for event in events:
                if event.event_type is EventType.USER_MESSAGE:
                    text = str(event.payload.get("text", "")).strip()
                    if text:
                        title = text[:42]
                        break
            for event in events:
                if event.event_type is EventType.SESSION_RENAMED:
                    renamed = str(event.payload.get("title", "")).strip()
                    if renamed:
                        title = renamed
            sessions.append(
                {
                    "session_id": session_id,
                    "title": title,
                    "events": len(events),
                    "updated": path.stat().st_mtime,
                    "provider": provider,
                    "model": model,
                }
            )
        sessions.sort(key=lambda item: item["updated"], reverse=True)
        return sessions[:50]

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        normalized = title.strip()
        if not normalized or len(normalized) > 80 or "\n" in normalized or "\r" in normalized:
            raise ValueError("session title must be a single line of 1 to 80 characters")
        log = SessionLog(self.session_root, session_id=session_id)
        log.append(
            AgentEvent.create(
                EventType.SESSION_RENAMED,
                session_id,
                {"title": normalized},
            )
        )
        return {"session_id": session_id, "title": normalized, "path": str(path)}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        with self._active_lock:
            if self._active_sessions.get(session_id, 0):
                raise RuntimeError("active session cannot be deleted")
        trash = self.session_root / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        destination = trash / f"{session_id}-{uuid4().hex[:8]}.jsonl"
        path.replace(destination)
        return {
            "session_id": session_id,
            "deleted": True,
            "recoverable": True,
        }

    def acquire_session(self, session_id: str) -> None:
        with self._active_lock:
            self._active_sessions[session_id] = self._active_sessions.get(session_id, 0) + 1

    def release_session(self, session_id: str) -> None:
        with self._active_lock:
            remaining = self._active_sessions.get(session_id, 0) - 1
            if remaining > 0:
                self._active_sessions[session_id] = remaining
            else:
                self._active_sessions.pop(session_id, None)

    def _session_path(self, session_id: str) -> Path:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("invalid session id")
        path = self.session_root / f"{session_id}.jsonl"
        if not path.is_file():
            raise ValueError("session log not found")
        return path


def create_web_app(
    workspace: Path,
    session_root: Path,
    agent_factory: AgentFactory | None = None,
    *,
    provider_factory: ProviderAgentFactory | None = None,
    provider_options: list[dict[str, Any]] | None = None,
    default_provider: str | None = None,
    provider_credentials: dict[str, str] | None = None,
) -> Starlette:
    static_root = Path(__file__).with_name("web_static")
    if provider_factory is None:
        selected_factory = agent_factory or (lambda approval_handler: SimulatedAgent())

        def provider_factory(
            provider_id: str,
            approval_handler: ApprovalHandler,
            model: str | None,
        ) -> WebAgent:
            return selected_factory(approval_handler)

    configured_options = provider_options or [
        {
            "id": "simulation",
            "label": "离线模拟",
            "configured": True,
            "api_key_env": None,
            "default_model": "simulated-local",
            "models": ["simulated-local"],
            "credential_entry_supported": False,
        }
    ]
    selected_default = default_provider or configured_options[0]["id"]
    runtime = WebRuntime(
        workspace,
        session_root,
        provider_factory,
        configured_options,
        selected_default,
        provider_credentials,
    )

    async def index(request: Request) -> FileResponse:
        return FileResponse(static_root / "index.html")

    async def sessions(request: Request) -> JSONResponse:
        return JSONResponse({"sessions": runtime.list_sessions()})

    async def providers(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "providers": runtime.provider_options,
                "default_provider": runtime.default_provider,
            }
        )

    async def provider_credential(request: Request) -> JSONResponse:
        if not _is_local_origin(request.headers.get("origin")):
            return JSONResponse(
                {"error": "non-local origin is not allowed"},
                status_code=403,
            )
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > 8_192:
            return JSONResponse({"error": "request body is too large"}, status_code=413)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            api_key = payload.get("api_key")
            model = payload.get("model")
            if not isinstance(api_key, str) or not isinstance(model, str):
                raise ValueError("api_key and model are required")
            return JSONResponse(
                runtime.set_provider_credential(
                    request.path_params["provider_id"],
                    api_key,
                    model,
                )
            )
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "request body must be valid JSON"},
                status_code=400,
            )
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)

    async def session_operation(request: Request) -> JSONResponse:
        if not _is_local_origin(request.headers.get("origin")):
            return JSONResponse({"error": "non-local origin is not allowed"}, status_code=403)
        session_id = request.path_params["session_id"]
        try:
            if request.method == "PATCH":
                payload = await request.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("title"), str):
                    raise ValueError("session title is required")
                return JSONResponse(runtime.rename_session(session_id, payload["title"]))
            return JSONResponse(runtime.delete_session(session_id))
        except json.JSONDecodeError:
            return JSONResponse({"error": "request body must be valid JSON"}, status_code=400)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=404 if "not found" in str(error) else 400)
        except RuntimeError as error:
            return JSONResponse({"error": str(error)}, status_code=409)

    async def websocket_endpoint(websocket: WebSocket) -> None:
        if not _is_local_origin(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        session_id = websocket.query_params.get("session_id")
        provider_id = websocket.query_params.get("provider")
        model = websocket.query_params.get("model")
        try:
            session = runtime.create_session(session_id, provider_id, model)
            ready = session.start()
        except ValueError as error:
            await websocket.close(code=1008, reason=str(error))
            return

        await websocket.accept()
        runtime.acquire_session(session.log.session_id)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        await queue.put(ready)
        loop = asyncio.get_running_loop()
        active_task: asyncio.Task[None] | None = None

        async def sender() -> None:
            while True:
                message = await queue.get()
                if message is None:
                    return
                try:
                    await websocket.send_json(message)
                except (RuntimeError, WebSocketDisconnect):
                    return

        sender_task = asyncio.create_task(sender())
        try:
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    await queue.put(_protocol_error("message must be an object"))
                    continue
                message_type = message.get("type")
                if message_type == "run":
                    text = message.get("text")
                    if not isinstance(text, str) or not text.strip():
                        await queue.put(_protocol_error("task text is required"))
                        continue
                    if len(text) > MAX_USER_TEXT:
                        await queue.put(_protocol_error("task text is too long"))
                        continue
                    if active_task is not None and not active_task.done():
                        await queue.put(_protocol_error("a turn is already running"))
                        continue
                    try:
                        references = session.references.normalize(
                            message.get("references")
                        )
                    except ReferenceError as error:
                        await queue.put(_protocol_error(str(error)))
                        continue
                    active_task = asyncio.create_task(
                        asyncio.to_thread(
                            session.run_turn,
                            text.strip(),
                            references,
                            loop,
                            queue,
                        )
                    )
                elif message_type == "suggest":
                    trigger = message.get("trigger")
                    query = message.get("query", "")
                    request_id = message.get("request_id")
                    if (
                        trigger not in {"@", "#"}
                        or not isinstance(query, str)
                        or not isinstance(request_id, str)
                    ):
                        await queue.put(_protocol_error("invalid suggestion request"))
                    else:
                        try:
                            await queue.put(
                                await asyncio.to_thread(
                                    session.suggest,
                                    trigger,
                                    query[:200],
                                    request_id,
                                )
                            )
                        except ReferenceError as error:
                            await queue.put(_protocol_error(str(error)))
                elif message_type == "approval":
                    request_id = message.get("request_id")
                    approved = message.get("approved")
                    if not isinstance(request_id, str) or not isinstance(approved, bool):
                        await queue.put(_protocol_error("invalid approval decision"))
                    elif not session.approvals.resolve(request_id, approved):
                        await queue.put(_protocol_error("approval request is not pending"))
                elif message_type == "stop":
                    session.cancel()
                elif message_type == "compact":
                    if active_task is not None and not active_task.done():
                        await queue.put(_protocol_error("wait for the active turn"))
                    else:
                        active_task = asyncio.create_task(
                            asyncio.to_thread(session.compact, loop, queue)
                        )
                elif message_type == "clear":
                    if active_task is not None and not active_task.done():
                        await queue.put(_protocol_error("wait for the active turn"))
                    else:
                        active_task = asyncio.create_task(
                            asyncio.to_thread(session.clear, loop, queue)
                        )
                elif message_type == "recall":
                    event_id = message.get("event_id")
                    if not isinstance(event_id, str) or not event_id.strip():
                        await queue.put(_protocol_error("event_id is required"))
                    else:
                        await queue.put(session.recall(event_id.strip()))
                else:
                    await queue.put(_protocol_error("unknown message type"))
        except WebSocketDisconnect:
            pass
        finally:
            session.cancel()
            runtime.release_session(session.log.session_id)
            await queue.put(None)
            await sender_task

    routes = [
        Route("/", index),
        Route("/api/sessions", sessions),
        Route("/api/providers", providers),
        Route(
            "/api/providers/{provider_id}/credential",
            provider_credential,
            methods=["POST"],
        ),
        Route(
            "/api/sessions/{session_id}",
            session_operation,
            methods=["PATCH", "DELETE"],
        ),
        WebSocketRoute("/ws", websocket_endpoint),
        Mount("/static", StaticFiles(directory=static_root), name="static"),
    ]
    middleware = [
        Middleware(
            TrustedHostMiddleware,
            allowed_hosts=["127.0.0.1", "localhost", "testserver"],
        ),
        Middleware(SecurityHeadersMiddleware),
    ]
    app = Starlette(routes=routes, middleware=middleware)
    app.state.runtime = runtime
    return app


def _is_local_origin(origin: str | None) -> bool:
    if origin is None:
        return True
    hostname = urlsplit(origin).hostname
    return hostname in {"127.0.0.1", "localhost", "::1", "testserver"}


def _protocol_error(message: str) -> dict[str, Any]:
    return {"type": "protocol_error", "message": message}


def _browser_event(event: AgentEvent) -> dict[str, Any]:
    data = event.to_dict()
    if event.event_type is EventType.USER_MESSAGE:
        data["payload"].pop("model_text", None)
    if event.event_type is EventType.ASSISTANT_MESSAGE:
        data["payload"].pop("reasoning_details", None)
        data["payload"].pop("reasoning", None)
    return data


def _branch_name(workspace: Path) -> str:
    try:
        status = GitInspector(workspace).status()["stdout"].splitlines()[0]
    except (GitToolError, IndexError):
        return "—"
    branch = status.removeprefix("## ").split("...", 1)[0].strip()
    return branch or "—"


def _live_provider_setup(
    workspace: Path,
    env: dict[str, str] | None = None,
) -> tuple[
    ProviderAgentFactory,
    list[dict[str, Any]],
    str,
    dict[str, str],
]:
    from .command import CommandRunner
    from .live_agent import LiveAgent
    from .openrouter import (
        OpenRouterConfig,
        OpenRouterProvider,
        PROVIDER_SPECS,
        provider_options_from_env,
    )
    from .tools import ToolRegistry

    values = dict(os.environ if env is None else env)
    credentials: dict[str, str] = {}
    options = provider_options_from_env(values)
    configured = [option for option in options if option["configured"]]
    default_provider = next(
        (option["id"] for option in configured if option["id"] == "openrouter"),
        configured[0]["id"] if configured else "openrouter",
    )

    def build(
        provider_id: str,
        approval_handler: ApprovalHandler,
        model: str | None,
    ) -> WebAgent:
        prepared = dict(values)
        if provider_id in credentials:
            spec = PROVIDER_SPECS[provider_id]
            prepared[spec.api_key_env] = credentials[provider_id]
        config = OpenRouterConfig.for_provider(
            provider_id,
            model=model,
            env=prepared,
        )
        command_runner = CommandRunner()
        return LiveAgent(
            OpenRouterProvider(config),
            ToolRegistry(workspace, command_runner=command_runner),
            approval_handler=approval_handler,
        )

    return build, options, default_provider, credentials


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcticode-web",
        description="Local Web UI for LcTiCodeAgent.",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--session-root", type=Path, default=Path("sessions"))
    parser.add_argument("--live", action="store_true", help="use OpenRouter")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("--host must remain local (127.0.0.1, localhost, or ::1)")
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    try:
        provider_setup = _live_provider_setup(args.workspace) if args.live else None
    except ValueError as error:
        parser.error(str(error))
    if provider_setup is None:
        app = create_web_app(args.workspace, args.session_root)
    else:
        provider_factory, options, default_provider, credentials = provider_setup
        app = create_web_app(
            args.workspace,
            args.session_root,
            provider_factory=provider_factory,
            provider_options=options,
            default_provider=default_provider,
            provider_credentials=credentials,
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

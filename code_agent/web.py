"""Local Starlette UI that streams the existing AgentEvent protocol."""

from __future__ import annotations

import argparse
import asyncio
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

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
from .security import ApprovalHandler, ApprovalRequest
from .session import SessionLog
from .simulator import SimulatedAgent


SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9-]+")
MAX_HISTORY_EVENTS = 500
MAX_USER_TEXT = 20_000
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
    ) -> None:
        self.workspace = workspace.resolve()
        self.log = SessionLog(session_root, session_id=session_id)
        self.approvals = ApprovalBroker()
        self.agent = agent_factory(self.approvals.wait)
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
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        from uuid import uuid4

        self._cancel.clear()
        turn_id = str(uuid4())
        user_event = AgentEvent.create(
            EventType.USER_MESSAGE,
            self.log.session_id,
            {"text": text},
            turn_id=turn_id,
        )
        self._publish(user_event, loop, queue)
        iterator = self.agent.respond(text, self.log.session_id, turn_id)
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
        agent_factory: AgentFactory,
    ) -> None:
        self.workspace = workspace.resolve()
        self.session_root = session_root.resolve()
        self.agent_factory = agent_factory

    def create_session(self, session_id: str | None) -> WebSession:
        if session_id is not None and not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("invalid session id")
        if session_id is not None:
            path = self.session_root / f"{session_id}.jsonl"
            if not path.is_file():
                raise ValueError("session log not found")
        return WebSession(
            self.workspace,
            self.session_root,
            self.agent_factory,
            session_id=session_id,
        )

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
            for event in events:
                if event.event_type is EventType.USER_MESSAGE:
                    text = str(event.payload.get("text", "")).strip()
                    if text:
                        title = text[:42]
                        break
            sessions.append(
                {
                    "session_id": session_id,
                    "title": title,
                    "events": len(events),
                    "updated": path.stat().st_mtime,
                }
            )
        sessions.sort(key=lambda item: item["updated"], reverse=True)
        return sessions[:50]


def create_web_app(
    workspace: Path,
    session_root: Path,
    agent_factory: AgentFactory | None = None,
) -> Starlette:
    static_root = Path(__file__).with_name("web_static")
    runtime = WebRuntime(
        workspace,
        session_root,
        agent_factory or (lambda approval_handler: SimulatedAgent()),
    )

    async def index(request: Request) -> FileResponse:
        return FileResponse(static_root / "index.html")

    async def sessions(request: Request) -> JSONResponse:
        return JSONResponse({"sessions": runtime.list_sessions()})

    async def websocket_endpoint(websocket: WebSocket) -> None:
        if not _is_local_origin(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        session_id = websocket.query_params.get("session_id")
        try:
            session = runtime.create_session(session_id)
            ready = session.start()
        except ValueError as error:
            await websocket.close(code=1008, reason=str(error))
            return

        await websocket.accept()
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
                    active_task = asyncio.create_task(
                        asyncio.to_thread(session.run_turn, text.strip(), loop, queue)
                    )
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
            await queue.put(None)
            await sender_task

    routes = [
        Route("/", index),
        Route("/api/sessions", sessions),
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


def _live_agent_factory(workspace: Path) -> AgentFactory:
    from .command import CommandRunner
    from .live_agent import LiveAgent
    from .openrouter import OpenRouterConfig, OpenRouterProvider
    from .tools import ToolRegistry

    config = OpenRouterConfig.from_env()

    def build(approval_handler: ApprovalHandler) -> WebAgent:
        command_runner = CommandRunner()
        return LiveAgent(
            OpenRouterProvider(config),
            ToolRegistry(workspace, command_runner=command_runner),
            approval_handler=approval_handler,
        )

    return build


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
        agent_factory = (
            _live_agent_factory(args.workspace)
            if args.live
            else lambda approval_handler: SimulatedAgent()
        )
    except ValueError as error:
        parser.error(str(error))
    app = create_web_app(args.workspace, args.session_root, agent_factory)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

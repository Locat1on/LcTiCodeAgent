"""Interactive command-line entry point."""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .events import AgentEvent, EventType
from .restore import RestoreReport
from .session import SessionLog
from .simulator import SimulatedAgent
from .ui import TerminalUI


SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9-]+")


class AgentBackend(Protocol):
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

    def restore(
        self,
        events: list[AgentEvent],
    ) -> RestoreReport | None: ...

    def clear_context(self) -> None: ...

    def compact_context(self, session_id: str) -> Iterator[AgentEvent]: ...

    def context_stats(self) -> dict[str, Any]: ...


class Application:
    def __init__(
        self,
        workspace: Path,
        session_root: Path,
        ui: TerminalUI | None = None,
        agent: AgentBackend | None = None,
        session_id: str | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.log = SessionLog(session_root, session_id=session_id)
        self.ui = ui or TerminalUI()
        self.agent = agent or SimulatedAgent()
        self.resumed = session_id is not None

    def start(self) -> None:
        if self.resumed:
            self._resume()
            return
        event = AgentEvent.create(
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
        self._publish(event, render=False)
        self.ui.show_header(
            self.workspace,
            self.log.session_id,
            self.agent.mode,
            self.agent.model,
            self.agent.sandbox,
        )

    def _resume(self) -> None:
        if not SESSION_ID_PATTERN.fullmatch(self.log.session_id):
            raise ValueError(
                "invalid session id; expected letters, digits, and hyphens only"
            )
        if not self.log.path.exists():
            raise ValueError(f"session log not found: {self.log.path}")
        events = self.log.load()
        first = events[0].payload if events else {}
        report = self.agent.restore(events)
        payload = {"events_replayed": len(events)}
        if report is not None:
            payload.update(report.to_payload())
        self._publish(
            AgentEvent.create(
                EventType.SESSION_RESUMED,
                self.log.session_id,
                payload,
            ),
            render=False,
        )
        self.ui.show_header(
            self.workspace,
            self.log.session_id,
            self.agent.mode,
            self.agent.model,
            self.agent.sandbox,
        )
        self.ui.console.print(
            f"[yellow]Resumed session with {len(events)} replayed events.[/yellow]"
        )
        logged_workspace = str(first.get("workspace", ""))
        if logged_workspace and logged_workspace != str(self.workspace):
            self.ui.console.print(
                f"[yellow]Warning: workspace changed since the session started "
                f"({logged_workspace}).[/yellow]"
            )
        logged_model = str(first.get("model", ""))
        if logged_model and logged_model != self.agent.model:
            self.ui.console.print(
                f"[yellow]Warning: model changed since the session started "
                f"({logged_model}).[/yellow]"
            )

    def run_turn(self, user_text: str) -> None:
        turn_id = str(uuid4())
        self._publish(
            AgentEvent.create(
                EventType.USER_MESSAGE,
                self.log.session_id,
                {"text": user_text},
                turn_id=turn_id,
            )
        )
        for event in self.agent.respond(user_text, self.log.session_id, turn_id):
            self._publish(event)

    def clear_context(self) -> None:
        self.agent.clear_context()
        self._publish(
            AgentEvent.create(
                EventType.CONTEXT_CLEARED,
                self.log.session_id,
                {"used_tokens": 0},
            )
        )

    def compact_context(self) -> None:
        for event in self.agent.compact_context(self.log.session_id):
            self._publish(event)

    def show_context(self) -> None:
        self.ui.show_context_report(self.agent.context_stats())

    def show_status(self) -> None:
        self.ui.show_status(
            self.workspace,
            self.log.path,
            self.log.event_count,
            self.agent.used_tokens,
            self.agent.context_limit,
            self.agent.sandbox,
        )

    def _publish(self, event: AgentEvent, *, render: bool = True) -> None:
        self.log.append(event)
        if render:
            self.ui.render(event)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcticode",
        description="Conversational coding agent terminal UI.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="workspace displayed by the current session",
    )
    parser.add_argument(
        "--session-root",
        type=Path,
        default=Path("sessions"),
        help="directory used for append-only JSONL session logs",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--demo",
        action="store_true",
        help="run one deterministic demonstration turn and exit",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="use OpenRouter instead of the deterministic simulator",
    )
    parser.add_argument(
        "--prompt",
        help="run one task and exit; combine with --live for a real model call",
    )
    parser.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="restore a previous session's model context from its JSONL log "
        "(requires --live)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui = TerminalUI()
    agent: AgentBackend | None = None
    if args.resume and not args.live:
        ui.console.print(
            "[red]--resume requires --live: only the live agent keeps "
            "restorable model context.[/red]"
        )
        return 2
    if args.live:
        from .command import CommandRunner
        from .live_agent import LiveAgent
        from .openrouter import (
            OpenRouterConfig,
            OpenRouterConfigurationError,
            OpenRouterProvider,
        )
        from .tools import ToolRegistry

        try:
            config = OpenRouterConfig.from_env()
        except OpenRouterConfigurationError as error:
            ui.console.print(f"[red]Configuration error: {error}[/red]")
            return 2
        command_runner = CommandRunner()
        provider = OpenRouterProvider(config)
        agent = LiveAgent(
            provider,
            ToolRegistry(args.workspace, command_runner=command_runner),
            approval_handler=ui.ask_approval,
        )

    app = Application(
        args.workspace,
        args.session_root,
        ui=ui,
        agent=agent,
        session_id=args.resume,
    )
    try:
        app.start()
    except ValueError as error:
        ui.console.print(f"[red]Resume failed: {error}[/red]")
        return 2

    one_shot_prompt = args.prompt
    if args.demo:
        one_shot_prompt = "检查注册功能并说明下一步"
    if one_shot_prompt:
        app.run_turn(one_shot_prompt)
        app.show_status()
        return 0

    app.ui.show_help()
    while True:
        try:
            user_text = app.ui.prompt()
        except (EOFError, KeyboardInterrupt):
            app.ui.console.print("\n[dim]Session ended.[/dim]")
            return 0

        if not user_text:
            continue
        if user_text == "/exit":
            app.ui.console.print("[dim]Session ended.[/dim]")
            return 0
        if user_text == "/help":
            app.ui.show_help()
            continue
        if user_text == "/status":
            app.show_status()
            continue
        if user_text == "/context":
            app.show_context()
            continue
        if user_text == "/compact":
            app.compact_context()
            continue
        if user_text == "/clear":
            app.clear_context()
            continue
        if user_text.startswith("/"):
            app.ui.console.print(f"[yellow]Unknown command: {user_text}[/yellow]")
            continue
        app.run_turn(user_text)


if __name__ == "__main__":
    raise SystemExit(main())

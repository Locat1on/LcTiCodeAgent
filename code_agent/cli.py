"""Interactive command-line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path
from uuid import uuid4

from .events import AgentEvent, EventType
from .session import SessionLog
from .simulator import SimulatedAgent
from .ui import TerminalUI


class Application:
    def __init__(
        self,
        workspace: Path,
        session_root: Path,
        ui: TerminalUI | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.log = SessionLog(session_root)
        self.ui = ui or TerminalUI()
        self.agent = SimulatedAgent()

    def start(self) -> None:
        event = AgentEvent.create(
            EventType.SESSION_STARTED,
            self.log.session_id,
            {
                "workspace": str(self.workspace),
                "mode": "simulation",
                "context_limit": self.agent.context_limit,
            },
        )
        self._publish(event, render=False)
        self.ui.show_header(self.workspace, self.log.session_id)

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

    def show_status(self) -> None:
        self.ui.show_status(
            self.workspace,
            self.log.path,
            self.log.event_count,
            self.agent.used_tokens,
            self.agent.context_limit,
        )

    def _publish(self, event: AgentEvent, *, render: bool = True) -> None:
        self.log.append(event)
        if render:
            self.ui.render(event)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcticode",
        description="Conversational coding agent terminal UI (stage-one simulation).",
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
    parser.add_argument(
        "--demo",
        action="store_true",
        help="run one deterministic demonstration turn and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = Application(args.workspace, args.session_root)
    app.start()

    if args.demo:
        app.run_turn("检查注册功能并说明下一步")
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
        if user_text in {"/status", "/context"}:
            app.show_status()
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

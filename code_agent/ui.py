"""Rich-based terminal rendering for agent events."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .events import AgentEvent, EventType
from .security import ApprovalRequest


class TerminalUI:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._assistant_stream_open = False

    def show_header(
        self,
        workspace: Path,
        session_id: str,
        mode: str = "simulation",
        model: str = "simulator",
        sandbox: str = "simulation",
    ) -> None:
        title = Text("LcTiCodeAgent", style="bold cyan")
        body = Text()
        body.append("Workspace  ", style="dim")
        body.append(str(workspace))
        body.append("\nSession    ", style="dim")
        body.append(session_id)
        body.append("\nModel      ", style="dim")
        body.append(model)
        body.append("\nSandbox    ", style="dim")
        sandbox_style = "green" if sandbox == "workspace-policy" else "yellow"
        body.append(sandbox, style=sandbox_style)
        body.append("\nStage      ", style="dim")
        if mode == "simulation":
            body.append("UI and event protocol simulation", style="yellow")
        else:
            body.append("OpenRouter live agent", style="green")
        self.console.print(Panel(body, title=title, border_style="cyan"))

    def show_help(self) -> None:
        table = Table(title="Commands", show_header=True, header_style="bold cyan")
        table.add_column("Command", no_wrap=True)
        table.add_column("Description")
        for command, description in (
            ("/help", "显示命令帮助"),
            ("/status", "显示工作区、会话和事件数量"),
            ("/context", "显示分层上下文占用"),
            ("/compact", "立即执行确定性上下文裁剪"),
            ("/clear", "清除模拟上下文，不删除审计日志"),
            ("/exit", "结束会话"),
        ):
            table.add_row(command, description)
        self.console.print(table)

    def show_status(
        self,
        workspace: Path,
        session_path: Path,
        event_count: int,
        used_tokens: int,
        limit_tokens: int,
        sandbox: str,
    ) -> None:
        ratio = used_tokens / limit_tokens if limit_tokens else 0
        self.console.print(
            Panel(
                "\n".join(
                    (
                        f"Workspace: {workspace}",
                        f"Session log: {session_path}",
                        f"Events: {event_count}",
                        f"Sandbox: {sandbox}",
                        f"Context: {used_tokens:,} / {limit_tokens:,} ({ratio:.1%})",
                    )
                ),
                title="Status",
                border_style="blue",
            )
        )

    def show_context_report(self, stats: dict[str, Any]) -> None:
        used = int(stats.get("used_tokens", 0))
        limit = int(stats.get("limit_tokens", 0))
        ratio = used / limit if limit else 0
        lines = [
            f"Model usage: {used:,} / {limit:,} ({ratio:.1%})",
            f"Estimated: {int(stats.get('estimated_tokens', 0)):,} tokens "
            f"across {int(stats.get('items', 0))} items",
        ]
        layers = stats.get("layers", {})
        for layer_name, label in (
            ("pinned", "Pinned  "),
            ("recent", "Recent  "),
            ("evidence", "Evidence"),
        ):
            layer = layers.get(layer_name, {})
            lines.append(
                f"{label}: {int(layer.get('items', 0))} items, "
                f"{int(layer.get('estimated_tokens', 0)):,} tokens"
            )
        memory = stats.get("working_memory")
        if memory:
            lines.append(
                f"Working memory: {memory.get('modified_files', 0)} modified files, "
                f"{memory.get('verified_commands', 0)} verified commands, "
                f"{memory.get('open_errors', 0)} open errors"
            )
        self.console.print(Panel("\n".join(lines), title="Context", border_style="blue"))

    def render(self, event: AgentEvent) -> None:
        handlers = {
            EventType.USER_MESSAGE: self._render_user,
            EventType.ASSISTANT_DELTA: self._render_assistant_delta,
            EventType.ASSISTANT_MESSAGE: self._render_assistant_message,
            EventType.TOOL_REQUESTED: self._render_tool_requested,
            EventType.TOOL_STARTED: self._render_tool_started,
            EventType.TOOL_COMPLETED: self._render_tool_completed,
            EventType.TOOL_FAILED: self._render_tool_failed,
            EventType.TOOL_APPROVAL_REQUIRED: self._render_approval,
            EventType.TOOL_APPROVAL_DECIDED: self._render_approval_decided,
            EventType.CONTEXT_USAGE: self._render_context,
            EventType.CONTEXT_CLEARED: self._render_context_cleared,
            EventType.CONTEXT_COMPACTION_STARTED: self._render_compaction_started,
            EventType.CONTEXT_COMPACTION_COMPLETED: self._render_compaction_completed,
            EventType.ERROR: self._render_error,
        }
        handler = handlers.get(event.event_type)
        if handler is not None:
            handler(event)

    def prompt(self) -> str:
        return self.console.input("[bold green]You> [/bold green]").strip()

    def ask_approval(self, request: ApprovalRequest) -> bool:
        try:
            answer = self.console.input(
                "[bold yellow]Allow this action once? [y/N] [/bold yellow]"
            )
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}

    def _render_user(self, event: AgentEvent) -> None:
        self._close_assistant_stream()
        self.console.print(f"[bold green]You[/bold green]  {event.payload['text']}")

    def _render_assistant_delta(self, event: AgentEvent) -> None:
        if not self._assistant_stream_open:
            self.console.print("[bold cyan]Agent[/bold cyan]  ", end="")
            self._assistant_stream_open = True
        self.console.print(event.payload["text"], end="", soft_wrap=True)

    def _render_assistant_message(self, event: AgentEvent) -> None:
        self._close_assistant_stream()

    def _render_tool_requested(self, event: AgentEvent) -> None:
        self._close_assistant_stream()
        name = event.payload["name"]
        arguments = event.payload.get("arguments", {})
        self.console.print(f"[dim]  ↳ requested[/dim] [bold]{name}[/bold] {arguments}")

    def _render_tool_started(self, event: AgentEvent) -> None:
        self.console.print(f"[yellow]  … running[/yellow]  {event.payload['name']}")

    def _render_tool_completed(self, event: AgentEvent) -> None:
        duration = event.payload.get("duration_ms", 0)
        self.console.print(
            f"[green]  ✓ completed[/green] {event.payload['name']} "
            f"[dim]{duration} ms[/dim]\n"
            f"    {event.payload.get('summary', '')}"
        )

    def _render_tool_failed(self, event: AgentEvent) -> None:
        self.console.print(
            f"[red]  ✗ failed[/red] {event.payload['name']}: "
            f"{event.payload.get('error', 'unknown error')}"
        )

    def _render_approval(self, event: AgentEvent) -> None:
        arguments = event.payload.get("arguments", {})
        self.console.print(
            Panel(
                "\n".join(
                    (
                        f"Tool: {event.payload.get('name', 'unknown')}",
                        f"Risk: {event.payload.get('risk', 'unknown')}",
                        f"Reason: {event.payload.get('reason', '')}",
                        f"Arguments: {arguments}",
                        f"Preflight: {event.payload.get('context', {})}",
                    )
                ),
                title="Permission required",
                border_style="yellow",
            )
        )

    def _render_approval_decided(self, event: AgentEvent) -> None:
        approved = bool(event.payload.get("approved"))
        if approved:
            self.console.print("[green]  ✓ approved once[/green]")
        else:
            self.console.print("[red]  ✗ permission denied[/red]")

    def _render_context(self, event: AgentEvent) -> None:
        used = event.payload["used_tokens"]
        limit = event.payload["limit_tokens"]
        ratio = used / limit if limit else 0
        color = "green" if ratio < 0.6 else "yellow" if ratio < 0.8 else "red"
        self.console.print(
            f"[{color}]  context {used:,} / {limit:,} ({ratio:.1%})[/{color}]"
        )

    def _render_context_cleared(self, event: AgentEvent) -> None:
        self.console.print("[yellow]Context cleared; the audit log was preserved.[/yellow]")

    def _render_compaction_started(self, event: AgentEvent) -> None:
        estimated = event.payload.get("estimated_tokens", 0)
        limit = event.payload.get("limit_tokens", 0)
        self.console.print(
            f"[yellow]  … compacting context "
            f"({estimated:,} / {limit:,} estimated tokens)[/yellow]"
        )

    def _render_compaction_completed(self, event: AgentEvent) -> None:
        payload = event.payload
        if not payload.get("changed"):
            self.console.print("[dim]  context already compact; nothing pruned[/dim]")
            return
        before = payload.get("before_tokens", 0)
        after = payload.get("after_tokens", 0)
        items = payload.get("items_pruned", 0)
        self.console.print(
            f"[green]  ✓ compacted context: {before:,} → {after:,} estimated tokens, "
            f"{items} tool result(s) pruned[/green]"
        )

    def _render_error(self, event: AgentEvent) -> None:
        self.console.print(f"[red]Error: {event.payload.get('message', 'unknown')}[/red]")

    def _close_assistant_stream(self) -> None:
        if self._assistant_stream_open:
            self.console.print()
            self._assistant_stream_open = False

"""Layered context management with deterministic, evidence-preserving pruning.

The manager owns the model message list for the live agent. Tier 1 compaction
never deletes messages (assistant tool_calls must keep their paired tool
messages); it rewrites old tool results in place into summaries that cite the
original session-log event, which stays recoverable through SessionLog.recall.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4


PRUNED_PREFIX = "[pruned"
DUPLICATE_PREFIX = "[duplicate"
SUMMARY_PREFIX = "[validated context summary]\n"
MIN_PRUNE_CHARS = 400
COMMAND_OUTCOME_TOOLS = {"run_command", "git_status", "git_diff", "git_log"}
KEEP_STDOUT_TAIL = 400
KEEP_STDERR_TAIL = 2_000
KEEP_GIT_STDOUT_HEAD = 1_200
KEEP_TEXT_HEAD = 600
MAX_VERIFIED_COMMANDS = 20
MAX_OPEN_ERRORS = 8


class ContextLayer(StrEnum):
    PINNED = "pinned"
    RECENT = "recent"
    EVIDENCE = "evidence"


class TokenCounter:
    """Deterministic width-aware estimate that deliberately overestimates.

    Wide characters (CJK and friends) count as one unit, other characters as
    0.3. Overestimation only makes pruning trigger earlier, which is the safe
    direction; the model-reported usage stays the displayed number.
    """

    WIDE_THRESHOLD = 0x2E80

    @staticmethod
    def estimate(text: str | None) -> int:
        if not text:
            return 0
        units = sum(
            1.0 if ord(character) >= TokenCounter.WIDE_THRESHOLD else 0.3
            for character in text
        )
        return max(1, math.ceil(units))


@dataclass(frozen=True, slots=True)
class ContextItem:
    item_id: str
    role: str
    content: str | None
    layer: ContextLayer
    tokens: int
    tool_name: str | None = None
    call_id: str | None = None
    arguments: dict[str, Any] | None = None
    tool_args_key: str | None = None
    source_event_id: str | None = None
    tool_calls: tuple[dict[str, Any], ...] | None = None
    reasoning_details: tuple[dict[str, Any], ...] | None = None
    reasoning: str | None = None
    pruned: bool = False


@dataclass(frozen=True, slots=True)
class CompactionReport:
    trigger: str
    changed: bool
    before_tokens: int
    after_tokens: int
    items_pruned: int
    rules: dict[str, int]
    pruned_event_ids: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "changed": self.changed,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "items_pruned": self.items_pruned,
            "rules": dict(self.rules),
            "pruned_event_ids": list(self.pruned_event_ids),
        }


@dataclass(slots=True)
class WorkingMemory:
    """Structured facts collected deterministically from tool evidence."""

    modified_files: dict[str, str] = field(default_factory=dict)
    verified_commands: list[dict[str, Any]] = field(default_factory=list)
    open_errors: list[str] = field(default_factory=list)

    def record(self, tool_name: str, ok: bool, result: Any) -> None:
        if tool_name in {"replace_in_file", "write_file"} and isinstance(result, dict):
            path = result.get("path")
            digest = result.get("new_sha256") or result.get("sha256")
            if isinstance(path, str) and isinstance(digest, str):
                self.modified_files[path] = digest
        if tool_name == "run_command" and isinstance(result, dict):
            argv = result.get("argv")
            if isinstance(argv, list):
                entry = {"argv": list(argv), "exit_code": result.get("exit_code")}
                self.verified_commands = [
                    item for item in self.verified_commands if item["argv"] != argv
                ]
                self.verified_commands.append(entry)
                if len(self.verified_commands) > MAX_VERIFIED_COMMANDS:
                    self.verified_commands = self.verified_commands[
                        -MAX_VERIFIED_COMMANDS:
                    ]
                if entry["exit_code"] == 0:
                    marker = f"run_command:{json.dumps(argv, ensure_ascii=False)}"
                    self.open_errors = [
                        error
                        for error in self.open_errors
                        if not error.startswith(marker)
                    ]
        if not ok:
            if isinstance(result, dict):
                summary = json.dumps(result, ensure_ascii=False)
            else:
                summary = str(result)
            prefix = f"{tool_name}:{summary}"[:160]
            if tool_name == "run_command" and isinstance(result, dict):
                prefix = (
                    f"run_command:{json.dumps(result.get('argv'), ensure_ascii=False)}"
                    f" exit_code={result.get('exit_code')}"
                )[:160]
            if prefix not in self.open_errors:
                self.open_errors.append(prefix)
                if len(self.open_errors) > MAX_OPEN_ERRORS:
                    self.open_errors = self.open_errors[-MAX_OPEN_ERRORS:]


def parse_envelope(content: str | None) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    try:
        envelope = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict) or "ok" not in envelope or "result" not in envelope:
        return None
    return envelope


def loads_if_json(text: Any) -> Any:
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def already_pruned(result: Any) -> bool:
    if isinstance(result, str):
        return result.startswith(PRUNED_PREFIX) or result.startswith(DUPLICATE_PREFIX)
    if isinstance(result, dict):
        return bool(result.get("pruned"))
    return False


def head(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n{PRUNED_PREFIX}: {len(text) - limit} more characters elided]"


def tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return (
        f"{PRUNED_PREFIX}: {len(text) - limit} earlier characters elided]\n"
        + text[-limit:]
    )


class ToolResultPruner:
    """Pure per-tool rewrite rules; each returns (new_result, rule) or None."""

    @staticmethod
    def prune(item: ContextItem) -> tuple[str, str] | None:
        if item.role != "tool" or item.content is None:
            return None
        if len(item.content) < MIN_PRUNE_CHARS:
            return None
        envelope = parse_envelope(item.content)
        if envelope is None:
            return None
        result = envelope["result"]
        if already_pruned(result):
            return None
        rewritten = ToolResultPruner.dispatch(item, result)
        if rewritten is None:
            return None
        new_result, rule = rewritten
        return (
            json.dumps(
                {"ok": envelope["ok"], "result": new_result},
                ensure_ascii=False,
            ),
            rule,
        )

    @staticmethod
    def dispatch(
        item: ContextItem,
        result: Any,
    ) -> tuple[Any, str] | None:
        tool_name = item.tool_name or ""
        if tool_name in COMMAND_OUTCOME_TOOLS:
            return ToolResultPruner.command_outcome(item, result, tool_name)
        if tool_name == "read_file":
            return ToolResultPruner.read_file(item, result)
        if tool_name == "list_files":
            return ToolResultPruner.list_files(item, result)
        if tool_name == "search_text":
            return ToolResultPruner.search_text(item, result)
        if tool_name == "fetch_url":
            return ToolResultPruner.fetch_url(item, result)
        return ToolResultPruner.generic(result, item)

    @staticmethod
    def command_outcome(
        item: ContextItem,
        result: Any,
        tool_name: str,
    ) -> tuple[Any, str] | None:
        outcome = result if isinstance(result, dict) else loads_if_json(result)
        if not isinstance(outcome, dict) or "exit_code" not in outcome:
            return None
        summary = {
            key: outcome[key]
            for key in ("argv", "cwd", "sandbox", "exit_code", "timed_out", "duration_ms")
            if key in outcome
        }
        stdout = str(outcome.get("stdout") or "")
        stderr = str(outcome.get("stderr") or "")
        failed = outcome.get("exit_code") != 0 or bool(outcome.get("timed_out"))
        if failed:
            summary["stdout"] = tail(stdout, KEEP_STDOUT_TAIL)
            summary["stderr"] = tail(stderr, KEEP_STDERR_TAIL)
        elif tool_name == "run_command":
            summary["stdout"] = f"{PRUNED_PREFIX}: {len(stdout)} characters elided]"
            summary["stderr"] = (
                f"{PRUNED_PREFIX}: {len(stderr)} characters elided]" if stderr else ""
            )
        else:
            summary["stdout"] = head(stdout, KEEP_GIT_STDOUT_HEAD)
            summary["stderr"] = stderr
        summary["pruned"] = True
        if item.source_event_id:
            summary["event_id"] = item.source_event_id
        return json.dumps(summary, ensure_ascii=False), f"command_outcome:{tool_name}"

    @staticmethod
    def read_file(item: ContextItem, result: Any) -> tuple[Any, str] | None:
        if not isinstance(result, str):
            return None
        arguments = item.arguments or {}
        path = arguments.get("path", "?")
        start_line = arguments.get("start_line", 1)
        end_line = arguments.get("end_line", "?")
        first_lines = result.splitlines()[:3]
        summary = "\n".join(
            (
                f"{PRUNED_PREFIX} read_file] path={path} lines={start_line}-{end_line}",
                f"{len(result)} characters elided; original event: "
                f"{item.source_event_id or 'unknown'}",
                "first lines:",
                *first_lines,
            )
        )
        return summary, "read_file"

    @staticmethod
    def list_files(item: ContextItem, result: Any) -> tuple[Any, str] | None:
        parsed = result if isinstance(result, dict) else loads_if_json(result)
        if not isinstance(parsed, dict) or "entries" not in parsed:
            return None
        summary: dict[str, Any] = {
            "pruned": True,
            "entries": len(parsed.get("entries") or []),
            "truncated": bool(parsed.get("truncated")),
        }
        if item.source_event_id:
            summary["event_id"] = item.source_event_id
        return json.dumps(summary, ensure_ascii=False), "list_files"

    @staticmethod
    def search_text(item: ContextItem, result: Any) -> tuple[Any, str] | None:
        parsed = result if isinstance(result, dict) else loads_if_json(result)
        if not isinstance(parsed, dict) or "matches" not in parsed:
            return None
        summary: dict[str, Any] = {
            key: parsed[key]
            for key in ("query", "path", "engine", "files_searched", "returned", "truncated")
            if key in parsed
        }
        summary["match_count"] = len(parsed.get("matches") or [])
        summary["pruned"] = True
        if item.source_event_id:
            summary["event_id"] = item.source_event_id
        return json.dumps(summary, ensure_ascii=False), "search_text"

    @staticmethod
    def fetch_url(item: ContextItem, result: Any) -> tuple[Any, str] | None:
        parsed = result if isinstance(result, dict) else loads_if_json(result)
        if not isinstance(parsed, dict) or "body" not in parsed:
            return None
        summary = {
            key: parsed[key]
            for key in ("url", "status", "content_type", "bytes")
            if key in parsed
        }
        summary["body"] = head(str(parsed.get("body") or ""), KEEP_TEXT_HEAD)
        summary["pruned"] = True
        if item.source_event_id:
            summary["event_id"] = item.source_event_id
        return json.dumps(summary, ensure_ascii=False), "fetch_url"

    @staticmethod
    def generic(result: Any, item: ContextItem) -> tuple[Any, str] | None:
        parsed = result if not isinstance(result, str) else loads_if_json(result)
        if isinstance(parsed, dict):
            truncated = {
                key: head(value, KEEP_TEXT_HEAD)
                if isinstance(value, str) and len(value) > KEEP_TEXT_HEAD
                else value
                for key, value in parsed.items()
            }
            if truncated == parsed:
                return None
            truncated["pruned"] = True
            if item.source_event_id:
                truncated["event_id"] = item.source_event_id
            return json.dumps(truncated, ensure_ascii=False), "json_truncate"
        if isinstance(result, str):
            return head(result, KEEP_TEXT_HEAD), "text_truncate"
        return None


def duplicate_content(item: ContextItem) -> str:
    envelope = parse_envelope(item.content)
    ok = bool(envelope["ok"]) if envelope else True
    note = (
        f"{DUPLICATE_PREFIX} {item.tool_name} result; superseded by a later "
        f"identical call; original event: {item.source_event_id or 'unknown'}]"
    )
    return json.dumps({"ok": ok, "result": note}, ensure_ascii=False)


class ContextManager:
    """Owns model messages as immutable items and projects chat-format dicts."""

    def __init__(self, system_prompt: str) -> None:
        self._system_prompt = system_prompt
        self._items: list[ContextItem] = [
            self._build_item("system", system_prompt, ContextLayer.PINNED)
        ]
        self.working_memory = WorkingMemory()

    @staticmethod
    def _build_item(
        role: str,
        content: str | None,
        layer: ContextLayer,
        **extra: Any,
    ) -> ContextItem:
        return ContextItem(
            item_id=str(uuid4()),
            role=role,
            content=content,
            layer=layer,
            tokens=TokenCounter.estimate(content),
            **extra,
        )

    def add_user(self, text: str) -> ContextItem:
        item = self._build_item("user", text, ContextLayer.RECENT)
        self._items.append(item)
        return item

    def add_assistant(
        self,
        text: str,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_details: list[dict[str, Any]] | None = None,
        reasoning: str | None = None,
    ) -> ContextItem:
        calls = tuple(deepcopy(tool_calls)) if tool_calls else None
        details = tuple(deepcopy(reasoning_details)) if reasoning_details else None
        tokens = TokenCounter.estimate(text)
        if calls:
            tokens += sum(
                TokenCounter.estimate(call["function"]["name"])
                + TokenCounter.estimate(call["function"]["arguments"])
                for call in calls
            )
        if details:
            tokens += TokenCounter.estimate(
                json.dumps(details, ensure_ascii=False, separators=(",", ":"))
            )
        elif reasoning:
            tokens += TokenCounter.estimate(reasoning)
        item = ContextItem(
            item_id=str(uuid4()),
            role="assistant",
            content=text or None,
            layer=ContextLayer.RECENT,
            tokens=tokens,
            tool_calls=calls,
            reasoning_details=details,
            reasoning=reasoning,
        )
        self._items.append(item)
        return item

    def add_tool(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
        content: str,
        source_event_id: str | None = None,
    ) -> ContextItem:
        args_key = json.dumps(
            {"name": tool_name, "arguments": arguments or {}},
            ensure_ascii=False,
            sort_keys=True,
        )
        item = ContextItem(
            item_id=str(uuid4()),
            role="tool",
            content=content,
            layer=ContextLayer.RECENT,
            tokens=TokenCounter.estimate(content),
            tool_name=tool_name,
            call_id=call_id,
            arguments=dict(arguments or {}),
            tool_args_key=args_key,
            source_event_id=source_event_id,
        )
        self._items.append(item)
        return item

    def messages(self) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        for item in self._items:
            if item.role == "tool":
                rendered.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.call_id,
                        "content": item.content,
                    }
                )
            elif item.role == "assistant":
                message: dict[str, Any] = {"role": "assistant", "content": item.content}
                if item.tool_calls:
                    message["tool_calls"] = [
                        {
                            "id": call["id"],
                            "type": call["type"],
                            "function": {
                                "name": call["function"]["name"],
                                "arguments": call["function"]["arguments"],
                            },
                        }
                        for call in item.tool_calls
                    ]
                if item.reasoning_details:
                    message["reasoning_details"] = deepcopy(
                        list(item.reasoning_details)
                    )
                elif item.reasoning:
                    message["reasoning"] = item.reasoning
                rendered.append(message)
            else:
                rendered.append({"role": item.role, "content": item.content})
        return rendered

    def summary_source(
        self,
    ) -> tuple[list[dict[str, Any]], tuple[str, ...], int, int]:
        """Return older complete turns eligible for second-stage summarization."""

        self._refresh_layers()
        indices = self._summary_candidate_indices()
        messages = self.messages()
        source_messages = [messages[index] for index in indices]
        event_ids = tuple(
            item.source_event_id
            for index, item in enumerate(self._items)
            if index in indices and item.source_event_id
        )
        source_tokens = sum(self._items[index].tokens for index in indices)
        return source_messages, event_ids, len(indices), source_tokens

    def apply_structured_summary(self, summary: dict[str, Any]) -> tuple[int, int, int]:
        """Atomically replace eligible older turns with one validated summary."""

        self._refresh_layers()
        indices = self._summary_candidate_indices()
        if not indices:
            return 0, self.estimated_tokens, self.estimated_tokens
        before = self.estimated_tokens
        encoded = SUMMARY_PREFIX + json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        summary_item = self._build_item(
            "system",
            encoded,
            ContextLayer.PINNED,
            tool_name="context_summary",
            pruned=True,
        )
        selected = set(indices)
        retained = [
            item for index, item in enumerate(self._items) if index not in selected
        ]
        retained.insert(1, summary_item)
        self._items = retained
        self.refresh_state()
        return len(indices), before, self.estimated_tokens

    @property
    def item_count(self) -> int:
        return len(self._items)

    @property
    def estimated_tokens(self) -> int:
        return sum(item.tokens for item in self._items)

    def layer_stats(self) -> dict[str, Any]:
        self._refresh_layers()
        layers = {
            layer.value: {"items": 0, "estimated_tokens": 0}
            for layer in ContextLayer
        }
        for item in self._items:
            stats = layers[item.layer.value]
            stats["items"] += 1
            stats["estimated_tokens"] += item.tokens
        return {
            "items": len(self._items),
            "estimated_tokens": self.estimated_tokens,
            "layers": layers,
        }

    def prune(self, *, trigger: str = "threshold") -> CompactionReport:
        before = self.estimated_tokens
        self.refresh_state()
        rules: dict[str, int] = {}
        pruned_event_ids: list[str] = []
        changed: dict[int, ContextItem] = {}

        candidates = [
            (index, item)
            for index, item in enumerate(self._items)
            if item.role == "tool"
            and item.layer is ContextLayer.EVIDENCE
            and not item.pruned
            and item.content is not None
            and len(item.content) >= MIN_PRUNE_CHARS
        ]

        latest_by_key: dict[str, int] = {}
        for index, item in candidates:
            if item.tool_args_key:
                latest_by_key[item.tool_args_key] = index
        for index, item in candidates:
            if item.tool_args_key and latest_by_key[item.tool_args_key] != index:
                content = duplicate_content(item)
                changed[index] = replace(
                    item,
                    content=content,
                    tokens=TokenCounter.estimate(content),
                    pruned=True,
                )
                rules["duplicate"] = rules.get("duplicate", 0) + 1
                if item.source_event_id:
                    pruned_event_ids.append(item.source_event_id)

        for index, item in candidates:
            if index in changed:
                continue
            rewritten = ToolResultPruner.prune(item)
            if rewritten is None:
                continue
            content, rule = rewritten
            changed[index] = replace(
                item,
                content=content,
                tokens=TokenCounter.estimate(content),
                pruned=True,
            )
            rules[rule] = rules.get(rule, 0) + 1
            if item.source_event_id:
                pruned_event_ids.append(item.source_event_id)

        for index, new_item in changed.items():
            self._items[index] = new_item

        return CompactionReport(
            trigger=trigger,
            changed=bool(changed),
            before_tokens=before,
            after_tokens=self.estimated_tokens,
            items_pruned=len(changed),
            rules=rules,
            pruned_event_ids=tuple(pruned_event_ids),
        )

    def clear(self) -> None:
        self._items = [
            self._build_item("system", self._system_prompt, ContextLayer.PINNED)
        ]
        self.working_memory = WorkingMemory()

    def refresh_state(self) -> None:
        self._refresh_layers()
        self._collect_working_memory()

    def _refresh_layers(self) -> None:
        last_user_index = None
        for index, item in enumerate(self._items):
            if item.role == "user":
                last_user_index = index
        for index, item in enumerate(self._items):
            if item.role == "system":
                layer = ContextLayer.PINNED
            elif last_user_index is not None and index >= last_user_index:
                layer = ContextLayer.RECENT
            else:
                layer = ContextLayer.EVIDENCE
            if item.layer is not layer:
                self._items[index] = replace(item, layer=layer)

    def _summary_candidate_indices(self) -> list[int]:
        return [
            index
            for index, item in enumerate(self._items)
            if index > 0
            and (
                item.layer is ContextLayer.EVIDENCE
                or item.tool_name == "context_summary"
            )
        ]

    def _collect_working_memory(self) -> None:
        memory = WorkingMemory()
        for item in self._items:
            if item.role != "tool":
                continue
            envelope = parse_envelope(item.content)
            if envelope is None:
                continue
            result = envelope["result"]
            parsed = result if not isinstance(result, str) else loads_if_json(result)
            memory.record(
                item.tool_name or "",
                bool(envelope.get("ok")),
                parsed,
            )
        self.working_memory = memory

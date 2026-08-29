"""Structured, locally validated summaries for second-stage compaction."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Protocol


SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "version",
        "objective",
        "completed",
        "decisions",
        "files",
        "identifiers",
        "commands",
        "exit_codes",
        "open_errors",
        "next_actions",
        "event_ids",
    ],
    "properties": {
        "version": {"type": "integer", "enum": [1]},
        "objective": {"type": "string"},
        "completed": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string"},
        },
        "decisions": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string"},
        },
        "files": {
            "type": "array",
            "maxItems": 30,
            "items": {"type": "string"},
        },
        "identifiers": {
            "type": "array",
            "maxItems": 40,
            "items": {"type": "string"},
        },
        "commands": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string"},
        },
        "exit_codes": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "integer"},
        },
        "open_errors": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string"},
        },
        "next_actions": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string"},
        },
        "event_ids": {
            "type": "array",
            "maxItems": 40,
            "items": {"type": "string"},
        },
    },
}


SUMMARY_SYSTEM_PROMPT = """Summarize an older coding-agent context as JSON.
Return exactly the supplied JSON Schema. Preserve the user's objective, completed
work, decisions, file paths and identifiers, verified commands with exit codes,
unresolved errors, and next actions. Never invent a fact. Copy every path,
identifier, number, exit code, and event_id exactly from the source. event_ids must
refer only to source tool-result events. Put command argv in commands as compact
JSON strings copied from the source. Prefer omission over uncertainty.
"""


class ContextSummarizer(Protocol):
    def summarize_context(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> dict[str, Any]: ...


class SummaryValidationError(ValueError):
    pass


_PATH = re.compile(
    r"(?<![\w.-])(?:[A-Za-z]:[\\/])?(?:[\w.-]+[\\/])+[\w.-]+|"
    r"(?<![\w.-])[\w.-]+\.[A-Za-z0-9]{1,12}(?![\w.-])"
)
_NUMBER = re.compile(r"(?<![\w.-])-?\d+(?:\.\d+)?(?![\w.-])")


def validate_summary(
    summary: Any,
    source_messages: Sequence[dict[str, Any]],
    source_event_ids: Sequence[str],
) -> dict[str, Any]:
    """Validate shape and reject protected facts absent from source evidence."""

    if not isinstance(summary, dict):
        raise SummaryValidationError("summary must be a JSON object")
    required = set(SUMMARY_SCHEMA["required"])
    if set(summary) != required:
        raise SummaryValidationError("summary fields do not match the fixed schema")
    if (
        not isinstance(summary.get("version"), int)
        or isinstance(summary.get("version"), bool)
        or summary["version"] != 1
    ):
        raise SummaryValidationError("summary version must be 1")

    for name in ("objective",):
        _require_string(summary.get(name), name, max_length=800)
    for name, limit in (
        ("completed", 20),
        ("decisions", 12),
        ("files", 30),
        ("identifiers", 40),
        ("commands", 20),
        ("exit_codes", 20),
        ("open_errors", 12),
        ("next_actions", 12),
        ("event_ids", 40),
    ):
        value = summary.get(name)
        if not isinstance(value, list) or len(value) > limit:
            raise SummaryValidationError(f"{name} must be an array of at most {limit}")

    source_text = json.dumps(list(source_messages), ensure_ascii=False, sort_keys=True)
    _validate_protected_text(summary["objective"], source_text, "objective")
    for section in ("completed", "decisions", "open_errors"):
        for index, item in enumerate(summary[section]):
            _require_string(item, f"{section}[{index}]", max_length=800)
            _validate_protected_text(item, source_text, f"{section}[{index}]")
    for index, action in enumerate(summary["next_actions"]):
        _require_string(action, f"next_actions[{index}]", max_length=500)
        _validate_protected_text(action, source_text, f"next_actions[{index}]")

    for index, path in enumerate(summary["files"]):
        _require_grounded_string(path, source_text, f"files[{index}]", 300)
    for index, identifier in enumerate(summary["identifiers"]):
        _require_grounded_string(
            identifier, source_text, f"identifiers[{index}]", 120
        )
    for index, command in enumerate(summary["commands"]):
        _require_string(command, f"commands[{index}]", max_length=1_000)
        try:
            argv = json.loads(command)
        except json.JSONDecodeError as error:
            raise SummaryValidationError(
                f"commands[{index}] must be a JSON argv array"
            ) from error
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(argument, str) for argument in argv)
            or any(argument not in source_text for argument in argv)
        ):
            raise SummaryValidationError(f"ungrounded argv in commands[{index}]")
        _validate_protected_text(command, source_text, f"commands[{index}]")
    for index, exit_code in enumerate(summary["exit_codes"]):
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise SummaryValidationError(f"exit_codes[{index}] must be an integer")
        if str(exit_code) not in source_text:
            raise SummaryValidationError(f"ungrounded exit code at index {index}")
    _validate_event_ids(summary["event_ids"], set(source_event_ids), "summary")
    return summary


def _require_grounded_string(
    value: Any,
    source: str,
    location: str,
    max_length: int,
) -> None:
    _require_string(value, location, max_length=max_length)
    if value not in source:
        raise SummaryValidationError(f"ungrounded value in {location}")


def _validate_event_ids(value: Any, allowed: set[str], location: str) -> None:
    if not isinstance(value, list) or len(value) > 20:
        raise SummaryValidationError(f"{location}.event_ids is invalid")
    for event_id in value:
        if not isinstance(event_id, str) or event_id not in allowed:
            raise SummaryValidationError(f"ungrounded event_id in {location}")


def _validate_protected_text(value: str, source: str, location: str) -> None:
    for token in [*_PATH.findall(value), *_NUMBER.findall(value)]:
        if token not in source:
            raise SummaryValidationError(f"ungrounded protected fact in {location}")


def _require_string(
    value: Any,
    location: str,
    *,
    max_length: int | None = None,
) -> None:
    if not isinstance(value, str):
        raise SummaryValidationError(f"{location} must be a string")
    if max_length is not None and len(value) > max_length:
        raise SummaryValidationError(
            f"{location} exceeds the {max_length} character limit"
        )

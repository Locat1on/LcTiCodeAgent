"""Minimal local read-only tools for the first live agent loop."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "sessions",
    "tmp",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
SENSITIVE_NAMES = {
    "id_rsa",
    "id_ed25519",
    "credentials",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
SENSITIVE_DIRECTORIES = {".aws", ".ssh"}


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    is_error: bool = False

    def as_message_content(self) -> str:
        return json.dumps(
            {"ok": not self.is_error, "result": self.content},
            ensure_ascii=False,
        )


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self._definitions = {
            definition.name: definition
            for definition in (
                ToolDefinition(
                    name="list_files",
                    description=(
                        "List files and directories inside the workspace. "
                        "Use a relative path and a depth from 1 to 4."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "default": "."},
                            "depth": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 4,
                                "default": 2,
                            },
                        },
                        "additionalProperties": False,
                    },
                    handler=self._list_files,
                ),
                ToolDefinition(
                    name="read_file",
                    description=(
                        "Read up to 200 numbered lines from a UTF-8 text file "
                        "inside the workspace. Sensitive credential files are denied."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {
                                "type": "integer",
                                "minimum": 1,
                                "default": 1,
                            },
                            "end_line": {
                                "type": "integer",
                                "minimum": 1,
                                "default": 200,
                            },
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                    handler=self._read_file,
                ),
            )
        }

    def schemas(self) -> list[dict[str, Any]]:
        return [definition.schema() for definition in self._definitions.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        definition = self._definitions.get(name)
        if definition is None:
            return ToolResult(f"unknown tool: {name}", is_error=True)
        try:
            return definition.handler(arguments)
        except (OSError, TypeError, ValueError) as error:
            return ToolResult(str(error), is_error=True)

    def _resolve(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("path must be a non-empty string")
        candidate = (self.workspace / relative_path).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as error:
            raise ValueError("path resolves outside the workspace") from error
        return candidate

    def _list_files(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(arguments.get("path", "."))
        depth = arguments.get("depth", 2)
        if not isinstance(depth, int) or not 1 <= depth <= 4:
            raise ValueError("depth must be an integer from 1 to 4")
        if not path.is_dir():
            raise ValueError("path is not a directory")

        entries: list[dict[str, Any]] = []
        for current, directories, files in os.walk(path):
            current_path = Path(current)
            current_depth = len(current_path.relative_to(path).parts)
            directories[:] = sorted(
                name
                for name in directories
                if name not in IGNORED_DIRECTORIES and not name.endswith(".egg-info")
            )
            if current_depth >= depth:
                directories.clear()
                continue
            candidates = [
                *((current_path / name, "directory") for name in directories),
                *((current_path / name, "file") for name in sorted(files)),
            ]
            for candidate, entry_type in candidates:
                entries.append(
                    {
                        "path": candidate.relative_to(self.workspace).as_posix(),
                        "type": entry_type,
                    }
                )
                if len(entries) >= 200:
                    break
            if len(entries) >= 200:
                break
        return ToolResult(
            json.dumps(
                {"entries": entries, "truncated": len(entries) >= 200},
                ensure_ascii=False,
            )
        )

    def _read_file(self, arguments: dict[str, Any]) -> ToolResult:
        path_value = arguments.get("path")
        path = self._resolve(path_value)
        if self._is_sensitive(path):
            raise ValueError("reading sensitive credential files is denied")
        if not path.is_file():
            raise ValueError("path is not a file")
        if path.stat().st_size > 1_000_000:
            raise ValueError("file is larger than the 1 MB read limit")

        start_line = arguments.get("start_line", 1)
        end_line = arguments.get("end_line", start_line + 199)
        if not isinstance(start_line, int) or not isinstance(end_line, int):
            raise ValueError("start_line and end_line must be integers")
        if start_line < 1 or end_line < start_line:
            raise ValueError("invalid line range")
        if end_line - start_line + 1 > 200:
            raise ValueError("a single read may contain at most 200 lines")

        text = path.read_text(encoding="utf-8")
        if "\x00" in text:
            raise ValueError("binary files are not supported")
        lines = text.splitlines()
        selected = lines[start_line - 1 : end_line]
        numbered = "\n".join(
            f"{number}: {line}"
            for number, line in enumerate(selected, start=start_line)
        )
        if len(numbered) > 32_000:
            numbered = numbered[:32_000] + "\n[output truncated at 32000 characters]"
        return ToolResult(numbered)

    def _is_sensitive(self, path: Path) -> bool:
        lower_name = path.name.lower()
        relative_parts = {
            part.lower() for part in path.relative_to(self.workspace).parts
        }
        return (
            lower_name == ".env"
            or (lower_name.startswith(".env.") and lower_name != ".env.example")
            or lower_name in SENSITIVE_NAMES
            or path.suffix.lower() in SENSITIVE_SUFFIXES
            or bool(relative_parts & SENSITIVE_DIRECTORIES)
        )

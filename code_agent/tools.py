"""Minimal local read-only tools for the first live agent loop."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .command import CommandExecutor, CommandRunner
from .git_tools import GitInspector
from .network import UrlFetcher
from .search import TextSearcher


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
    def __init__(
        self,
        workspace: Path,
        *,
        command_runner: CommandExecutor | None = None,
        url_fetcher: UrlFetcher | None = None,
        git_inspector: GitInspector | None = None,
        searcher: TextSearcher | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.command_runner = command_runner or CommandRunner()
        self.url_fetcher = url_fetcher or UrlFetcher()
        self.git_inspector = git_inspector or GitInspector(self.workspace)
        self._searcher = searcher or TextSearcher(self.workspace)
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
                ToolDefinition(
                    name="search_text",
                    description=(
                        "Search UTF-8 text files under a workspace directory for a "
                        "case-sensitive regular expression. Returns path, line "
                        "number, and snippet matches. Hidden and dependency "
                        "directories are excluded."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1000,
                            },
                            "path": {"type": "string", "default": "."},
                            "max_results": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 200,
                                "default": 50,
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    handler=self._search_text,
                ),
                ToolDefinition(
                    name="replace_in_file",
                    description=(
                        "Replace exactly one occurrence of old_text in an existing "
                        "UTF-8 file inside the workspace. Read the file first."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                        },
                        "required": ["path", "old_text", "new_text"],
                        "additionalProperties": False,
                    },
                    handler=self._replace_in_file,
                ),
                ToolDefinition(
                    name="write_file",
                    description=(
                        "Create a new UTF-8 text file inside the workspace. "
                        "This tool never overwrites an existing file."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                    handler=self._write_file,
                ),
                ToolDefinition(
                    name="run_command",
                    description=(
                        "Run a verification command locally without a shell. Pass argv "
                        "as an array. Only test, lint, typecheck, build, and compile "
                        "commands are allowed; network and install commands are denied."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "argv": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 32,
                            },
                            "cwd": {"type": "string", "default": "."},
                            "timeout_seconds": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 120,
                                "default": 60,
                            },
                        },
                        "required": ["argv"],
                        "additionalProperties": False,
                    },
                    handler=self._run_command,
                ),
                ToolDefinition(
                    name="fetch_url",
                    description=(
                        "Fetch public text over HTTPS after user approval. Only GET "
                        "and HEAD are supported; private addresses and URL credentials "
                        "are denied."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "method": {
                                "type": "string",
                                "enum": ["GET", "HEAD"],
                                "default": "GET",
                            },
                            "max_bytes": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 1_000_000,
                                "default": 200_000,
                            },
                        },
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                    handler=self._fetch_url,
                ),
                ToolDefinition(
                    name="git_status",
                    description="Show the current branch and concise working tree status.",
                    parameters={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    handler=self._git_status,
                ),
                ToolDefinition(
                    name="git_diff",
                    description=(
                        "Show a read-only Git diff for the whole workspace or one "
                        "workspace-relative path."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "staged": {"type": "boolean", "default": False},
                            "path": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                    handler=self._git_diff,
                ),
                ToolDefinition(
                    name="git_log",
                    description="Show up to 20 recent commits without changing the repository.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "max_count": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                                "default": 10,
                            }
                        },
                        "additionalProperties": False,
                    },
                    handler=self._git_log,
                ),
                ToolDefinition(
                    name="git_commit",
                    description=(
                        "Create one local Git commit containing only explicitly listed "
                        "workspace files after preflight and user approval."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 50,
                            },
                            "message": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 200,
                            },
                        },
                        "required": ["files", "message"],
                        "additionalProperties": False,
                    },
                    handler=self._git_commit,
                ),
                ToolDefinition(
                    name="git_push",
                    description=(
                        "Push current HEAD to an existing same-name remote branch "
                        "after preflight and one-time user approval. Force, delete, "
                        "mirror, tags, and arbitrary refspecs are not supported."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "remote": {"type": "string", "default": "origin"},
                            "branch": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                    handler=self._git_push,
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
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            return ToolResult(str(error), is_error=True)

    def approval_context(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "fetch_url":
            return {
                "operation": "fetch_url",
                "url": arguments.get("url"),
                "method": arguments.get("method", "GET"),
                "max_bytes": arguments.get("max_bytes", 200_000),
            }
        if name == "git_commit":
            return self.git_inspector.commit_preflight(
                files=arguments.get("files"),
                message=arguments.get("message"),
            )
        if name == "git_push":
            return self.git_inspector.push_preflight(
                remote=arguments.get("remote", "origin"),
                branch=arguments.get("branch"),
            )
        return {"operation": name, "arguments": arguments}

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

        text = self._read_text(path)
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

    def _search_text(self, arguments: dict[str, Any]) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if len(query) > 1000:
            raise ValueError("query exceeds the 1000 character limit")
        max_results = arguments.get("max_results", 50)
        if not isinstance(max_results, int) or not 1 <= max_results <= 200:
            raise ValueError("max_results must be an integer from 1 to 200")
        result = self._searcher.search(
            query,
            relative_path=arguments.get("path", "."),
            max_results=max_results,
        )
        result["matches"] = [
            match
            for match in result["matches"]
            if not self._is_sensitive(self.workspace / match["path"])
        ]
        result["returned"] = len(result["matches"])
        payload = json.dumps(result, ensure_ascii=False)
        while result["matches"] and len(payload) > 24_000:
            result["matches"].pop()
            result["truncated"] = True
            result["returned"] = len(result["matches"])
            payload = json.dumps(result, ensure_ascii=False)
        return ToolResult(payload)

    def _replace_in_file(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(arguments.get("path"))
        if self._is_sensitive(path):
            raise ValueError("writing sensitive credential files is denied")
        if not path.is_file():
            raise ValueError("path is not an existing file")
        if path.stat().st_size > 1_000_000:
            raise ValueError("file is larger than the 1 MB edit limit")

        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            raise ValueError("old_text must be a non-empty string")
        if not isinstance(new_text, str):
            raise ValueError("new_text must be a string")
        if len(new_text) > 200_000:
            raise ValueError("new_text exceeds the 200000 character limit")

        original = self._read_text(path)
        occurrences = original.count(old_text)
        if occurrences != 1:
            raise ValueError(
                f"old_text must match exactly once; found {occurrences} occurrences"
            )
        updated = original.replace(old_text, new_text, 1)
        if updated == original:
            raise ValueError("replacement would not change the file")
        self._write_text_atomic(path, updated)
        return ToolResult(
            json.dumps(
                {
                    "path": path.relative_to(self.workspace).as_posix(),
                    "old_sha256": self._sha256(original),
                    "new_sha256": self._sha256(updated),
                    "changed": True,
                },
                ensure_ascii=False,
            )
        )

    def _write_file(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve(arguments.get("path"))
        if self._is_sensitive(path):
            raise ValueError("writing sensitive credential files is denied")
        if path.exists():
            raise ValueError("write_file refuses to overwrite an existing path")
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        if len(content) > 200_000:
            raise ValueError("content exceeds the 200000 character limit")

        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_text_atomic(path, content)
        return ToolResult(
            json.dumps(
                {
                    "path": path.relative_to(self.workspace).as_posix(),
                    "sha256": self._sha256(content),
                    "created": True,
                },
                ensure_ascii=False,
            )
        )

    def _run_command(self, arguments: dict[str, Any]) -> ToolResult:
        argv = arguments.get("argv")
        cwd = self._resolve(arguments.get("cwd", "."))
        timeout_seconds = arguments.get("timeout_seconds", 60)
        if not cwd.is_dir():
            raise ValueError("cwd is not a directory")
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be an integer from 1 to 120")

        outcome = self.command_runner.run(argv, cwd, timeout_seconds)
        failed = outcome["timed_out"] or outcome["exit_code"] != 0
        return ToolResult(
            json.dumps(outcome, ensure_ascii=False),
            is_error=failed,
        )

    def _fetch_url(self, arguments: dict[str, Any]) -> ToolResult:
        result = self.url_fetcher.fetch(
            arguments.get("url"),
            method=arguments.get("method", "GET"),
            max_bytes=arguments.get("max_bytes", 200_000),
        )
        return ToolResult(json.dumps(result, ensure_ascii=False))

    def _git_status(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            json.dumps(self.git_inspector.status(), ensure_ascii=False)
        )

    def _git_diff(self, arguments: dict[str, Any]) -> ToolResult:
        staged = arguments.get("staged", False)
        if not isinstance(staged, bool):
            raise ValueError("staged must be a boolean")
        path = arguments.get("path")
        if path is not None and not isinstance(path, str):
            raise ValueError("path must be a string")
        return ToolResult(
            json.dumps(
                self.git_inspector.diff(staged=staged, path=path),
                ensure_ascii=False,
            )
        )

    def _git_log(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            json.dumps(
                self.git_inspector.log(max_count=arguments.get("max_count", 10)),
                ensure_ascii=False,
            )
        )

    def _git_commit(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            json.dumps(
                self.git_inspector.commit(
                    files=arguments.get("files"),
                    message=arguments.get("message"),
                ),
                ensure_ascii=False,
            )
        )

    def _git_push(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            json.dumps(
                self.git_inspector.push(
                    remote=arguments.get("remote", "origin"),
                    branch=arguments.get("branch"),
                ),
                ensure_ascii=False,
            )
        )

    @staticmethod
    def _read_text(path: Path) -> str:
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()

    @staticmethod
    def _write_text_atomic(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sha256(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

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

"""Workspace text search using ripgrep with a pure-Python fallback."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .command import CommandRunner


class SearchError(ValueError):
    pass


MAX_SNIPPET_CHARS = 200
MAX_FILE_BYTES = 1_000_000
MAX_STDOUT_CHARS = 5_000_000
RG_TIMEOUT_SECONDS = 15
EXCLUDED_DIRECTORY_NAMES = {"node_modules", "tmp", "sessions", "__pycache__"}


def _is_excluded_name(name: str) -> bool:
    return (
        name.startswith(".")
        or name in EXCLUDED_DIRECTORY_NAMES
        or name.endswith(".egg-info")
    )


def _normalize_path(text: str) -> str:
    path = text.replace("\\", "/")
    return path[2:] if path.startswith("./") else path


def _snippet(text: str) -> str:
    return text.rstrip("\r\n")[:MAX_SNIPPET_CHARS]


class TextSearcher:
    """Case-sensitive regular-expression search over workspace text files.

    Hidden files and dependency directories are skipped in both engines, and
    binary or oversized files never produce matches.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.workspace = workspace.resolve()
        self._process_runner = process_runner

    def search(
        self,
        query: str,
        *,
        relative_path: str = ".",
        max_results: int = 50,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise SearchError("query must be a non-empty string")
        if len(query) > 1000:
            raise SearchError("query exceeds the 1000 character limit")
        if not isinstance(max_results, int) or not 1 <= max_results <= 200:
            raise SearchError("max_results must be an integer from 1 to 200")
        root = self._resolve_root(relative_path)
        try:
            return self._search_ripgrep(query, root, max_results)
        except FileNotFoundError:
            return self._search_python(query, root, max_results)

    def _resolve_root(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise SearchError("path must be a non-empty string")
        candidate = (self.workspace / relative_path).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as error:
            raise SearchError("path resolves outside the workspace") from error
        if not candidate.is_dir():
            raise SearchError("path is not a directory")
        if any(_is_excluded_name(part) for part in candidate.relative_to(self.workspace).parts):
            raise SearchError("path is inside an excluded directory")
        return candidate

    def _search_ripgrep(
        self,
        query: str,
        root: Path,
        max_results: int,
    ) -> dict[str, Any]:
        rel_root = root.relative_to(self.workspace).as_posix()
        argv = [
            "rg",
            "--json",
            "--no-ignore",
            "--max-filesize",
            "1M",
            "--glob",
            "!**/{node_modules,tmp,sessions,__pycache__}/**",
            "--glob",
            "!**/*.egg-info/**",
            "-e",
            query,
            rel_root,
        ]
        try:
            completed = self._process_runner(
                argv,
                cwd=self.workspace,
                env=CommandRunner.safe_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=RG_TIMEOUT_SECONDS,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SearchError("text search timed out after 15 seconds") from error
        if completed.returncode not in (0, 1):
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise SearchError(f"ripgrep failed: {detail[:500]}")
        stdout = completed.stdout or ""
        if len(stdout) > MAX_STDOUT_CHARS:
            raise SearchError("search output exceeds the safety limit")
        matches: list[dict[str, Any]] = []
        total_matches = 0
        files_searched = 0
        for line in stdout.splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise SearchError("ripgrep produced unreadable output") from error
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            data = event.get("data") or {}
            if not isinstance(data, dict):
                continue
            if kind == "summary":
                stats = data.get("stats") or {}
                if isinstance(stats, dict):
                    files_searched = int(stats.get("searches") or 0)
            elif kind == "match":
                total_matches += 1
                if len(matches) >= max_results:
                    continue
                path_text = (data.get("path") or {}).get("text")
                line_number = data.get("line_number")
                lines_text = (data.get("lines") or {}).get("text")
                if (
                    isinstance(path_text, str)
                    and isinstance(line_number, int)
                    and isinstance(lines_text, str)
                ):
                    matches.append(
                        {
                            "path": _normalize_path(path_text),
                            "line": line_number,
                            "snippet": _snippet(lines_text),
                        }
                    )
        matches.sort(key=lambda match: (match["path"], match["line"]))
        return {
            "query": query,
            "path": rel_root,
            "engine": "ripgrep",
            "files_searched": files_searched,
            "returned": len(matches),
            "truncated": total_matches > len(matches),
            "matches": matches,
        }

    def _search_python(
        self,
        query: str,
        root: Path,
        max_results: int,
    ) -> dict[str, Any]:
        try:
            pattern = re.compile(query)
        except re.error as error:
            raise SearchError(f"invalid regular expression: {error}") from error
        matches: list[dict[str, Any]] = []
        files_searched = 0
        truncated = False
        for current, directories, files in os.walk(root, followlinks=False):
            if truncated:
                break
            directories[:] = sorted(
                name for name in directories if not _is_excluded_name(name)
            )
            for name in sorted(files):
                if truncated:
                    break
                if _is_excluded_name(name):
                    continue
                path = Path(current) / name
                if path.is_symlink():
                    continue
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    raw = path.read_bytes()
                except OSError:
                    continue
                if b"\x00" in raw[:1024]:
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                files_searched += 1
                for number, line in enumerate(text.splitlines(), start=1):
                    if pattern.search(line):
                        matches.append(
                            {
                                "path": path.relative_to(self.workspace).as_posix(),
                                "line": number,
                                "snippet": line[:MAX_SNIPPET_CHARS],
                            }
                        )
                        if len(matches) > max_results:
                            truncated = True
                            break
        return {
            "query": query,
            "path": root.relative_to(self.workspace).as_posix(),
            "engine": "python",
            "files_searched": files_searched,
            "returned": min(len(matches), max_results),
            "truncated": truncated,
            "matches": matches[:max_results],
        }

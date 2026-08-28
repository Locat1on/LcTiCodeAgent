"""Fixed read-only Git operations for repository inspection."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .command import CommandRunner


class GitToolError(ValueError):
    pass


class GitInspector:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def status(self) -> dict[str, Any]:
        return self._run(["status", "--short", "--branch"])

    def diff(self, *, staged: bool = False, path: str | None = None) -> dict[str, Any]:
        argv = ["diff", "--no-ext-diff"]
        if staged:
            argv.append("--cached")
        if path is not None:
            resolved = self._resolve_path(path)
            argv.extend(["--", resolved.relative_to(self.workspace).as_posix()])
        return self._run(argv)

    def log(self, *, max_count: int = 10) -> dict[str, Any]:
        if not isinstance(max_count, int) or not 1 <= max_count <= 20:
            raise GitToolError("max_count must be between 1 and 20")
        return self._run(
            ["log", "--oneline", "--decorate", f"--max-count={max_count}"]
        )

    def _resolve_path(self, value: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise GitToolError("path must be a non-empty string")
        resolved = (self.workspace / value).resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as error:
            raise GitToolError("Git path resolves outside the workspace") from error
        return resolved

    def _run(self, arguments: list[str]) -> dict[str, Any]:
        environment = CommandRunner.safe_environment()
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=self.workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                shell=False,
                check=False,
            )
        except FileNotFoundError as error:
            raise GitToolError("git executable was not found") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise GitToolError(f"Git command failed: {detail[:500]}")
        return {
            "argv": ["git", *arguments],
            "exit_code": completed.returncode,
            "stdout": CommandRunner.truncate(completed.stdout, 32_000),
            "stderr": CommandRunner.truncate(completed.stderr, 4_000),
        }


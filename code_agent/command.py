"""Restricted local command execution without a shell."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PYTHON_NAMES = {"python", "python.exe", "python3", "python3.exe"}
PYTHON_MODULES = {"compileall", "pytest", "unittest"}
PACKAGE_COMMANDS = {
    "npm": {"test", "run"},
    "npm.cmd": {"test", "run"},
    "pnpm": {"test", "run"},
    "pnpm.cmd": {"test", "run"},
    "yarn": {"test", "run"},
    "yarn.cmd": {"test", "run"},
    "cargo": {"check", "test"},
    "cargo.exe": {"check", "test"},
    "go": {"test"},
    "go.exe": {"test"},
    "dotnet": {"test"},
    "dotnet.exe": {"test"},
}
PACKAGE_SCRIPTS = {"build", "check", "lint", "test", "typecheck"}


class CommandPolicyError(ValueError):
    pass


class CommandRunner:
    def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        normalized = self._validate_and_normalize(argv)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                normalized,
                cwd=cwd,
                env=self._safe_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return {
                "argv": normalized,
                "cwd": str(cwd),
                "exit_code": None,
                "timed_out": True,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "stdout": self._truncate(self._coerce_output(error.stdout)),
                "stderr": self._truncate(self._coerce_output(error.stderr)),
            }
        except FileNotFoundError as error:
            raise CommandPolicyError(f"command executable was not found: {argv[0]}") from error

        return {
            "argv": normalized,
            "cwd": str(cwd),
            "exit_code": completed.returncode,
            "timed_out": False,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stdout": self._truncate(completed.stdout),
            "stderr": self._truncate(completed.stderr),
        }

    @staticmethod
    def _validate_and_normalize(argv: list[str]) -> list[str]:
        if not isinstance(argv, list) or not 1 <= len(argv) <= 32:
            raise CommandPolicyError("argv must contain between 1 and 32 arguments")
        if not all(isinstance(item, str) and item for item in argv):
            raise CommandPolicyError("every argv item must be a non-empty string")
        if sum(len(item) for item in argv) > 8_192:
            raise CommandPolicyError("command arguments exceed the length limit")

        executable = Path(argv[0]).name.lower()
        if executable in PYTHON_NAMES:
            if len(argv) < 3 or argv[1] != "-m" or argv[2] not in PYTHON_MODULES:
                raise CommandPolicyError(
                    "Python commands are limited to -m compileall, pytest, or unittest"
                )
            return [sys.executable, *argv[1:]]
        if executable in {"pytest", "pytest.exe"}:
            return [sys.executable, "-m", "pytest", *argv[1:]]

        allowed_actions = PACKAGE_COMMANDS.get(executable)
        if allowed_actions is None or len(argv) < 2 or argv[1] not in allowed_actions:
            raise CommandPolicyError("command is not in the verification allowlist")
        if argv[1] == "run":
            if len(argv) < 3 or argv[2] not in PACKAGE_SCRIPTS:
                raise CommandPolicyError("package scripts are limited to verification tasks")
        return argv.copy()

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        allowed = {
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "VIRTUAL_ENV",
            "WINDIR",
        }
        environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        return environment

    @staticmethod
    def _truncate(text: str, limit: int = 16_000) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + "\n[output truncated at 16000 characters]"

    @staticmethod
    def _coerce_output(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

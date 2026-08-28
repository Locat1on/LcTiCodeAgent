"""Fixed read-only Git operations for repository inspection."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .command import CommandRunner


class GitToolError(ValueError):
    pass


SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "openrouter_key": re.compile(r"sk-or-v1-[A-Za-z0-9_-]{20,}"),
    "openai_key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
}


class GitInspector:
    def __init__(
        self,
        workspace: Path,
        *,
        process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.workspace = workspace.resolve()
        self._process_runner = process_runner

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

    def commit_preflight(
        self,
        *,
        files: list[str],
        message: str,
    ) -> dict[str, Any]:
        selected = self._validate_files(files)
        self._validate_commit_message(message)
        relative_files = [path.relative_to(self.workspace).as_posix() for path in selected]
        status = self._run_text(["status", "--porcelain=v1", "--", *relative_files])
        if not status.strip():
            raise GitToolError("selected files contain no changes")
        findings = self._scan_worktree_files(selected)
        if findings:
            raise GitToolError(
                "secret scan blocked commit: "
                + ", ".join(f"{item['path']} ({item['kind']})" for item in findings)
            )
        head = self._head_or_unborn()
        file_hashes = {
            path.relative_to(self.workspace).as_posix(): self._file_hash(path)
            for path in selected
        }
        diff_stat = self._run_text(["diff", "--no-ext-diff", "--stat", "--", *relative_files])
        staged_files = [
            line
            for line in self._run_text(["diff", "--cached", "--name-only"]).splitlines()
            if line
        ]
        context = {
            "operation": "git_commit",
            "head": head,
            "message": message,
            "files": relative_files,
            "file_hashes": file_hashes,
            "status": status,
            "diff_stat": diff_stat,
            "unrelated_staged_files": sorted(set(staged_files) - set(relative_files)),
            "secret_scan": "passed",
        }
        context["state_token"] = self._state_token(context)
        return context

    def commit(self, *, files: list[str], message: str) -> dict[str, Any]:
        selected = self._validate_files(files)
        self._validate_commit_message(message)
        relative_files = [path.relative_to(self.workspace).as_posix() for path in selected]
        self._run_text(["add", "--", *relative_files])
        output = self._run_text(
            ["commit", "--only", "-m", message, "--", *relative_files]
        )
        return {
            "head": self._run_text(["rev-parse", "HEAD"]).strip(),
            "files": relative_files,
            "message": message,
            "stdout": CommandRunner.truncate(output, 8_000),
        }

    def push_preflight(
        self,
        *,
        remote: str = "origin",
        branch: str | None = None,
    ) -> dict[str, Any]:
        self._validate_remote(remote)
        current_branch = self._run_text(["branch", "--show-current"]).strip()
        target_branch = branch or current_branch
        if not target_branch:
            raise GitToolError("cannot push from a detached HEAD")
        self._validate_branch(target_branch)
        if branch is not None and branch != current_branch:
            raise GitToolError("git_push only supports the current branch")

        remote_url = self._run_text(["remote", "get-url", "--push", remote]).strip()
        self._validate_remote_url(remote_url)
        remote_ref = f"refs/remotes/{remote}/{target_branch}"
        remote_head = self._run_text(["rev-parse", "--verify", remote_ref]).strip()
        head = self._run_text(["rev-parse", "HEAD"]).strip()
        revision_range = f"{remote_ref}..HEAD"
        commit_count = int(self._run_text(["rev-list", "--count", revision_range]).strip())
        if commit_count < 1:
            raise GitToolError("there are no commits to push")
        commits = self._run_text(
            ["log", "--oneline", "--max-count=20", revision_range]
        ).splitlines()
        changed_files = [
            line
            for line in self._run_text(["diff", "--name-only", revision_range]).splitlines()
            if line
        ]
        findings = self._scan_head_files(changed_files)
        if findings:
            raise GitToolError(
                "secret scan blocked push: "
                + ", ".join(f"{item['path']} ({item['kind']})" for item in findings)
            )
        context = {
            "operation": "git_push",
            "remote": remote,
            "remote_url": remote_url,
            "branch": target_branch,
            "head": head,
            "remote_head": remote_head,
            "commit_count": commit_count,
            "commits": commits,
            "changed_files": changed_files,
            "working_tree_dirty": bool(
                self._run_text(["status", "--porcelain=v1"]).strip()
            ),
            "force": False,
            "secret_scan": "passed",
        }
        context["state_token"] = self._state_token(context)
        return context

    def push(self, *, remote: str = "origin", branch: str | None = None) -> dict[str, Any]:
        current_branch = self._run_text(["branch", "--show-current"]).strip()
        target_branch = branch or current_branch
        self._validate_remote(remote)
        self._validate_branch(target_branch)
        if target_branch != current_branch:
            raise GitToolError("git_push only supports the current branch")
        output = self._run_text(
            ["push", "--porcelain", remote, f"HEAD:refs/heads/{target_branch}"],
            timeout=60,
        )
        return {
            "remote": remote,
            "branch": target_branch,
            "head": self._run_text(["rev-parse", "HEAD"]).strip(),
            "force": False,
            "stdout": CommandRunner.truncate(output, 8_000),
        }

    def _resolve_path(self, value: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise GitToolError("path must be a non-empty string")
        resolved = (self.workspace / value).resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as error:
            raise GitToolError("Git path resolves outside the workspace") from error
        return resolved

    def _validate_files(self, files: list[str]) -> list[Path]:
        if not isinstance(files, list) or not 1 <= len(files) <= 50:
            raise GitToolError("files must contain between 1 and 50 paths")
        if not all(isinstance(path, str) and path.strip() for path in files):
            raise GitToolError("every commit path must be a non-empty string")
        resolved = [self._resolve_path(path) for path in files]
        relative = [path.relative_to(self.workspace).as_posix() for path in resolved]
        if len(set(relative)) != len(relative):
            raise GitToolError("commit paths must be unique")
        return resolved

    @staticmethod
    def _validate_commit_message(message: str) -> None:
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 200:
            raise GitToolError("commit message must contain between 1 and 200 characters")
        if "\n" in message or "\r" in message:
            raise GitToolError("commit message must be a single line")

    @staticmethod
    def _validate_remote(remote: str) -> None:
        if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", remote):
            raise GitToolError("remote name is invalid")

    def _validate_branch(self, branch: str) -> None:
        if not isinstance(branch, str) or not branch:
            raise GitToolError("branch name is invalid")
        self._run_text(["check-ref-format", "--branch", branch])

    @staticmethod
    def _validate_remote_url(url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme in {"http", "https"} and (parsed.username or parsed.password):
            raise GitToolError("remote URL must not contain embedded credentials")

    def _head_or_unborn(self) -> str:
        try:
            return self._run_text(["rev-parse", "HEAD"]).strip()
        except GitToolError:
            return "<unborn>"

    def _scan_worktree_files(self, paths: list[Path]) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        for path in paths:
            if not path.is_file() or path.stat().st_size > 1_000_000:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            findings.extend(
                self._find_secrets(path.relative_to(self.workspace).as_posix(), text)
            )
        return findings

    def _scan_head_files(self, paths: list[str]) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        for path in paths:
            try:
                text = self._run_text(["show", f"HEAD:{path}"], max_output=1_000_001)
            except GitToolError as error:
                if "exceeds the safety limit" in str(error):
                    findings.append({"path": path, "kind": "unscannable_large_file"})
                continue
            if len(text) > 1_000_000 or "\x00" in text:
                continue
            findings.extend(self._find_secrets(path, text))
        return findings

    @staticmethod
    def _find_secrets(path: str, text: str) -> list[dict[str, str]]:
        return [
            {"path": path, "kind": kind}
            for kind, pattern in SECRET_PATTERNS.items()
            if pattern.search(text)
        ]

    @staticmethod
    def _file_hash(path: Path) -> str:
        if not path.exists():
            return "<deleted>"
        if path.is_dir():
            return "<directory>"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _state_token(context: dict[str, Any]) -> str:
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _run(self, arguments: list[str]) -> dict[str, Any]:
        stdout = self._run_text(arguments)
        return {
            "argv": ["git", *arguments],
            "exit_code": 0,
            "stdout": CommandRunner.truncate(stdout, 32_000),
            "stderr": "",
        }

    def _run_text(
        self,
        arguments: list[str],
        *,
        timeout: int = 20,
        max_output: int = 32_000,
    ) -> str:
        environment = CommandRunner.safe_environment()
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = self._process_runner(
                ["git", *arguments],
                cwd=self.workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
        except FileNotFoundError as error:
            raise GitToolError("git executable was not found") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise GitToolError(f"Git command failed: {detail[:500]}")
        if len(completed.stdout) > max_output:
            raise GitToolError("Git output exceeds the safety limit")
        return completed.stdout

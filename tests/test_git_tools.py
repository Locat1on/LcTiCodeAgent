from __future__ import annotations

import subprocess
import unittest

from code_agent.git_tools import GitInspector, GitToolError
from tests.helpers import test_directory


class _PushCapturingRunner:
    def __init__(self) -> None:
        self.push_argv: list[str] | None = None

    def __call__(self, argv, **kwargs):
        if len(argv) > 1 and argv[1] == "push":
            self.push_argv = argv
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="push simulated for deterministic test\n",
                stderr="",
            )
        return subprocess.run(argv, **kwargs)


class GitInspectorTests(unittest.TestCase):
    def test_status_diff_and_log_are_read_only(self) -> None:
        with test_directory() as workspace:
            self._git(workspace, "init", "-q", "-b", "main")
            source = workspace / "example.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(workspace, "add", "example.py")
            self._git(
                workspace,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-q",
                "-m",
                "initial",
            )
            source.write_text("value = 2\n", encoding="utf-8")
            inspector = GitInspector(workspace)

            status = inspector.status()
            diff = inspector.diff(path="example.py")
            log = inspector.log(max_count=1)

        self.assertIn("example.py", status["stdout"])
        self.assertIn("-value = 1", diff["stdout"])
        self.assertIn("+value = 2", diff["stdout"])
        self.assertIn("initial", log["stdout"])

    def test_diff_rejects_path_outside_workspace(self) -> None:
        with test_directory() as workspace:
            inspector = GitInspector(workspace)
            with self.assertRaisesRegex(GitToolError, "outside"):
                inspector.diff(path="../outside.txt")

    def test_commit_and_push_use_preflight_and_fixed_refspec(self) -> None:
        with test_directory() as root:
            workspace = root / "repo"
            remote = root / "remote.git"
            workspace.mkdir()
            self._git(root, "init", "-q", "--bare", str(remote.resolve()))
            self._git(workspace, "init", "-q", "-b", "main")
            self._git(workspace, "config", "user.name", "Test User")
            self._git(workspace, "config", "user.email", "test@example.com")
            source = workspace / "example.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(workspace, "add", "example.py")
            self._git(workspace, "commit", "-q", "-m", "initial")
            initial_head = self._git_output(workspace, "rev-parse", "HEAD").strip()
            self._git(workspace, "remote", "add", "origin", str(remote.resolve()))
            self._git(
                workspace,
                "update-ref",
                "refs/remotes/origin/main",
                initial_head,
            )

            source.write_text("value = 2\n", encoding="utf-8")
            unrelated = workspace / "unrelated.txt"
            unrelated.write_text("not selected\n", encoding="utf-8")
            process_runner = _PushCapturingRunner()
            inspector = GitInspector(workspace, process_runner=process_runner)

            commit_context = inspector.commit_preflight(
                files=["example.py"],
                message="update value",
            )
            commit_result = inspector.commit(
                files=["example.py"],
                message="update value",
            )
            push_context = inspector.push_preflight()
            push_result = inspector.push()
            status = inspector.status()["stdout"]

        self.assertEqual(commit_context["secret_scan"], "passed")
        self.assertTrue(commit_context["state_token"])
        self.assertEqual(push_context["commit_count"], 1)
        self.assertFalse(push_context["force"])
        self.assertEqual(push_result["head"], commit_result["head"])
        self.assertEqual(
            process_runner.push_argv,
            ["git", "push", "--porcelain", "origin", "HEAD:refs/heads/main"],
        )
        self.assertIn("unrelated.txt", status)
        self.assertEqual(commit_result["message"], "update value")

    def test_commit_preflight_blocks_high_confidence_secret(self) -> None:
        with test_directory() as workspace:
            self._git(workspace, "init", "-q", "-b", "main")
            secret = workspace / "secret.txt"
            secret.write_text(
                "OPENROUTER_API_KEY=sk-or-v1-abcdefghijklmnopqrstuvwxyz123456\n",
                encoding="utf-8",
            )
            inspector = GitInspector(workspace)

            with self.assertRaisesRegex(GitToolError, "secret scan blocked"):
                inspector.commit_preflight(
                    files=["secret.txt"],
                    message="add secret",
                )

    @staticmethod
    def _git(workspace, *arguments) -> None:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)

    @staticmethod
    def _git_output(workspace, *arguments) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
        return completed.stdout


if __name__ == "__main__":
    unittest.main()

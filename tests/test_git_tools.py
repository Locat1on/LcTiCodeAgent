from __future__ import annotations

import subprocess
import unittest

from code_agent.git_tools import GitInspector, GitToolError
from tests.helpers import test_directory


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


if __name__ == "__main__":
    unittest.main()

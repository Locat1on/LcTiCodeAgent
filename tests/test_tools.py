from __future__ import annotations

import json
import unittest

from code_agent.tools import ToolRegistry
from tests.helpers import test_directory


class ToolRegistryTests(unittest.TestCase):
    def test_list_files_ignores_internal_directories(self) -> None:
        with test_directory() as workspace:
            (workspace / "src").mkdir()
            (workspace / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
            (workspace / ".git").mkdir()
            (workspace / ".git" / "config").write_text("secret", encoding="utf-8")
            (workspace / "package.egg-info").mkdir()
            (workspace / "package.egg-info" / "PKG-INFO").write_text(
                "metadata",
                encoding="utf-8",
            )
            registry = ToolRegistry(workspace)

            result = registry.execute("list_files", {"path": ".", "depth": 2})
            payload = json.loads(result.content)

        paths = {entry["path"] for entry in payload["entries"]}
        self.assertFalse(result.is_error)
        self.assertIn("src/main.py", paths)
        self.assertNotIn(".git/config", paths)
        self.assertNotIn("package.egg-info/PKG-INFO", paths)

    def test_read_file_returns_numbered_range(self) -> None:
        with test_directory() as workspace:
            (workspace / "example.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
            registry = ToolRegistry(workspace)

            result = registry.execute(
                "read_file",
                {"path": "example.py", "start_line": 2, "end_line": 3},
            )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "2: two\n3: three")

    def test_path_escape_and_sensitive_file_are_denied(self) -> None:
        with test_directory() as workspace:
            (workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")
            registry = ToolRegistry(workspace)

            escaped = registry.execute("read_file", {"path": "../outside.txt"})
            sensitive = registry.execute("read_file", {"path": ".env"})

        self.assertTrue(escaped.is_error)
        self.assertIn("outside", escaped.content)
        self.assertTrue(sensitive.is_error)
        self.assertIn("sensitive", sensitive.content)

    def test_env_example_is_readable_but_real_env_is_not(self) -> None:
        with test_directory() as workspace:
            (workspace / ".env.example").write_text("TOKEN=", encoding="utf-8")
            (workspace / ".env.local").write_text("TOKEN=secret", encoding="utf-8")
            registry = ToolRegistry(workspace)

            example = registry.execute("read_file", {"path": ".env.example"})
            local = registry.execute("read_file", {"path": ".env.local"})

        self.assertFalse(example.is_error)
        self.assertTrue(local.is_error)


if __name__ == "__main__":
    unittest.main()

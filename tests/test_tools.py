from __future__ import annotations

import hashlib
import json
import os
import unittest
from unittest.mock import patch

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
            source = workspace / "example.py"
            source.write_text("one\ntwo\nthree\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            registry = ToolRegistry(workspace)

            result = registry.execute(
                "read_file",
                {"path": "example.py", "start_line": 2, "end_line": 3},
            )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, f"sha256: {digest}\n2: two\n3: three")

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

    def test_search_text_finds_matches_and_skips_sensitive_files(self) -> None:
        with test_directory() as workspace:
            (workspace / "notes.txt").write_text("needle in notes\n", encoding="utf-8")
            (workspace / "server.pem").write_text("needle in cert\n", encoding="utf-8")
            (workspace / "id_rsa").write_text("needle in key\n", encoding="utf-8")
            registry = ToolRegistry(workspace)

            result = registry.execute("search_text", {"query": "needle"})

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        paths = [match["path"] for match in payload["matches"]]
        self.assertEqual(paths, ["notes.txt"])
        self.assertEqual(payload["returned"], 1)
        self.assertIn("line", payload["matches"][0])
        self.assertIn("snippet", payload["matches"][0])

    def test_search_text_validates_arguments(self) -> None:
        with test_directory() as workspace:
            (workspace / "app.py").write_text("needle\n", encoding="utf-8")
            registry = ToolRegistry(workspace)

            missing = registry.execute("search_text", {})
            empty = registry.execute("search_text", {"query": "  "})
            low = registry.execute("search_text", {"query": "x", "max_results": 0})
            high = registry.execute("search_text", {"query": "x", "max_results": 201})
            escaped = registry.execute(
                "search_text", {"query": "x", "path": "../outside"}
            )

        self.assertTrue(missing.is_error)
        self.assertIn("query", missing.content)
        self.assertTrue(empty.is_error)
        self.assertTrue(low.is_error)
        self.assertIn("max_results", low.content)
        self.assertTrue(high.is_error)
        self.assertTrue(escaped.is_error)
        self.assertIn("outside", escaped.content)

    def test_search_text_caps_serialized_payload(self) -> None:
        class _StubSearcher:
            def __init__(self, result):
                self.result = result

            def search(self, query, *, relative_path=".", max_results=50):
                return self.result

        with test_directory() as workspace:
            stub = _StubSearcher(
                {
                    "query": "needle",
                    "path": ".",
                    "engine": "stub",
                    "files_searched": 1,
                    "returned": 200,
                    "truncated": False,
                    "matches": [
                        {
                            "path": f"dir_{index // 50}/file_{index}.py",
                            "line": 1,
                            "snippet": "n" * 200,
                        }
                        for index in range(200)
                    ],
                }
            )
            registry = ToolRegistry(workspace, searcher=stub)

            result = registry.execute(
                "search_text", {"query": "needle", "max_results": 200}
            )

        self.assertFalse(result.is_error)
        self.assertLessEqual(len(result.content), 24_000)
        payload = json.loads(result.content)
        self.assertTrue(payload["truncated"])
        self.assertLess(payload["returned"], 200)

    def test_replace_requires_one_exact_match(self) -> None:
        with test_directory() as workspace:
            source = workspace / "example.py"
            source.write_text("value = 1\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            registry = ToolRegistry(workspace)

            result = registry.execute(
                "replace_in_file",
                {
                    "path": "example.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                    "expected_sha256": digest,
                },
            )
            updated = source.read_text(encoding="utf-8")

        self.assertFalse(result.is_error)
        self.assertEqual(updated, "value = 2\n")
        self.assertNotEqual(
            json.loads(result.content)["old_sha256"],
            json.loads(result.content)["new_sha256"],
        )

    def test_replace_rejects_ambiguous_match_without_changing_file(self) -> None:
        with test_directory() as workspace:
            source = workspace / "example.txt"
            source.write_text("same\nsame\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            registry = ToolRegistry(workspace)

            result = registry.execute(
                "replace_in_file",
                {
                    "path": "example.txt",
                    "old_text": "same",
                    "new_text": "new",
                    "expected_sha256": digest,
                },
            )
            preserved = source.read_text(encoding="utf-8")

        self.assertTrue(result.is_error)
        self.assertIn("found 2", result.content)
        self.assertEqual(preserved, "same\nsame\n")

    def test_replace_accepts_sha256_returned_by_read_file(self) -> None:
        with test_directory() as workspace:
            source = workspace / "example.py"
            source.write_text("alpha\nbeta\n", encoding="utf-8")
            registry = ToolRegistry(workspace)

            read = registry.execute(
                "read_file",
                {"path": "example.py", "start_line": 1, "end_line": 1},
            )
            digest = read.content.splitlines()[0].removeprefix("sha256: ")
            result = registry.execute(
                "replace_in_file",
                {
                    "path": "example.py",
                    "old_text": "beta",
                    "new_text": "gamma",
                    "expected_sha256": digest,
                },
            )
            updated = source.read_text(encoding="utf-8")

        self.assertFalse(read.is_error)
        self.assertFalse(result.is_error)
        self.assertIn("gamma", updated)

    def test_replace_rejects_stale_sha256_and_keeps_file(self) -> None:
        with test_directory() as workspace:
            source = workspace / "example.py"
            source.write_text("first version\n", encoding="utf-8")
            registry = ToolRegistry(workspace)

            read = registry.execute("read_file", {"path": "example.py"})
            digest = read.content.splitlines()[0].removeprefix("sha256: ")
            source.write_text("rewritten externally\n", encoding="utf-8")
            result = registry.execute(
                "replace_in_file",
                {
                    "path": "example.py",
                    "old_text": "rewritten externally",
                    "new_text": "edited by agent",
                    "expected_sha256": digest,
                },
            )
            preserved = source.read_text(encoding="utf-8")

        self.assertTrue(result.is_error)
        self.assertIn("changed since", result.content)
        self.assertIn("re-read", result.content)
        self.assertEqual(preserved, "rewritten externally\n")

    def test_replace_requires_expected_sha256(self) -> None:
        with test_directory() as workspace:
            (workspace / "example.py").write_text("value = 1\n", encoding="utf-8")
            registry = ToolRegistry(workspace)

            result = registry.execute(
                "replace_in_file",
                {
                    "path": "example.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                },
            )

        self.assertTrue(result.is_error)
        self.assertIn("expected_sha256", result.content)

    def test_replace_rejects_malformed_sha256(self) -> None:
        with test_directory() as workspace:
            (workspace / "example.py").write_text("value = 1\n", encoding="utf-8")
            registry = ToolRegistry(workspace)

            result = registry.execute(
                "replace_in_file",
                {
                    "path": "example.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                    "expected_sha256": "nothex",
                },
            )

        self.assertTrue(result.is_error)
        self.assertIn("64-character", result.content)

    def test_write_file_creates_new_file_but_never_overwrites(self) -> None:
        with test_directory() as workspace:
            registry = ToolRegistry(workspace)

            created = registry.execute(
                "write_file",
                {"path": "new/module.py", "content": "answer = 42\n"},
            )
            overwritten = registry.execute(
                "write_file",
                {"path": "new/module.py", "content": "answer = 0\n"},
            )
            content = (workspace / "new" / "module.py").read_text(encoding="utf-8")

        self.assertFalse(created.is_error)
        self.assertTrue(overwritten.is_error)
        self.assertEqual(content, "answer = 42\n")

    def test_write_rejects_sensitive_and_outside_paths(self) -> None:
        with test_directory() as workspace:
            registry = ToolRegistry(workspace)

            sensitive = registry.execute(
                "write_file",
                {"path": ".env", "content": "TOKEN=secret"},
            )
            escaped = registry.execute(
                "write_file",
                {"path": "../outside.py", "content": "unsafe = True"},
            )

        self.assertTrue(sensitive.is_error)
        self.assertTrue(escaped.is_error)

    def test_run_command_executes_tests_without_forwarding_api_key(self) -> None:
        with test_directory() as workspace:
            tests = workspace / "tests"
            tests.mkdir()
            (tests / "test_environment.py").write_text(
                "import os\n"
                "import unittest\n\n"
                "class EnvironmentTests(unittest.TestCase):\n"
                "    def test_secret_is_absent(self):\n"
                "        self.assertNotIn('OPENROUTER_API_KEY', os.environ)\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(workspace)
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "top-secret"}):
                result = registry.execute(
                    "run_command",
                    {
                        "argv": [
                            "python",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                            "-v",
                        ]
                    },
                )
            outcome = json.loads(result.content)

        self.assertFalse(result.is_error)
        self.assertEqual(outcome["exit_code"], 0)
        self.assertFalse(outcome["timed_out"])

    def test_run_command_reports_failure_and_denies_arbitrary_python(self) -> None:
        with test_directory() as workspace:
            tests = workspace / "tests"
            tests.mkdir()
            (tests / "test_failure.py").write_text(
                "import unittest\n\n"
                "class FailureTests(unittest.TestCase):\n"
                "    def test_failure(self):\n"
                "        self.fail('expected failure')\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(workspace)

            failed = registry.execute(
                "run_command",
                {"argv": ["python", "-m", "unittest", "discover", "-s", "tests"]},
            )
            denied = registry.execute(
                "run_command",
                {"argv": ["python", "-c", "print('not allowed')"]},
            )

        self.assertTrue(failed.is_error)
        self.assertNotEqual(json.loads(failed.content)["exit_code"], 0)
        self.assertTrue(denied.is_error)
        self.assertIn("limited", denied.content)

    def test_run_command_times_out(self) -> None:
        with test_directory() as workspace:
            tests = workspace / "tests"
            tests.mkdir()
            (tests / "test_slow.py").write_text(
                "import time\n"
                "import unittest\n\n"
                "class SlowTests(unittest.TestCase):\n"
                "    def test_slow(self):\n"
                "        time.sleep(3)\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(workspace)

            result = registry.execute(
                "run_command",
                {
                    "argv": ["python", "-m", "unittest", "discover", "-s", "tests"],
                    "timeout_seconds": 1,
                },
            )
            outcome = json.loads(result.content)

        self.assertTrue(result.is_error)
        self.assertTrue(outcome["timed_out"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from code_agent.search import SearchError, TextSearcher
from tests.helpers import test_directory


class _NoRipgrepRunner:
    def __call__(self, argv, **kwargs):
        raise FileNotFoundError("rg not found")


class _TimeoutRunner:
    def __call__(self, argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 15)


class _FakeRgRunner:
    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.argv: list[str] | None = None
        self.kwargs: dict[str, object] | None = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return subprocess.CompletedProcess(
            argv,
            self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def _rg_event(kind: str, **data) -> str:
    return json.dumps({"type": kind, "data": data})


class PythonEngineTests(unittest.TestCase):
    def test_python_engine_returns_path_line_snippet(self) -> None:
        with test_directory() as workspace:
            (workspace / "alpha.py").write_text(
                "needle here\nplain\nneedle again\n", encoding="utf-8"
            )
            (workspace / "beta.py").write_text("intro\nfinal needle\n", encoding="utf-8")
            searcher = TextSearcher(workspace, process_runner=_NoRipgrepRunner())

            result = searcher.search("needle")

        self.assertEqual(result["engine"], "python")
        self.assertEqual(result["path"], ".")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["files_searched"], 2)
        self.assertEqual(result["returned"], 3)
        self.assertEqual(
            result["matches"],
            [
                {"path": "alpha.py", "line": 1, "snippet": "needle here"},
                {"path": "alpha.py", "line": 3, "snippet": "needle again"},
                {"path": "beta.py", "line": 2, "snippet": "final needle"},
            ],
        )

    def test_python_engine_is_case_sensitive(self) -> None:
        with test_directory() as workspace:
            (workspace / "app.py").write_text("Value = 1\nvalue = 2\n", encoding="utf-8")
            searcher = TextSearcher(workspace, process_runner=_NoRipgrepRunner())

            result = searcher.search("Value")

        self.assertEqual(
            [(match["path"], match["line"]) for match in result["matches"]],
            [("app.py", 1)],
        )

    def test_python_engine_skips_excluded_and_hidden_paths(self) -> None:
        with test_directory() as workspace:
            (workspace / "app.py").write_text("needle target\n", encoding="utf-8")
            for name in ("node_modules", "tmp", "sessions", "__pycache__", "demo.egg-info"):
                directory = workspace / name
                directory.mkdir()
                (directory / "dep.py").write_text("needle excluded\n", encoding="utf-8")
            (workspace / ".hidden").mkdir()
            (workspace / ".hidden" / "secret.py").write_text("needle hidden\n", encoding="utf-8")
            (workspace / ".note").write_text("needle dotfile\n", encoding="utf-8")
            searcher = TextSearcher(workspace, process_runner=_NoRipgrepRunner())

            result = searcher.search("needle")

        self.assertEqual(
            [match["path"] for match in result["matches"]],
            ["app.py"],
        )

    def test_python_engine_skips_binary_large_and_non_utf8_files(self) -> None:
        with test_directory() as workspace:
            (workspace / "app.py").write_text("needle text\n", encoding="utf-8")
            (workspace / "blob.bin").write_bytes(b"needle\x00binary\n")
            (workspace / "gbk.txt").write_bytes("针needle".encode("gbk"))
            (workspace / "big.py").write_text(
                "needle big\n" + "x" * 1_000_100, encoding="utf-8"
            )
            searcher = TextSearcher(workspace, process_runner=_NoRipgrepRunner())

            result = searcher.search("needle")

        self.assertEqual([match["path"] for match in result["matches"]], ["app.py"])

    def test_python_engine_truncates_beyond_max_results(self) -> None:
        with test_directory() as workspace:
            (workspace / "many.py").write_text("needle\n" * 5, encoding="utf-8")
            searcher = TextSearcher(workspace, process_runner=_NoRipgrepRunner())

            result = searcher.search("needle", max_results=2)

        self.assertTrue(result["truncated"])
        self.assertEqual(result["returned"], 2)
        self.assertEqual([match["line"] for match in result["matches"]], [1, 2])

    def test_invalid_regex_raises_search_error(self) -> None:
        with test_directory() as workspace:
            searcher = TextSearcher(workspace, process_runner=_NoRipgrepRunner())

            with self.assertRaisesRegex(SearchError, "regular expression"):
                searcher.search("(")

    def test_rejects_path_escape_file_and_excluded_root(self) -> None:
        with test_directory() as workspace:
            (workspace / "file.py").write_text("content\n", encoding="utf-8")
            (workspace / ".git").mkdir()
            (workspace / ".git" / "config").write_text("needle\n", encoding="utf-8")
            searcher = TextSearcher(workspace, process_runner=_NoRipgrepRunner())

            with self.assertRaisesRegex(SearchError, "outside"):
                searcher.search("needle", relative_path="../outside")
            with self.assertRaisesRegex(SearchError, "not a directory"):
                searcher.search("needle", relative_path="file.py")
            with self.assertRaisesRegex(SearchError, "excluded"):
                searcher.search("needle", relative_path=".git")


class RipgrepEngineTests(unittest.TestCase):
    def test_ripgrep_parses_events_and_normalizes_windows_paths(self) -> None:
        with test_directory() as workspace:
            runner = _FakeRgRunner(
                stdout="\n".join(
                    (
                        _rg_event("begin", path={"text": ".\\src\\app.py"}),
                        _rg_event(
                            "match",
                            path={"text": ".\\src\\app.py"},
                            line_number=3,
                            lines={"text": "needle here\n"},
                        ),
                        _rg_event(
                            "match",
                            path={"text": "src\\other.py"},
                            line_number=7,
                            lines={"text": "needle again\r\n"},
                        ),
                        json.dumps(
                            {"type": "summary", "data": {"stats": {"searches": 2}}}
                        ),
                    )
                )
            )
            searcher = TextSearcher(workspace, process_runner=runner)

            result = searcher.search("needle")

        self.assertEqual(result["engine"], "ripgrep")
        self.assertEqual(result["files_searched"], 2)
        self.assertEqual(result["returned"], 2)
        self.assertFalse(result["truncated"])
        self.assertEqual(
            result["matches"],
            [
                {"path": "src/app.py", "line": 3, "snippet": "needle here"},
                {"path": "src/other.py", "line": 7, "snippet": "needle again"},
            ],
        )

    def test_ripgrep_exit_one_means_no_matches(self) -> None:
        with test_directory() as workspace:
            runner = _FakeRgRunner(
                returncode=1,
                stdout=json.dumps(
                    {"type": "summary", "data": {"stats": {"searches": 3}}}
                ),
            )
            searcher = TextSearcher(workspace, process_runner=runner)

            result = searcher.search("needle")

        self.assertEqual(result["engine"], "ripgrep")
        self.assertEqual(result["matches"], [])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["files_searched"], 3)

    def test_ripgrep_exit_two_raises_search_error(self) -> None:
        with test_directory() as workspace:
            runner = _FakeRgRunner(returncode=2, stderr="rg: regex parse error")
            searcher = TextSearcher(workspace, process_runner=runner)

            with self.assertRaisesRegex(SearchError, "ripgrep failed"):
                searcher.search("(")

    def test_ripgrep_truncates_beyond_max_results(self) -> None:
        with test_directory() as workspace:
            events = [
                _rg_event(
                    "match",
                    path={"text": f"src/file_{index}.py"},
                    line_number=index + 1,
                    lines={"text": "needle\n"},
                )
                for index in range(3)
            ]
            runner = _FakeRgRunner(stdout="\n".join(events))
            searcher = TextSearcher(workspace, process_runner=runner)

            result = searcher.search("needle", max_results=2)

        self.assertTrue(result["truncated"])
        self.assertEqual(result["returned"], 2)
        self.assertEqual(
            [match["path"] for match in result["matches"]],
            ["src/file_0.py", "src/file_1.py"],
        )

    def test_ripgrep_timeout_is_contained(self) -> None:
        with test_directory() as workspace:
            searcher = TextSearcher(workspace, process_runner=_TimeoutRunner())

            with self.assertRaisesRegex(SearchError, "timed out"):
                searcher.search("needle")

    def test_ripgrep_missing_falls_back_to_python(self) -> None:
        with test_directory() as workspace:
            (workspace / "app.py").write_text("needle here\n", encoding="utf-8")
            searcher = TextSearcher(workspace, process_runner=_NoRipgrepRunner())

            result = searcher.search("needle")

        self.assertEqual(result["engine"], "python")
        self.assertEqual(result["matches"][0]["path"], "app.py")

    def test_ripgrep_uses_fixed_safe_arguments(self) -> None:
        with test_directory() as workspace:
            (workspace / "src").mkdir()
            runner = _FakeRgRunner()
            searcher = TextSearcher(workspace, process_runner=runner)

            searcher.search("needle", relative_path="src")

        argv = runner.argv
        kwargs = runner.kwargs
        assert argv is not None and kwargs is not None
        self.assertEqual(argv[0], "rg")
        for flag in ("--json", "--no-ignore", "--max-filesize"):
            self.assertIn(flag, argv)
        self.assertIn("!**/{node_modules,tmp,sessions,__pycache__}/**", argv)
        self.assertIn("!**/*.egg-info/**", argv)
        self.assertEqual(argv[argv.index("-e") + 1], "needle")
        self.assertEqual(argv[-1], "src")
        self.assertEqual(kwargs["cwd"], workspace.resolve())
        self.assertEqual(kwargs["timeout"], 15)
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    @unittest.skipUnless(shutil.which("rg") is not None, "ripgrep not installed")
    def test_real_ripgrep_end_to_end(self) -> None:
        with test_directory() as workspace:
            (workspace / "app.py").write_text("needle here\n", encoding="utf-8")
            (workspace / "node_modules").mkdir()
            (workspace / "node_modules" / "dep.py").write_text(
                "needle excluded\n", encoding="utf-8"
            )
            searcher = TextSearcher(workspace)

            result = searcher.search("needle")

        self.assertEqual(result["engine"], "ripgrep")
        self.assertEqual([match["path"] for match in result["matches"]], ["app.py"])


if __name__ == "__main__":
    unittest.main()

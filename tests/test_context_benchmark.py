from __future__ import annotations

import unittest

from code_agent.git_tools import GitInspector
from experiments.context_benchmark import claims_test_success, initialize_git_baseline
from tests.helpers import test_directory


class BenchmarkEvidenceTests(unittest.TestCase):
    def test_recognizes_common_success_wording(self) -> None:
        for text in (
            "All tests pass.",
            "2 tests passed.",
            "测试全部通过。",
            "验证成功。",
            "Tests: OK",
        ):
            with self.subTest(text=text):
                self.assertTrue(claims_test_success(text))

    def test_failure_wording_wins_over_success_wording(self) -> None:
        self.assertFalse(claims_test_success("1 test passed, 1 test failed"))
        self.assertFalse(claims_test_success("测试未通过"))

    def test_git_baseline_makes_workspace_changes_visible(self) -> None:
        with test_directory() as root:
            workspace = root / "workspace"
            workspace.mkdir()
            source = workspace / "calculator.py"
            source.write_text("value = 1\n", encoding="utf-8")

            initialize_git_baseline(workspace)
            source.write_text("value = 2\n", encoding="utf-8")

            inspector = GitInspector(workspace)
            status = inspector.status()["stdout"]
            diff = inspector.diff(path="calculator.py")["stdout"]

        self.assertIn("calculator.py", status)
        self.assertIn("-value = 1", diff)
        self.assertIn("+value = 2", diff)


if __name__ == "__main__":
    unittest.main()

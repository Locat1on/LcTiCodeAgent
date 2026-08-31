from __future__ import annotations

import unittest

from experiments.context_benchmark import claims_test_success


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


if __name__ == "__main__":
    unittest.main()

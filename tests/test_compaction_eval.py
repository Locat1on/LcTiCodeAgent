from __future__ import annotations

import unittest

from experiments.context_compaction import evaluate


class CompactionEvaluationTests(unittest.TestCase):
    def test_four_strategies_emit_comparable_metrics(self) -> None:
        metrics = {item.strategy: item for item in evaluate()}
        self.assertEqual(
            set(metrics),
            {
                "no_compression",
                "drop_oldest",
                "plain_summary",
                "validated_structured_summary",
            },
        )
        proposed = metrics["validated_structured_summary"]
        self.assertLess(proposed.compression_ratio, 0.5)
        self.assertEqual(proposed.fact_recall, 1.0)
        self.assertEqual(proposed.event_recall, 1.0)
        self.assertEqual(proposed.validation, "passed")
        self.assertFalse(metrics["drop_oldest"].tool_pairing_valid)


if __name__ == "__main__":
    unittest.main()

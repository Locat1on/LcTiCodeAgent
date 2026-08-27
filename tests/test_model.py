from __future__ import annotations

import unittest

from code_agent.model import ToolCallAccumulator, ToolCallParseError


class ToolCallAccumulatorTests(unittest.TestCase):
    def test_fragmented_function_call_is_reconstructed(self) -> None:
        accumulator = ToolCallAccumulator()
        accumulator.add(
            0,
            call_id="call-1",
            name_fragment="read_",
            arguments_fragment='{"path":',
        )
        accumulator.add(
            0,
            name_fragment="file",
            arguments_fragment='"README.md"}',
        )

        calls = accumulator.finish()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].call_id, "call-1")
        self.assertEqual(calls[0].name, "read_file")
        self.assertEqual(calls[0].arguments, {"path": "README.md"})

    def test_invalid_json_arguments_are_rejected(self) -> None:
        accumulator = ToolCallAccumulator()
        accumulator.add(
            0,
            call_id="call-1",
            name_fragment="read_file",
            arguments_fragment='{"path":',
        )

        with self.assertRaisesRegex(ToolCallParseError, "invalid JSON"):
            accumulator.finish()

    def test_non_object_arguments_are_rejected(self) -> None:
        accumulator = ToolCallAccumulator()
        accumulator.add(
            0,
            call_id="call-1",
            name_fragment="read_file",
            arguments_fragment='["README.md"]',
        )

        with self.assertRaisesRegex(ToolCallParseError, "JSON object"):
            accumulator.finish()


if __name__ == "__main__":
    unittest.main()


from __future__ import annotations

import unittest

from code_agent.summary import SummaryValidationError, validate_summary


def _summary() -> dict:
    return {
        "version": 1,
        "objective": "Fix app.py and verify the result",
        "completed": ["Updated average in app.py"],
        "decisions": [],
        "files": ["app.py"],
        "identifiers": ["average"],
        "commands": ['["python", "-m", "unittest"]'],
        "exit_codes": [0],
        "open_errors": [],
        "next_actions": [],
        "event_ids": ["event-1"],
    }


class SummaryValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.messages = [
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": (
                    'Updated average in app.py; identifier average; '
                    'argv ["python", "-m", "unittest"]; exit_code 0; '
                    "All tests passed; objective Fix app.py and verify the result"
                ),
            }
        ]

    def test_accepts_fixed_schema_with_grounded_facts(self) -> None:
        summary = _summary()
        self.assertIs(
            validate_summary(summary, self.messages, ["event-1"]),
            summary,
        )

    def test_rejects_unknown_event_id(self) -> None:
        summary = _summary()
        summary["event_ids"] = ["invented"]
        with self.assertRaisesRegex(SummaryValidationError, "event_id"):
            validate_summary(summary, self.messages, ["event-1"])

    def test_rejects_hallucinated_path_identifier_and_exit_code(self) -> None:
        for mutate in (
            lambda value: value["files"].__setitem__(0, "secret.py"),
            lambda value: value["identifiers"].append("invented_name"),
            lambda value: value["exit_codes"].__setitem__(0, 17),
        ):
            with self.subTest(mutate=mutate):
                summary = _summary()
                mutate(summary)
                with self.assertRaises(SummaryValidationError):
                    validate_summary(summary, self.messages, ["event-1"])

    def test_rejects_extra_schema_field(self) -> None:
        summary = _summary()
        summary["notes"] = "not allowed"
        with self.assertRaisesRegex(SummaryValidationError, "fixed schema"):
            validate_summary(summary, self.messages, ["event-1"])


if __name__ == "__main__":
    unittest.main()

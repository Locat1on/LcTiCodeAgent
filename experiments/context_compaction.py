"""Deterministic four-baseline context-compaction evaluation harness."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from code_agent.context import TokenCounter
from code_agent.summary import validate_summary


@dataclass(frozen=True, slots=True)
class Metrics:
    strategy: str
    input_tokens: int
    output_tokens: int
    compression_ratio: float
    fact_recall: float
    event_recall: float
    tool_pairing_valid: bool
    validation: str


def fixture() -> dict[str, Any]:
    event_read = "11111111-1111-1111-1111-111111111111"
    event_test = "22222222-2222-2222-2222-222222222222"
    messages = [
        {"role": "user", "content": "Fix average in examples/buggy_average/app.py."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"examples/buggy_average/app.py"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-read",
            "content": (
                "examples/buggy_average/app.py defines average; sha256 abc123; "
                f"event_id {event_read}; " + "source line evidence " * 180
            ),
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-test",
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "arguments": '{"argv":["python","-m","unittest"]}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-test",
            "content": (
                'argv ["python", "-m", "unittest"]; exit_code 0; 122 tests passed; '
                f"event_id {event_test}; " + "verification output " * 180
            ),
        },
    ]
    facts = [
        "examples/buggy_average/app.py",
        "average",
        "exit_code 0",
        "122 tests passed",
    ]
    summary = {
        "version": 1,
        "objective": "Fix average in examples/buggy_average/app.py",
        "completed": [
            "122 tests passed with exit_code 0"
        ],
        "decisions": [],
        "files": ["examples/buggy_average/app.py"],
        "identifiers": ["average"],
        "commands": ['["python", "-m", "unittest"]'],
        "exit_codes": [0],
        "open_errors": [],
        "next_actions": [],
        "event_ids": [event_read, event_test],
    }
    return {
        "messages": messages,
        "facts": facts,
        "event_ids": [event_read, event_test],
        "summary": summary,
    }


def evaluate() -> list[Metrics]:
    case = fixture()
    source = json.dumps(case["messages"], ensure_ascii=False)
    target_chars = len(source) // 2
    proposed = json.dumps(case["summary"], ensure_ascii=False, sort_keys=True)
    validate_summary(case["summary"], case["messages"], case["event_ids"])
    outputs = {
        "no_compression": (source, True, "not_applicable"),
        "drop_oldest": (source[-target_chars:], False, "not_applicable"),
        "plain_summary": (
            "The task was inspected and tests were run. Continue from prior progress.",
            True,
            "not_checked",
        ),
        "validated_structured_summary": (proposed, True, "passed"),
    }
    input_tokens = TokenCounter.estimate(source)
    metrics: list[Metrics] = []
    for strategy, (output, pairing, validation) in outputs.items():
        output_tokens = TokenCounter.estimate(output)
        fact_hits = sum(fact in output for fact in case["facts"])
        event_hits = sum(event_id in output for event_id in case["event_ids"])
        metrics.append(
            Metrics(
                strategy=strategy,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                compression_ratio=round(output_tokens / input_tokens, 4),
                fact_recall=round(fact_hits / len(case["facts"]), 4),
                event_recall=round(event_hits / len(case["event_ids"]), 4),
                tool_pairing_valid=pairing,
                validation=validation,
            )
        )
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional JSON metrics path")
    args = parser.parse_args(argv)
    payload = {
        "fixture": "coding_context_v1",
        "note": "Deterministic mechanism fixture; not a model-quality benchmark.",
        "metrics": [asdict(metric) for metric in evaluate()],
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

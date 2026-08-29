"""Live structured-summary smoke test using synthetic, non-repository content."""

from __future__ import annotations

from code_agent.openrouter import OpenRouterConfig, OpenRouterProvider
from code_agent.summary import validate_summary


def main() -> int:
    event_id = "33333333-3333-3333-3333-333333333333"
    messages = [
        {"role": "user", "content": "Inspect app.py and run 7 tests."},
        {
            "role": "tool",
            "tool_call_id": "call-test",
            "content": (
                'app.py defines average; argv ["python", "-m", "unittest"]; '
                f"exit_code 0; 7 tests passed; event_id {event_id}"
            ),
        },
    ]
    provider = OpenRouterProvider(OpenRouterConfig.from_env())
    summary = provider.summarize_context(messages)
    print(f"returned_fields={','.join(summary)}")
    validate_summary(summary, messages, [event_id])
    print("openrouter_summary_smoke=passed")
    print(f"model={provider.config.model}")
    print(f"summary_fields={','.join(summary)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

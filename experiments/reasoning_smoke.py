"""Live OpenRouter reasoning-shape smoke test with synthetic content."""

from __future__ import annotations

from code_agent.model import ModelEventType
from code_agent.openrouter import OpenRouterConfig, OpenRouterProvider


def main() -> int:
    provider = OpenRouterProvider(OpenRouterConfig.from_env())
    events = list(
        provider.stream(
            [
                {
                    "role": "user",
                    "content": (
                        "分析编程智能体上下文压缩实验需要控制的变量，"
                        "给出简短结论，不调用工具。"
                    ),
                }
            ],
            [],
        )
    )
    reasoning = [
        event
        for event in events
        if event.event_type is ModelEventType.REASONING_DELTA
    ]
    details = [detail for event in reasoning for detail in event.reasoning_details]
    display = sum(len(event.text or "") for event in reasoning)
    summaries = sum(
        len(str(detail.get("summary") or ""))
        for detail in details
        if detail.get("type") == "reasoning.summary"
    )
    provider_text = sum(
        len(str(detail.get("text") or ""))
        for detail in details
        if detail.get("type") == "reasoning.text"
    )
    raw_reasoning = sum(len(event.reasoning or "") for event in reasoning)
    answer = sum(
        len(event.text or "")
        for event in events
        if event.event_type is ModelEventType.TEXT_DELTA
    )
    print("reasoning_smoke=passed")
    print(f"model={provider.config.model}")
    print(f"effort={provider.config.reasoning_effort}")
    print(f"detail_types={','.join(str(item.get('type')) for item in details)}")
    shapes = []
    for detail in details:
        fields = []
        for key, value in detail.items():
            size = len(value) if isinstance(value, (str, list, tuple, dict)) else 0
            fields.append(f"{key}:{type(value).__name__}:{size}")
        shapes.append("|".join(fields))
    print(f"detail_shapes={';'.join(shapes)}")
    print(f"summary_chars={summaries}")
    print(f"provider_text_chars={provider_text}")
    print(f"display_chars={display}")
    print(f"raw_reasoning_chars={raw_reasoning}")
    print(f"answer_chars={answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

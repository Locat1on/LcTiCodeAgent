"""Metrics extracted from append-only AgentEvent logs."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from .events import AgentEvent, EventType


@dataclass(frozen=True, slots=True)
class ContextMetrics:
    strategy: str
    compactions: int
    compactions_changed: int
    input_tokens: int
    output_tokens: int
    compression_ratio: float | None
    total_tokens_removed: int
    prompt_tokens: int
    completion_tokens: int
    tool_calls: int
    repeated_reads: int
    recoverable_events: int
    validation_passed: int
    validation_rejected: int
    final_turn_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def collect_context_metrics(
    events: list[AgentEvent],
    *,
    strategy: str,
) -> ContextMetrics:
    completed = [
        event
        for event in events
        if event.event_type is EventType.CONTEXT_COMPACTION_COMPLETED
    ]
    changed = [event for event in completed if event.payload.get("changed") is True]
    representative = max(
        changed,
        key=lambda event: int(event.payload.get("before_tokens", 0))
        - int(event.payload.get("after_tokens", 0)),
        default=None,
    )
    before_tokens = (
        int(representative.payload.get("before_tokens", 0))
        if representative
        else 0
    )
    after_tokens = (
        int(representative.payload.get("after_tokens", 0))
        if representative
        else 0
    )
    ratio = round(after_tokens / before_tokens, 4) if before_tokens else None
    total_removed = sum(
        max(
            0,
            int(event.payload.get("before_tokens", 0))
            - int(event.payload.get("after_tokens", 0)),
        )
        for event in changed
    )

    usage = [
        event.payload
        for event in events
        if event.event_type is EventType.CONTEXT_USAGE
    ]
    requested = [
        event
        for event in events
        if event.event_type is EventType.TOOL_REQUESTED
    ]
    read_paths = [
        str(event.payload.get("arguments", {}).get("path", ""))
        for event in requested
        if event.payload.get("name") == "read_file"
    ]
    counts = Counter(path for path in read_paths if path)
    repeated_reads = sum(count - 1 for count in counts.values() if count > 1)
    recoverable = {
        str(event_id)
        for event in completed
        for event_id in event.payload.get("pruned_event_ids", [])
        if event_id
    }
    reasons = [
        str(event.payload.get("reason"))
        for event in events
        if event.event_type is EventType.TURN_COMPLETED
    ]
    return ContextMetrics(
        strategy=strategy,
        compactions=len(completed),
        compactions_changed=len(changed),
        input_tokens=before_tokens,
        output_tokens=after_tokens,
        compression_ratio=ratio,
        total_tokens_removed=total_removed,
        prompt_tokens=sum(int(item.get("prompt_tokens", 0)) for item in usage),
        completion_tokens=sum(
            int(item.get("completion_tokens", 0)) for item in usage
        ),
        tool_calls=len(requested),
        repeated_reads=repeated_reads,
        recoverable_events=len(recoverable),
        validation_passed=sum(
            event.payload.get("validation") == "passed" for event in completed
        ),
        validation_rejected=sum(
            event.payload.get("validation") == "rejected" for event in completed
        ),
        final_turn_reason=reasons[-1] if reasons else None,
    )

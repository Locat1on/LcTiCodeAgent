"""Deterministic event source used to validate the first-stage UI."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

from .events import AgentEvent, EventType


class SimulatedAgent:
    """Produce realistic events without calling a model or changing files."""

    mode = "simulation"
    model = "simulator"
    sandbox = "simulation"
    context_limit = 32_000

    def __init__(self) -> None:
        self.used_tokens = 1_200

    def respond(
        self,
        user_text: str,
        session_id: str,
        turn_id: str,
    ) -> Iterator[AgentEvent]:
        step_id = str(uuid4())
        intro = "我会先检查项目结构和相关代码，再根据验证结果决定下一步。"
        for chunk in ("我会先检查项目结构", "和相关代码，", "再根据验证结果决定下一步。"):
            yield AgentEvent.create(
                EventType.ASSISTANT_DELTA,
                session_id,
                {"text": chunk},
                turn_id=turn_id,
                step_id=step_id,
            )
        yield AgentEvent.create(
            EventType.ASSISTANT_MESSAGE,
            session_id,
            {"text": intro},
            turn_id=turn_id,
            step_id=step_id,
        )

        yield from self._tool_events(
            session_id,
            turn_id,
            step_id,
            name="list_files",
            arguments={"path": ".", "depth": 2},
            summary="发现 8 个源码文件和 4 个测试文件",
            duration_ms=12,
        )
        yield from self._tool_events(
            session_id,
            turn_id,
            step_id,
            name="search_text",
            arguments={"query": self._query_from(user_text)},
            summary="在 src/service.py 和 tests/test_service.py 中找到相关实现",
            duration_ms=18,
        )

        self.used_tokens = min(self.context_limit, self.used_tokens + 860)
        yield AgentEvent.create(
            EventType.CONTEXT_USAGE,
            session_id,
            {"used_tokens": self.used_tokens, "limit_tokens": self.context_limit},
            turn_id=turn_id,
            step_id=step_id,
        )

        conclusion = (
            "当前为离线模拟工具流；使用 --live 可切换到 OpenRouter "
            "模型和本地只读工具。"
        )
        yield AgentEvent.create(
            EventType.ASSISTANT_DELTA,
            session_id,
            {"text": conclusion},
            turn_id=turn_id,
            step_id=step_id,
        )
        yield AgentEvent.create(
            EventType.ASSISTANT_MESSAGE,
            session_id,
            {"text": conclusion},
            turn_id=turn_id,
            step_id=step_id,
        )
        yield AgentEvent.create(
            EventType.TURN_COMPLETED,
            session_id,
            {"reason": "assistant_response"},
            turn_id=turn_id,
            step_id=step_id,
        )

    def clear_context(self) -> None:
        self.used_tokens = 0

    @staticmethod
    def _query_from(user_text: str) -> str:
        compact = " ".join(user_text.split())
        return compact[:40] or "project"

    @staticmethod
    def _tool_events(
        session_id: str,
        turn_id: str,
        step_id: str,
        *,
        name: str,
        arguments: dict[str, object],
        summary: str,
        duration_ms: int,
    ) -> Iterator[AgentEvent]:
        call_id = str(uuid4())
        common = {"call_id": call_id, "name": name}
        yield AgentEvent.create(
            EventType.TOOL_REQUESTED,
            session_id,
            {**common, "arguments": arguments},
            turn_id=turn_id,
            step_id=step_id,
        )
        yield AgentEvent.create(
            EventType.TOOL_STARTED,
            session_id,
            common,
            turn_id=turn_id,
            step_id=step_id,
        )
        yield AgentEvent.create(
            EventType.TOOL_COMPLETED,
            session_id,
            {**common, "summary": summary, "duration_ms": duration_ms},
            turn_id=turn_id,
            step_id=step_id,
        )

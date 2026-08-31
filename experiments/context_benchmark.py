"""Run four context strategies on isolated copies of buggy_average."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from code_agent.command import CommandRunner
from code_agent.context import CompactionStrategy
from code_agent.evaluation import collect_context_metrics
from code_agent.events import AgentEvent, EventType
from code_agent.live_agent import LiveAgent
from code_agent.openrouter import OpenRouterConfig, OpenRouterProvider
from code_agent.session import SessionLog
from code_agent.tools import ToolRegistry


PRELUDE_PROMPTS = (
    "只读检查 calculator.py 和测试文件，确认 average 的预期行为、当前缺陷和验证命令。不要修改文件，不要运行测试。",
    "继续只读分析：明确空列表约束、不能修改测试的约束，以及修复时必须保留的 average 标识符。不要修改文件。",
    "在仍不修改文件的前提下，回顾已有证据并给出最小修复计划，指出完成后应执行的测试命令。",
)
FIX_PROMPT = (
    "现在修复 average 对空列表除零的问题：空列表必须返回 0.0。"
    "不要修改测试，并运行测试验证；根据失败继续修复，最后如实报告证据。"
)
PROTECTED_IDENTIFIERS = ("average", "calculator.py", "0.0")


@dataclass(frozen=True, slots=True)
class TaskMetrics:
    strategy: str
    task_success: bool
    verification_exit_code: int
    tests_unchanged: bool
    constraint_retention: float
    identifier_retention: float
    test_evidence_accuracy: float
    elapsed_seconds: float
    session_log: str
    workspace: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _test_hashes(workspace: Path) -> dict[str, str]:
    return {
        path.relative_to(workspace).as_posix(): _sha256(path)
        for path in sorted((workspace / "tests").rglob("*.py"))
    }


def initialize_git_baseline(workspace: Path) -> None:
    """Create an isolated repository so Git evidence has a trusted baseline."""

    commands = (
        ("init", "-q", "-b", "main"),
        ("add", "--", "."),
        (
            "-c",
            "user.name=LcTiCodeAgent Evaluation",
            "-c",
            "user.email=evaluation@localhost",
            "commit",
            "-q",
            "-m",
            "fixture baseline",
        ),
    )
    for arguments in commands:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            env=CommandRunner.safe_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"failed to initialize evaluation Git baseline: {detail}")


def _run_turn(agent: LiveAgent, log: SessionLog, prompt: str) -> None:
    turn_id = str(uuid4())
    log.append(
        AgentEvent.create(
            EventType.USER_MESSAGE,
            log.session_id,
            {"text": prompt},
            turn_id=turn_id,
        )
    )
    for event in agent.respond(prompt, log.session_id, turn_id):
        log.append(event)


def _verify(workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=workspace,
        env=CommandRunner.safe_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        shell=False,
        check=False,
    )


def claims_test_success(text: str) -> bool:
    lower = text.lower()
    positive = bool(
        "通过" in text
        or "成功" in text
        or "全绿" in text
        or re.search(r"\bpass(?:ed|es|ing)?\b", lower)
        or re.search(r"\bok\b", lower)
    )
    negative = bool(
        "未通过" in text
        or "失败" in text
        or re.search(r"\bfail(?:ed|s|ing)?\b", lower)
    )
    return positive and not negative


def run_strategy(
    source: Path,
    run_root: Path,
    base_config: OpenRouterConfig,
    strategy: CompactionStrategy,
) -> tuple[TaskMetrics, dict]:
    workspace = run_root / strategy.value / "workspace"
    workspace.parent.mkdir(parents=True, exist_ok=False)
    shutil.copytree(
        source,
        workspace,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    initialize_git_baseline(workspace)
    session_root = run_root / strategy.value / "sessions"
    config = replace(
        base_config,
        context_budget=4_096,
        compaction_strategy=strategy,
    )
    agent = LiveAgent(
        OpenRouterProvider(config),
        ToolRegistry(workspace, command_runner=CommandRunner()),
        approval_handler=lambda request: False,
    )
    log = SessionLog(session_root)
    log.append(
        AgentEvent.create(
            EventType.SESSION_STARTED,
            log.session_id,
            {
                "workspace": str(workspace),
                "mode": agent.mode,
                "model": agent.model,
                "sandbox": agent.sandbox,
                "context_limit": agent.context_limit,
                "compaction_strategy": strategy.value,
            },
        )
    )
    before_tests = _test_hashes(workspace)
    started = time.perf_counter()
    for prompt in PRELUDE_PROMPTS:
        _run_turn(agent, log, prompt)
    for event in agent.compact_context(log.session_id):
        log.append(event)
    _run_turn(agent, log, FIX_PROMPT)
    elapsed = time.perf_counter() - started
    verification = _verify(workspace)
    after_tests = _test_hashes(workspace)
    events = log.load()
    assistant_texts = [
        str(event.payload.get("text", ""))
        for event in events
        if event.event_type is EventType.ASSISTANT_MESSAGE
    ]
    final_text = assistant_texts[-1] if assistant_texts else ""
    task_success = verification.returncode == 0
    claimed_success = claims_test_success(final_text)
    identifiers_found = sum(
        identifier.lower() in final_text.lower()
        for identifier in PROTECTED_IDENTIFIERS
    )
    tests_unchanged = before_tests == after_tests
    task = TaskMetrics(
        strategy=strategy.value,
        task_success=task_success,
        verification_exit_code=verification.returncode,
        tests_unchanged=tests_unchanged,
        constraint_retention=1.0 if tests_unchanged else 0.0,
        identifier_retention=round(
            identifiers_found / len(PROTECTED_IDENTIFIERS),
            4,
        ),
        test_evidence_accuracy=1.0 if claimed_success == task_success else 0.0,
        elapsed_seconds=round(elapsed, 3),
        session_log=str(log.path),
        workspace=str(workspace),
    )
    context = collect_context_metrics(events, strategy=strategy.value).to_dict()
    return task, context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("examples/buggy_average"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("tmp/context-benchmark"),
    )
    parser.add_argument(
        "--strategy",
        action="append",
        choices=[strategy.value for strategy in CompactionStrategy],
        help="strategy to run; repeat for multiple; default runs all",
    )
    args = parser.parse_args(argv)
    source = args.source.resolve()
    if not source.is_dir():
        parser.error(f"source workspace not found: {source}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = (args.output_root / f"run-{stamp}-{uuid4().hex[:8]}").resolve()
    run_root.mkdir(parents=True, exist_ok=False)
    strategies = [
        CompactionStrategy(value)
        for value in args.strategy
    ] if args.strategy else list(CompactionStrategy)
    config = OpenRouterConfig.from_env()
    results = []
    for strategy in strategies:
        print(f"running_strategy={strategy.value}", flush=True)
        task, context = run_strategy(source, run_root, config, strategy)
        results.append({"task": asdict(task), "context": context})
        print(
            f"completed_strategy={strategy.value} "
            f"task_success={task.task_success} "
            f"compression_ratio={context['compression_ratio']}",
            flush=True,
        )
    payload = {
        "scenario": "buggy_average_multiturn_v1",
        "model": config.model,
        "context_budget": 4_096,
        "strategies": [strategy.value for strategy in strategies],
        "results": results,
    }
    result_path = run_root / "results.json"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"results={result_path}")
    return 0 if all(item["task"]["task_success"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

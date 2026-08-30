from __future__ import annotations

import json
import unittest
from typing import Any

from code_agent.context import (
    ContextLayer,
    ContextManager,
    TokenCounter,
    WorkingMemory,
)


def _envelope(result: Any, ok: bool = True) -> str:
    return json.dumps({"ok": ok, "result": result}, ensure_ascii=False)


def _command_envelope(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    argv: list[str] | None = None,
    timed_out: bool = False,
) -> str:
    outcome = {
        "argv": argv or ["python", "-m", "unittest"],
        "cwd": ".",
        "sandbox": "workspace-policy",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": 12,
        "stdout": stdout,
        "stderr": stderr,
    }
    return _envelope(
        json.dumps(outcome, ensure_ascii=False),
        ok=exit_code == 0 and not timed_out,
    )


def _numbered_lines(count: int) -> str:
    return "\n".join(f"{index}: line body padding padding" for index in range(1, count + 1))


def _manager_with_tool_result(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    content: str,
    source_event_id: str | None = "event-original",
    call_id: str = "call-1",
) -> ContextManager:
    manager = ContextManager("SYSTEM RULES")
    manager.add_user("first task")
    manager.add_assistant(
        "",
        [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    )
    manager.add_tool(
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        content=content,
        source_event_id=source_event_id,
    )
    manager.add_user("second task")
    return manager


def _tool_content(manager: ContextManager) -> str:
    for message in manager.messages():
        if message["role"] == "tool":
            return message["content"]
    raise AssertionError("no tool message found")


class TokenCounterTests(unittest.TestCase):
    def test_empty_text_estimates_zero(self) -> None:
        self.assertEqual(TokenCounter.estimate(""), 0)
        self.assertEqual(TokenCounter.estimate(None), 0)

    def test_short_text_estimates_at_least_one(self) -> None:
        self.assertEqual(TokenCounter.estimate("a"), 1)
        self.assertEqual(TokenCounter.estimate("中"), 1)

    def test_wide_text_estimates_more_than_ascii(self) -> None:
        self.assertGreater(
            TokenCounter.estimate("汉" * 100),
            TokenCounter.estimate("a" * 100),
        )

    def test_estimate_is_deterministic(self) -> None:
        text = "mixed 中文 text"
        self.assertEqual(TokenCounter.estimate(text), TokenCounter.estimate(text))


class ContextProjectionTests(unittest.TestCase):
    def test_reasoning_details_are_preserved_in_assistant_messages(self) -> None:
        manager = ContextManager("SYS")
        details = [
            {
                "type": "reasoning.encrypted",
                "data": "opaque",
                "format": "google-gemini-v1",
                "index": 0,
            }
        ]
        manager.add_assistant("", None, details)

        rendered = manager.messages()
        self.assertEqual(rendered[-1]["reasoning_details"], details)
        rendered[-1]["reasoning_details"][0]["data"] = "changed"
        self.assertEqual(manager.messages()[-1]["reasoning_details"], details)

    def test_messages_match_legacy_chat_format(self) -> None:
        manager = ContextManager("SYS")
        manager.add_user("hello")
        manager.add_assistant(
            "",
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        )
        tool_content = _envelope("1: first line")
        manager.add_tool(
            call_id="c1",
            tool_name="read_file",
            arguments={"path": "a.py"},
            content=tool_content,
        )

        self.assertEqual(
            manager.messages(),
            [
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "a.py"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": tool_content},
            ],
        )

    def test_assistant_text_without_tool_calls_keeps_content(self) -> None:
        manager = ContextManager("SYS")
        manager.add_user("hello")
        manager.add_assistant("done", None)

        self.assertEqual(
            manager.messages()[-1],
            {"role": "assistant", "content": "done"},
        )

    def test_projection_returns_fresh_dicts(self) -> None:
        manager = ContextManager("SYS")
        manager.add_user("hello")
        manager.add_assistant(
            "",
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": "{}"},
                }
            ],
        )

        rendered = manager.messages()
        rendered[0]["content"] = "hacked"
        rendered[2]["tool_calls"][0]["id"] = "hacked"

        fresh = manager.messages()
        self.assertEqual(fresh[0]["content"], "SYS")
        self.assertEqual(fresh[2]["tool_calls"][0]["id"], "c1")

    def test_layer_stats_split_pinned_recent_evidence(self) -> None:
        manager = ContextManager("SYS")
        manager.add_user("first task")
        manager.add_tool(
            call_id="c1",
            tool_name="list_files",
            arguments={},
            content=_envelope("old evidence"),
        )
        manager.add_user("second task")

        stats = manager.layer_stats()
        self.assertEqual(stats["layers"]["pinned"]["items"], 1)
        self.assertEqual(stats["layers"]["recent"]["items"], 1)
        self.assertEqual(stats["layers"]["evidence"]["items"], 2)
        self.assertEqual(stats["items"], 4)


class DeterministicPruningTests(unittest.TestCase):
    def test_recent_tool_results_are_protected(self) -> None:
        manager = ContextManager("SYS")
        manager.add_user("only task")
        manager.add_tool(
            call_id="c1",
            tool_name="run_command",
            arguments={"argv": ["python", "-m", "unittest"]},
            content=_command_envelope(stdout="x" * 3000),
        )

        report = manager.prune(trigger="manual")

        self.assertFalse(report.changed)
        self.assertEqual(report.items_pruned, 0)

    def test_small_envelopes_are_never_pruned(self) -> None:
        manager = _manager_with_tool_result(
            tool_name="run_command",
            arguments={"argv": ["python", "-m", "unittest"]},
            content=_command_envelope(stdout="tiny"),
        )

        report = manager.prune(trigger="manual")

        self.assertFalse(report.changed)

    def test_successful_command_elides_output_but_keeps_metadata(self) -> None:
        stdout = "o" * 2500
        manager = _manager_with_tool_result(
            tool_name="run_command",
            arguments={"argv": ["python", "-m", "unittest"]},
            content=_command_envelope(stdout=stdout),
        )

        report = manager.prune(trigger="manual")

        self.assertTrue(report.changed)
        pruned = json.loads(_tool_content(manager))
        outcome = json.loads(pruned["result"])
        self.assertEqual(outcome["exit_code"], 0)
        self.assertEqual(outcome["argv"], ["python", "-m", "unittest"])
        self.assertIn(f"{len(stdout)} characters elided", outcome["stdout"])
        self.assertTrue(outcome["pruned"])
        self.assertEqual(outcome["event_id"], "event-original")
        self.assertIn("event-original", report.pruned_event_ids)

    def test_failed_command_keeps_stderr_tail(self) -> None:
        stderr = "e" * 3000 + "FINAL TRACEBACK LINE"
        manager = _manager_with_tool_result(
            tool_name="run_command",
            arguments={"argv": ["python", "-m", "unittest"]},
            content=_command_envelope(exit_code=1, stderr=stderr),
        )

        manager.prune(trigger="manual")

        outcome = json.loads(json.loads(_tool_content(manager))["result"])
        self.assertEqual(outcome["exit_code"], 1)
        self.assertTrue(outcome["stderr"].endswith("FINAL TRACEBACK LINE"))
        self.assertLess(len(outcome["stderr"]), len(stderr))

    def test_old_read_file_keeps_location_and_first_lines(self) -> None:
        manager = _manager_with_tool_result(
            tool_name="read_file",
            arguments={"path": "src/a.py", "start_line": 1, "end_line": 199},
            content=_envelope(_numbered_lines(180)),
        )

        report = manager.prune(trigger="manual")

        self.assertEqual(report.rules, {"read_file": 1})
        result = json.loads(_tool_content(manager))["result"]
        self.assertIn("path=src/a.py lines=1-199", result)
        self.assertIn("event-original", result)
        self.assertIn("1: line body padding padding", result)
        self.assertLess(len(result), len(_numbered_lines(180)))

    def test_read_file_pruning_preserves_sha256_line(self) -> None:
        digest = "b" * 64
        content = f"sha256: {digest}\n" + _numbered_lines(180)
        manager = _manager_with_tool_result(
            tool_name="read_file",
            arguments={"path": "src/a.py", "start_line": 1, "end_line": 180},
            content=_envelope(content),
        )

        manager.prune(trigger="manual")

        result = json.loads(_tool_content(manager))["result"]
        self.assertIn(f"sha256: {digest}", result)

    def test_list_files_result_keeps_entry_count(self) -> None:
        entries = [{"path": f"file_{index}.py", "type": "file"} for index in range(60)]
        manager = _manager_with_tool_result(
            tool_name="list_files",
            arguments={"path": ".", "depth": 2},
            content=_envelope(json.dumps({"entries": entries, "truncated": False})),
        )

        manager.prune(trigger="manual")

        result = json.loads(json.loads(_tool_content(manager))["result"])
        self.assertEqual(result["entries"], 60)
        self.assertTrue(result["pruned"])
        self.assertEqual(result["event_id"], "event-original")

    def test_search_text_result_keeps_match_count(self) -> None:
        matches = [
            {"path": f"src/file_{index}.py", "line": index + 1, "snippet": "s" * 200}
            for index in range(60)
        ]
        manager = _manager_with_tool_result(
            tool_name="search_text",
            arguments={"query": "needle"},
            content=_envelope(
                json.dumps(
                    {
                        "query": "needle",
                        "path": ".",
                        "engine": "python",
                        "files_searched": 3,
                        "returned": 60,
                        "truncated": False,
                        "matches": matches,
                    }
                )
            ),
        )

        report = manager.prune(trigger="manual")

        result = json.loads(json.loads(_tool_content(manager))["result"])
        self.assertEqual(result["match_count"], 60)
        self.assertEqual(result["query"], "needle")
        self.assertEqual(result["engine"], "python")
        self.assertEqual(result["files_searched"], 3)
        self.assertFalse(result["truncated"])
        self.assertTrue(result["pruned"])
        self.assertEqual(result["event_id"], "event-original")
        self.assertNotIn("matches", result)
        self.assertEqual(report.rules, {"search_text": 1})

    def test_git_read_keeps_stdout_head(self) -> None:
        stdout = "## main...origin/main\n" + "M code_agent/tools.py\n" * 200
        manager = _manager_with_tool_result(
            tool_name="git_status",
            arguments={},
            content=_command_envelope(stdout=stdout, argv=["git", "status"]),
        )

        manager.prune(trigger="manual")

        outcome = json.loads(json.loads(_tool_content(manager))["result"])
        self.assertTrue(outcome["stdout"].startswith("## main...origin/main"))
        self.assertLess(len(outcome["stdout"]), len(stdout))

    def test_fetch_url_keeps_metadata_and_truncates_body(self) -> None:
        body = "b" * 2000
        fetch_result = json.dumps(
            {
                "url": "https://example.com/doc",
                "status": 200,
                "content_type": "text/plain",
                "bytes": 2000,
                "body": body,
            },
            ensure_ascii=False,
        )
        manager = _manager_with_tool_result(
            tool_name="fetch_url",
            arguments={"url": "https://example.com/doc"},
            content=_envelope(fetch_result),
        )

        manager.prune(trigger="manual")

        result = json.loads(json.loads(_tool_content(manager))["result"])
        self.assertEqual(result["url"], "https://example.com/doc")
        self.assertEqual(result["status"], 200)
        self.assertLess(len(result["body"]), len(body))
        self.assertTrue(result["pruned"])

    def test_unknown_json_result_truncates_long_strings(self) -> None:
        manager = _manager_with_tool_result(
            tool_name="custom_tool",
            arguments={"query": "x"},
            content=_envelope(json.dumps({"note": "n" * 900, "count": 3})),
        )

        manager.prune(trigger="manual")

        result = json.loads(json.loads(_tool_content(manager))["result"])
        self.assertEqual(result["count"], 3)
        self.assertTrue(result["pruned"])
        self.assertLess(len(result["note"]), 900)

    def test_duplicate_calls_keep_only_newest_raw_result(self) -> None:
        manager = ContextManager("SYS")
        manager.add_user("first task")
        listing = _envelope(
            json.dumps(
                {
                    "entries": [
                        {"path": f"file_{index}.py", "type": "file"}
                        for index in range(40)
                    ],
                    "truncated": False,
                }
            )
        )
        manager.add_tool(
            call_id="c1",
            tool_name="list_files",
            arguments={"path": ".", "depth": 2},
            content=listing,
            source_event_id="event-old",
        )
        manager.add_tool(
            call_id="c2",
            tool_name="list_files",
            arguments={"path": ".", "depth": 2},
            content=listing,
            source_event_id="event-new",
        )
        manager.add_user("second task")

        report = manager.prune(trigger="manual")

        self.assertGreaterEqual(report.rules.get("duplicate", 0), 1)
        messages = [message for message in manager.messages() if message["role"] == "tool"]
        self.assertIn("[duplicate list_files result", messages[0]["content"])
        self.assertIn("event-old", messages[0]["content"])
        self.assertNotIn("[duplicate", messages[1]["content"])

    def test_tool_pairing_survives_pruning(self) -> None:
        manager = _manager_with_tool_result(
            tool_name="read_file",
            arguments={"path": "src/a.py"},
            content=_envelope(_numbered_lines(180)),
        )

        manager.prune(trigger="manual")

        messages = manager.messages()
        assistant = next(message for message in messages if message["role"] == "assistant")
        tool = next(message for message in messages if message["role"] == "tool")
        call_ids = {call["id"] for call in assistant["tool_calls"]}
        self.assertEqual(call_ids, {tool["tool_call_id"]})

    def test_second_prune_is_no_op(self) -> None:
        manager = _manager_with_tool_result(
            tool_name="read_file",
            arguments={"path": "src/a.py"},
            content=_envelope(_numbered_lines(180)),
        )

        first = manager.prune(trigger="manual")
        second = manager.prune(trigger="manual")

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(second.items_pruned, 0)
        self.assertEqual(second.before_tokens, second.after_tokens)

    def test_prune_reduces_estimated_tokens(self) -> None:
        manager = _manager_with_tool_result(
            tool_name="read_file",
            arguments={"path": "src/a.py"},
            content=_envelope(_numbered_lines(180)),
        )

        report = manager.prune(trigger="manual")

        self.assertLess(report.after_tokens, report.before_tokens)
        self.assertEqual(manager.estimated_tokens, report.after_tokens)

    def test_clear_restores_system_prompt_only(self) -> None:
        manager = _manager_with_tool_result(
            tool_name="read_file",
            arguments={"path": "src/a.py"},
            content=_envelope(_numbered_lines(180)),
        )

        manager.clear()

        self.assertEqual(manager.messages(), [{"role": "system", "content": "SYSTEM RULES"}])
        self.assertEqual(manager.item_count, 1)


class WorkingMemoryTests(unittest.TestCase):
    def test_records_modified_files_and_verified_commands(self) -> None:
        manager = ContextManager("SYS")
        manager.add_user("task")
        manager.add_tool(
            call_id="c1",
            tool_name="replace_in_file",
            arguments={"path": "calc.py"},
            content=_envelope(
                json.dumps(
                    {
                        "path": "calc.py",
                        "old_sha256": "a" * 64,
                        "new_sha256": "b" * 64,
                        "changed": True,
                    }
                )
            ),
        )
        manager.add_tool(
            call_id="c2",
            tool_name="run_command",
            arguments={"argv": ["python", "-m", "unittest"]},
            content=_command_envelope(),
        )

        manager.prune(trigger="manual")

        memory = manager.working_memory
        self.assertEqual(memory.modified_files, {"calc.py": "b" * 64})
        self.assertEqual(
            memory.verified_commands,
            [{"argv": ["python", "-m", "unittest"], "exit_code": 0}],
        )
        self.assertEqual(memory.open_errors, [])

    def test_failed_then_successful_command_resolves_error(self) -> None:
        memory = WorkingMemory()
        argv = ["python", "-m", "unittest"]
        memory.record("run_command", False, {"argv": argv, "exit_code": 1})
        self.assertEqual(len(memory.open_errors), 1)

        memory.record("run_command", True, {"argv": argv, "exit_code": 0})

        self.assertEqual(memory.open_errors, [])
        self.assertEqual(memory.verified_commands[-1]["exit_code"], 0)
        self.assertEqual(len(memory.verified_commands), 1)

    def test_non_command_failures_are_recorded_once(self) -> None:
        memory = WorkingMemory()
        memory.record("read_file", False, "reading sensitive credential files is denied")
        memory.record("read_file", False, "reading sensitive credential files is denied")

        self.assertEqual(len(memory.open_errors), 1)


if __name__ == "__main__":
    unittest.main()

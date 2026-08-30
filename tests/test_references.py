from __future__ import annotations

import unittest

from code_agent.events import AgentEvent, EventType
from code_agent.references import ReferenceError, WorkspaceReferences
from code_agent.session import SessionLog
from tests.helpers import test_directory


class WorkspaceReferenceTests(unittest.TestCase):
    def test_file_suggestions_skip_sensitive_and_ignored_paths(self) -> None:
        with test_directory() as workspace:
            (workspace / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (workspace / ".env").write_text("SECRET=value\n", encoding="utf-8")
            (workspace / ".env.example").write_text("SECRET=\n", encoding="utf-8")
            (workspace / "tmp").mkdir()
            (workspace / "tmp" / "hidden.py").write_text("x=1\n", encoding="utf-8")
            references = WorkspaceReferences(workspace)

            suggestions = references.suggest("@", "")

        values = [item["value"] for item in suggestions]
        self.assertIn("app.py", values)
        self.assertIn(".env.example", values)
        self.assertNotIn(".env", values)
        self.assertNotIn("tmp/hidden.py", values)

    def test_reference_validation_rejects_escape_and_sensitive_file(self) -> None:
        with test_directory() as workspace:
            (workspace / ".env").write_text("SECRET=value\n", encoding="utf-8")
            references = WorkspaceReferences(workspace)
            for value in (".env", "../outside.py", "missing.py"):
                with self.subTest(value=value):
                    with self.assertRaises(ReferenceError):
                        references.normalize([{"kind": "file", "value": value}])

    def test_context_suggestions_are_fixed_and_searchable(self) -> None:
        with test_directory() as workspace:
            references = WorkspaceReferences(workspace)
            suggestions = references.suggest("#", "git")

        self.assertEqual(
            [item["value"] for item in suggestions],
            ["git-status", "git-diff"],
        )

    def test_composed_prompt_keeps_file_reference_as_path_only(self) -> None:
        with test_directory() as workspace:
            source = workspace / "app.py"
            source.write_text("PRIVATE_SOURCE_BODY\n", encoding="utf-8")
            log = SessionLog(workspace / "sessions")
            references = WorkspaceReferences(workspace)
            normalized = references.normalize(
                [{"kind": "file", "value": "app.py"}]
            )

            prompt = references.compose_model_text(
                "检查这个文件",
                normalized,
                context_stats={},
                log=log,
            )

        self.assertIn("@app.py", prompt)
        self.assertIn("read_file", prompt)
        self.assertNotIn("PRIVATE_SOURCE_BODY", prompt)

    def test_event_reference_removes_raw_reasoning_details(self) -> None:
        with test_directory() as workspace:
            log = SessionLog(workspace / "sessions")
            event = AgentEvent.create(
                EventType.ASSISTANT_MESSAGE,
                log.session_id,
                {
                    "text": "done",
                    "reasoning": "raw",
                    "reasoning_details": [{"data": "opaque"}],
                },
            )
            log.append(event)
            references = WorkspaceReferences(workspace)

            prompt = references.compose_model_text(
                "继续",
                [{"kind": "context", "value": f"event:{event.event_id}"}],
                context_stats={},
                log=log,
            )

        self.assertIn(event.event_id, prompt)
        self.assertNotIn("opaque", prompt)
        self.assertNotIn('"reasoning"', prompt)


if __name__ == "__main__":
    unittest.main()

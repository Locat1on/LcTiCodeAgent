"""Safe workspace and evidence references used by the Web composer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .events import EventType
from .git_tools import GitInspector, GitToolError
from .session import SessionLog
from .tools import IGNORED_DIRECTORIES, is_sensitive_path


MAX_REFERENCES = 12
MAX_INDEXED_FILES = 5_000
MAX_REFERENCE_CHARS = 32_000

CONTEXT_REFERENCES = (
    ("git-status", "当前分支与工作区状态"),
    ("git-diff", "当前未提交代码差异"),
    ("context", "上下文分层与工作记忆统计"),
    ("session", "当前会话与最近事件摘要"),
    ("event:", "按 event_id 回溯一条原始事件"),
)


class ReferenceError(ValueError):
    pass


class WorkspaceReferences:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self._files: list[str] | None = None

    def refresh(self) -> None:
        self._files = None

    def suggest(self, trigger: str, query: str) -> list[dict[str, str]]:
        normalized = query.strip().lower()
        if trigger == "@":
            return self._suggest_files(normalized)
        if trigger == "#":
            return [
                {
                    "kind": "context",
                    "value": value,
                    "label": f"#{value}" if value != "event:" else "#event:<id>",
                    "description": description,
                }
                for value, description in CONTEXT_REFERENCES
                if normalized in value.lower()
                or normalized in description.lower()
            ][:20]
        raise ReferenceError("suggest trigger must be @ or #")

    def normalize(self, references: Any) -> list[dict[str, str]]:
        if references is None:
            return []
        if not isinstance(references, list) or len(references) > MAX_REFERENCES:
            raise ReferenceError(
                f"references must be an array of at most {MAX_REFERENCES} items"
            )
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        context_values = {value for value, _ in CONTEXT_REFERENCES}
        for item in references:
            if not isinstance(item, dict):
                raise ReferenceError("each reference must be an object")
            kind = item.get("kind")
            value = item.get("value")
            if not isinstance(kind, str) or not isinstance(value, str):
                raise ReferenceError("reference kind and value must be strings")
            value = value.strip()
            if kind == "file":
                value = self._validate_file(value)
            elif kind == "context":
                if value.startswith("event:"):
                    if not value.removeprefix("event:").strip():
                        raise ReferenceError("#event requires an event_id")
                elif value not in context_values:
                    raise ReferenceError(f"unsupported context reference: {value}")
            else:
                raise ReferenceError(f"unsupported reference kind: {kind}")
            key = (kind, value)
            if key not in seen:
                normalized.append({"kind": kind, "value": value})
                seen.add(key)
        return normalized

    def compose_model_text(
        self,
        text: str,
        references: list[dict[str, str]],
        *,
        context_stats: dict[str, Any],
        log: SessionLog,
    ) -> str:
        if not references:
            return text
        sections: list[str] = []
        for reference in references:
            kind = reference["kind"]
            value = reference["value"]
            if kind == "file":
                sections.append(
                    f"@{value}\nWorkspace file selected by the user. "
                    "Inspect it with read_file before relying on its contents."
                )
                continue
            evidence = self._context_evidence(value, context_stats, log)
            sections.append(f"#{value}\n{evidence}")
        attached = "\n\n".join(sections)
        if len(attached) > MAX_REFERENCE_CHARS:
            attached = attached[:MAX_REFERENCE_CHARS] + "\n[references truncated]"
        return f"{text}\n\n[User-selected references]\n{attached}"

    def _suggest_files(self, query: str) -> list[dict[str, str]]:
        if self._files is None:
            self._files = self._index_files()
        candidates = [path for path in self._files if query in path.lower()]
        candidates.sort(
            key=lambda path: (
                0 if Path(path).name.lower().startswith(query) else 1,
                len(path),
                path,
            )
        )
        return [
            {
                "kind": "file",
                "value": path,
                "label": f"@{path}",
                "description": "工作区文件",
            }
            for path in candidates[:20]
        ]

    def _index_files(self) -> list[str]:
        paths: list[str] = []
        for current, directories, files in os.walk(self.workspace):
            current_path = Path(current)
            directories[:] = sorted(
                name
                for name in directories
                if name not in IGNORED_DIRECTORIES
                and not name.endswith(".egg-info")
            )
            for name in sorted(files):
                path = current_path / name
                if is_sensitive_path(self.workspace, path):
                    continue
                paths.append(path.relative_to(self.workspace).as_posix())
                if len(paths) >= MAX_INDEXED_FILES:
                    return paths
        return paths

    def _validate_file(self, value: str) -> str:
        if not value:
            raise ReferenceError("file reference must not be empty")
        path = (self.workspace / value).resolve()
        try:
            relative = path.relative_to(self.workspace)
        except ValueError as error:
            raise ReferenceError("file reference resolves outside workspace") from error
        if not path.is_file():
            raise ReferenceError("file reference is not an existing file")
        if is_sensitive_path(self.workspace, path):
            raise ReferenceError("sensitive files cannot be referenced")
        return relative.as_posix()

    def _context_evidence(
        self,
        value: str,
        context_stats: dict[str, Any],
        log: SessionLog,
    ) -> str:
        if value == "context":
            return json.dumps(context_stats, ensure_ascii=False, sort_keys=True)
        if value == "session":
            events = log.load()
            recent = [event.event_type.value for event in events[-20:]]
            return json.dumps(
                {
                    "session_id": log.session_id,
                    "event_count": len(events),
                    "recent_event_types": recent,
                },
                ensure_ascii=False,
            )
        if value.startswith("event:"):
            event_id = value.removeprefix("event:").strip()
            event = log.recall(event_id)
            if event is None:
                raise ReferenceError("referenced event_id was not found")
            data = event.to_dict()
            if event.event_type is EventType.ASSISTANT_MESSAGE:
                data["payload"].pop("reasoning", None)
                data["payload"].pop("reasoning_details", None)
            return json.dumps(data, ensure_ascii=False, sort_keys=True)
        inspector = GitInspector(self.workspace)
        try:
            result = (
                inspector.status()
                if value == "git-status"
                else inspector.diff()
                if value == "git-diff"
                else None
            )
        except GitToolError as error:
            raise ReferenceError(str(error)) from error
        if result is None:
            raise ReferenceError(f"unsupported context reference: {value}")
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

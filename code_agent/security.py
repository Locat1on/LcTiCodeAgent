"""Capability decisions and approval requests for model-facing tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable
from uuid import uuid4


class RiskClass(StrEnum):
    READ = "read"
    LOCAL_WRITE = "local_write"
    VERIFY = "verify"
    NETWORK = "network"
    REPOSITORY_WRITE = "repository_write"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    risk: RiskClass
    decision: PermissionDecision
    reason: str


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    tool_name: str
    risk: RiskClass
    reason: str
    arguments: dict[str, Any]
    context: dict[str, Any]

    @classmethod
    def create(
        cls,
        tool_name: str,
        rule: PermissionRule,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ApprovalRequest:
        return cls(
            request_id=str(uuid4()),
            tool_name=tool_name,
            risk=rule.risk,
            reason=rule.reason,
            arguments=arguments,
            context=context,
        )


ApprovalHandler = Callable[[ApprovalRequest], bool]


class PermissionPolicy:
    def __init__(self) -> None:
        self._rules = {
            "list_files": PermissionRule(
                RiskClass.READ,
                PermissionDecision.ALLOW,
                "read-only workspace inspection",
            ),
            "read_file": PermissionRule(
                RiskClass.READ,
                PermissionDecision.ALLOW,
                "read-only workspace inspection",
            ),
            "replace_in_file": PermissionRule(
                RiskClass.LOCAL_WRITE,
                PermissionDecision.ALLOW,
                "bounded atomic edit inside the workspace",
            ),
            "write_file": PermissionRule(
                RiskClass.LOCAL_WRITE,
                PermissionDecision.ALLOW,
                "create-only atomic write inside the workspace",
            ),
            "run_command": PermissionRule(
                RiskClass.VERIFY,
                PermissionDecision.ALLOW,
                "structured verification command from the allowlist",
            ),
            "fetch_url": PermissionRule(
                RiskClass.NETWORK,
                PermissionDecision.ASK,
                "outbound HTTPS request leaves the local workspace",
            ),
            "git_status": PermissionRule(
                RiskClass.READ,
                PermissionDecision.ALLOW,
                "read-only Git inspection",
            ),
            "git_diff": PermissionRule(
                RiskClass.READ,
                PermissionDecision.ALLOW,
                "read-only Git inspection",
            ),
            "git_log": PermissionRule(
                RiskClass.READ,
                PermissionDecision.ALLOW,
                "read-only Git inspection",
            ),
            "git_commit": PermissionRule(
                RiskClass.REPOSITORY_WRITE,
                PermissionDecision.ASK,
                "creating a commit changes repository history",
            ),
            "git_push": PermissionRule(
                RiskClass.EXTERNAL_WRITE,
                PermissionDecision.ASK,
                "pushing changes an external repository",
            ),
        }

    def evaluate(self, tool_name: str) -> PermissionRule:
        return self._rules.get(
            tool_name,
            PermissionRule(
                RiskClass.UNKNOWN,
                PermissionDecision.DENY,
                "tool is not registered in the capability policy",
            ),
        )


def deny_approval(request: ApprovalRequest) -> bool:
    return False

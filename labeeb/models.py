"""Core domain models, dataclasses, and format helpers."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import uuid
from typing import Any

VERSION = "1.0.0"
TERMINAL_PHASES = {"PASS", "FAIL", "BLOCKED"}
JULES_WAIT_STATES = {"QUEUED", "PLANNING", "IN_PROGRESS"}
JULES_TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}
DECISION_START = "LABEEB_DECISION_JSON"
DECISION_END = "END_LABEEB_DECISION_JSON"
CRITIC_START = "LABEEB_CRITIC_JSON"
CRITIC_END = "END_LABEEB_CRITIC_JSON"


class ArtifactValidity:
    VALID = "VALID"
    STALE = "STALE"


class ArtifactStatus:
    SATISFIED = "SATISFIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NEEDS_WORK = "NEEDS_WORK"
    BLOCKED = "BLOCKED"


@dataclasses.dataclass
class ArtifactMeta:
    artifact_type: str
    version: int
    validity: str = ArtifactValidity.VALID
    activity_status: str = ArtifactStatus.SATISFIED
    producer: str = ""
    created_at: str = ""
    dependencies: dict[str, int] = dataclasses.field(default_factory=dict)
    evidence_refs: list[str] = dataclasses.field(default_factory=list)
    not_applicable_reason: str | None = None
    data: dict[str, Any] = dataclasses.field(default_factory=dict)
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "version": self.version,
            "validity": self.validity,
            "activity_status": self.activity_status,
            "producer": self.producer,
            "created_at": self.created_at,
            "dependencies": self.dependencies,
            "evidence_refs": self.evidence_refs,
            "not_applicable_reason": self.not_applicable_reason,
            "data": self.data,
            "sha256": self.sha256,
        }


@dataclasses.dataclass
class GoalArtifactIndexEntry:
    ref: str
    version: int
    validity: str
    activity_status: str
    sha256: str
    updated_at: str
    not_applicable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        res = {
            "ref": self.ref,
            "version": self.version,
            "validity": self.validity,
            "activity_status": self.activity_status,
            "sha256": self.sha256,
            "updated_at": self.updated_at,
        }
        if self.not_applicable_reason is not None:
            res["not_applicable_reason"] = self.not_applicable_reason
        return res


class ReasoningActivity:
    AUTHORITY_CONTEXT = "authority_context"
    GOAL_CONTRACT = "goal_contract"
    PRODUCT_VALIDATION = "product_validation"
    PROOF_CONTRACT = "proof_contract"
    REALITY_AUDIT = "reality_audit"
    MUTATION_PREFLIGHT = "mutation_preflight"
    BASELINE = "baseline"
    DIAGNOSIS = "diagnosis"
    SOLUTION_EXPLORATION = "solution_exploration"
    SECOND_REALITY_AUDIT = "second_reality_audit"
    DELIVERY_READINESS = "delivery_readiness"
    INDEPENDENT_CRITIQUE = "independent_critique"
    CONVERGENCE = "convergence"
    CHANGE_AUTHORITY = "change_authority"
    IMPLEMENTATION_READINESS = "implementation_readiness"
    EXECUTION_CONTRACT = "execution_contract"


ALL_REASONING_ACTIVITIES: list[str] = [
    ReasoningActivity.AUTHORITY_CONTEXT,
    ReasoningActivity.GOAL_CONTRACT,
    ReasoningActivity.PRODUCT_VALIDATION,
    ReasoningActivity.PROOF_CONTRACT,
    ReasoningActivity.REALITY_AUDIT,
    ReasoningActivity.MUTATION_PREFLIGHT,
    ReasoningActivity.BASELINE,
    ReasoningActivity.DIAGNOSIS,
    ReasoningActivity.SOLUTION_EXPLORATION,
    ReasoningActivity.SECOND_REALITY_AUDIT,
    ReasoningActivity.DELIVERY_READINESS,
    ReasoningActivity.INDEPENDENT_CRITIQUE,
    ReasoningActivity.CONVERGENCE,
    ReasoningActivity.CHANGE_AUTHORITY,
    ReasoningActivity.IMPLEMENTATION_READINESS,
    ReasoningActivity.EXECUTION_CONTRACT,
]


class ReasoningDecisionAction:
    CONTINUE_REASONING = "CONTINUE_REASONING"
    IMPLEMENTATION_READY = "IMPLEMENTATION_READY"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    RETURN_TO_THINKING = "RETURN_TO_THINKING"
    TARGETED_REPAIR = "TARGETED_REPAIR"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class PathIntegrityStatus:
    ORIGINAL = "ORIGINAL"
    RECOMPILED_BEFORE_BASELINE = "RECOMPILED_BEFORE_BASELINE"
    ALTERNATE_DIAGNOSTIC_ONLY = "ALTERNATE_DIAGNOSTIC_ONLY"


class ChangeAuthorityLevel:
    LOCAL = "LOCAL"
    GATED = "GATED"
    INCIDENTAL = "INCIDENTAL"
    PRODUCTION = "PRODUCTION"


@dataclasses.dataclass
class HumanCheckpoint:
    needed: bool = False
    boundary_type: str | None = None
    question: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "needed": self.needed,
            "boundary_type": self.boundary_type,
            "question": self.question,
        }


@dataclasses.dataclass
class ReasoningDecision:
    decision: str
    current_activity: str
    activity_status: str = ArtifactStatus.SATISFIED
    next_activity: str | None = None
    reason: str = ""
    not_applicable_reason: str | None = None
    produced_artifact: dict[str, Any] | None = None
    invalidate_roots: list[str] = dataclasses.field(default_factory=list)
    evidence_refs: list[str] = dataclasses.field(default_factory=list)
    human_checkpoint: HumanCheckpoint = dataclasses.field(default_factory=HumanCheckpoint)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "current_activity": self.current_activity,
            "activity_status": self.activity_status,
            "next_activity": self.next_activity,
            "reason": self.reason,
            "not_applicable_reason": self.not_applicable_reason,
            "produced_artifact": self.produced_artifact,
            "invalidate_roots": self.invalidate_roots,
            "evidence_refs": self.evidence_refs,
            "human_checkpoint": (
                self.human_checkpoint.to_dict()
                if isinstance(self.human_checkpoint, HumanCheckpoint)
                else self.human_checkpoint
            ),
        }


@dataclasses.dataclass
class CmdResult:
    cmd: list[str]
    rc: int
    stdout: str
    stderr: str


@dataclasses.dataclass
class DomainEvent:
    event_type: str
    goal_id: str
    timestamp: str
    data: dict[str, Any] = dataclasses.field(default_factory=dict)
    event_id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "goal_id": self.goal_id,
            "timestamp": self.timestamp,
            "data": self.data,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def deadline_after(hours: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def safe_name(value: str, limit: int = 90) -> str:
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value).strip("-")
    return value[:limit]


def new_operation_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def task_name(goal_id: str, role: str, op_id: str) -> str:
    return safe_name(f"labeeb-{goal_id[:8]}-{role}-{op_id}")


def format_json_for_prompt(value: Any, max_chars: int = 50000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    if len(text) > max_chars:
        return text[:max_chars] + "\n...TRUNCATED..."
    return text

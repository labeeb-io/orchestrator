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

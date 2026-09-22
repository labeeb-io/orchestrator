"""Event classification, event deduplication, and async pub/sub for SSE streaming."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
from typing import Any

from labeeb.config import Config
from labeeb.errors import ControllerError
from labeeb.models import (
    DomainEvent,
    canonical_json,
    sha256_text,
    utc_now,
)
from labeeb.providers.jules import (
    activity_key,
    activity_text,
    latest_plan,
    ordered_activities,
    patch_candidates,
)
from labeeb.storage.goal_store import read_ref_json


def jules_snapshot(logs: dict[str, Any]) -> dict[str, Any]:
    raw_activities = [x for x in (logs.get("activities") or []) if isinstance(x, dict)]
    activities = ordered_activities(raw_activities)
    keys = sorted(activity_key(a) for a in activities)
    patches = patch_candidates(activities)
    patch_hashes = sorted(sha256_text(p["patch"]) for p in patches)
    return {
        "activity_keys": keys,
        "activity_count": len(keys),
        "latest_activity_time": max([str(a.get("createTime") or "") for a in activities] or [""]),
        "patch_hashes": patch_hashes,
        "captured_at": utc_now(),
    }


def has_repair_causality(
    new_activities: list[dict[str, Any]],
    marker: str,
    anchor: dict[str, Any],
) -> bool:
    if not marker:
        return True
    user_msg_idx = -1
    for idx, a in enumerate(new_activities):
        if isinstance(a.get("userMessaged"), dict) and marker in activity_text(a):
            user_msg_idx = idx
            break
    if user_msg_idx == -1:
        return False

    agent_msg_seen = any(
        isinstance(a.get("agentMessaged"), dict) and marker in activity_text(a)
        for a in new_activities[user_msg_idx + 1:]
    )

    anchor_patches = set(anchor.get("patch_hashes") or [])
    new_patch_hashes = [
        sha256_text(p["patch"])
        for p in patch_candidates(new_activities[user_msg_idx + 1:])
        if sha256_text(p["patch"]) not in anchor_patches
    ]
    new_patch_seen = bool(new_patch_hashes)

    return bool(agent_msg_seen or new_patch_seen)


def meaningful_event(
    state: dict[str, Any],
    session: dict[str, Any],
    logs: dict[str, Any],
    config: Config,
) -> tuple[str, dict[str, Any]] | None:
    jstate = str(session.get("state") or "UNKNOWN")
    raw_activities = [x for x in (logs.get("activities") or []) if isinstance(x, dict)]
    activities = ordered_activities(raw_activities)
    handled = set(state.get("handled_event_keys") or [])
    action = str(
        config.get(
            f"jules_state_actions.{jstate}",
            config.get("jules_state_actions.UNKNOWN", "block"),
        )
    ).lower()
    if action not in {"wait", "wake", "review", "block"}:
        raise ControllerError(f"Invalid jules_state_actions.{jstate}={action}")
    if action == "wait":
        return None
    if action == "block":
        key = "jules-policy-block:" + sha256_text(canonical_json({"state": jstate, "update": session.get("updateTime")}))
        return None if key in handled else (key, {"type": "POLICY_BLOCK", "state": jstate})
    if jstate == "AWAITING_PLAN_APPROVAL":
        plan = latest_plan(activities) or {}
        key = "jules-plan:" + sha256_text(canonical_json(plan))
        return None if key in handled else (key, {"type": jstate, "plan": plan})
    if jstate == "AWAITING_USER_FEEDBACK":
        last = activities[-1] if activities else {}
        key = "jules-feedback:" + activity_key(last)
        return None if key in handled else (key, {"type": jstate, "latest_activity": last})
    if jstate == "PAUSED":
        key = "jules-paused:" + str(session.get("updateTime") or sha256_text(canonical_json(session)))
        return None if key in handled else (key, {"type": jstate})
    if jstate in {"FAILED", "CANCELLED"}:
        key = f"jules-terminal:{jstate}:" + str(session.get("updateTime") or "")
        return None if key in handled else (key, {"type": jstate})
    if jstate == "COMPLETED" and action == "review":
        if state.get("repair_reserved") and state.get("round_anchor_ref"):
            anchor = read_ref_json(state["round_anchor_ref"])
            old = set(anchor.get("activity_keys") or [])
            new_activities = [a for a in activities if activity_key(a) not in old]
            marker = str(anchor.get("repair_marker") or "")
            if not has_repair_causality(new_activities, marker, anchor):
                return None
            new_keys = sorted(activity_key(a) for a in new_activities)
            if not new_keys:
                return None
            key = "jules-completed-repair:" + sha256_text(canonical_json(new_keys))
            return None if key in handled else (key, {"type": "COMPLETED", "round": "repair", "new_activity_keys": new_keys})
        snapshot = jules_snapshot(logs)
        key = "jules-completed-initial:" + sha256_text(canonical_json(snapshot))
        return None if key in handled else (key, {"type": "COMPLETED", "round": "initial"})
    if action == "review":
        key = "jules-invalid-review:" + sha256_text(canonical_json({"state": jstate, "update": session.get("updateTime")}))
        return None if key in handled else (key, {"type": "POLICY_BLOCK", "state": jstate, "reason": "review action on non-COMPLETED state"})
    key = "jules-wake:" + sha256_text(canonical_json({"state": jstate, "update": session.get("updateTime"), "last": activity_key(activities[-1]) if activities else "none"}))
    return None if key in handled else (key, {"type": jstate, "state": jstate})


def reserve_event_key(state: dict[str, Any], key: str, config: Config) -> None:
    handled = list(state.get("handled_event_keys") or [])
    if key not in handled:
        handled.append(key)
    max_keys = int(config.get("controller.max_handled_event_keys", 100))
    state["handled_event_keys"] = handled[-max_keys:]


class EventBroadcaster:
    """In-memory event broker to push real-time updates to SSE consumers."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue[DomainEvent]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, goal_id: str) -> asyncio.Queue[DomainEvent]:
        queue: asyncio.Queue[DomainEvent] = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(goal_id, []).append(queue)
        return queue

    async def unsubscribe(self, goal_id: str, queue: asyncio.Queue[DomainEvent]) -> None:
        async with self._lock:
            if goal_id in self._subscribers:
                self._subscribers[goal_id] = [q for q in self._subscribers[goal_id] if q is not queue]
                if not self._subscribers[goal_id]:
                    del self._subscribers[goal_id]

    async def publish(self, event: DomainEvent) -> None:
        async with self._lock:
            subscribers = list(self._subscribers.get(event.goal_id, []))
            # Also notify wildcard subscribers if any
            subscribers.extend(self._subscribers.get("*", []))
        for q in subscribers:
            await q.put(event)

    def publish_sync(self, event: DomainEvent) -> None:
        """Helper to publish from synchronous controller threads if an event loop is running."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.publish(event))
        except RuntimeError:
            pass


# Global singleton instance for the process
global_event_bus = EventBroadcaster()


def append_domain_event(events_file: pathlib.Path, event: DomainEvent) -> None:
    """Durably append a DomainEvent to the goal's events.jsonl file with fsync."""
    line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def read_domain_events(events_file: pathlib.Path, after_line: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read all events from events_file starting after the given line offset.

    Returns (events, total_lines_read).
    """
    if not events_file.exists():
        return [], 0
    events: list[dict[str, Any]] = []
    lines_count = 0
    try:
        with events_file.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                lines_count = i + 1
                if i >= after_line:
                    line_str = line.strip()
                    if line_str:
                        with contextlib.suppress(Exception):
                            events.append(json.loads(line_str))
    except Exception:
        pass
    return events, lines_count

"""Repair protocol, anchor reservation, timeout detection, and fallback handling.

Adheres to Single Responsibility Principle (SRP) by isolating Jules repair lifecycle
operations from LabeebController.
"""
from __future__ import annotations

import datetime as dt
import textwrap
from typing import TYPE_CHECKING, Any

from labeeb.core.events import jules_snapshot
from labeeb.models import new_operation_id, parse_utc, safe_name, utc_now
from labeeb.providers.jules import (
    activity_key,
    activity_text,
    ordered_activities,
    patch_candidates,
)
from labeeb.storage.goal_store import read_ref_json, read_ref_text

if TYPE_CHECKING:
    from labeeb.core.controller import LabeebController


def reserve_and_send_repair(ctl: LabeebController, state: dict[str, Any], message: str) -> None:
    """Reserve repair anchor and send targeted repair message to Jules."""
    if state.get("repair_reserved"):
        ctl.fail(state, "Repair already reserved")
        return
    sid = state.get("jules_session_id")
    if not sid:
        ctl.block(state, "No Jules session for repair")
        return
    logs = ctl.jules.get_logs(sid)
    if not logs:
        ctl.block(state, "Unable to snapshot Jules activities before repair")
        return
    op_id = new_operation_id("repair")
    marker = f"[LABEEB-REPAIR:{ctl.goal_id}:{op_id}]"

    anchor = jules_snapshot(logs)
    anchor["repair_marker"] = marker
    anchor["session_id"] = sid
    anchor["reserved_at"] = utc_now()
    anchor_ref = ctl.store.write_json(ctl.paths.evidence / f"repair-anchor-{op_id}.json", anchor)

    state["repair_reserved"] = True
    state["round_anchor_ref"] = anchor_ref
    ctl.store.save(state)
    repair_text = textwrap.dedent(
        f"""
        {marker}
        Targeted correction only. Do not widen scope, push, create PRs, merge, or mutate production.
        {message}

        When the follow-up work is complete, include this exact marker in your final agent message:
        {marker}
        """
    ).strip()
    repair_request_ref = ctl.store.write_text(ctl.paths.requests / f"{op_id}.repair.txt", repair_text)
    state["repair_request_ref"] = repair_request_ref
    ctl.store.save(state)
    ctl.prepare_effect(
        state,
        "jules_message",
        {"session_id": sid, "marker": marker, "message": repair_text},
        "WAITING_JULES",
    )
    ctl.record_event("repair.prepared", {"marker": marker})


def maybe_handle_repair_activation_timeout(
    ctl: LabeebController,
    state: dict[str, Any],
    session: dict[str, Any],
    logs: dict[str, Any],
) -> bool:
    """Detect if repair work failed to begin before timeout and trigger fallback."""
    if not state.get("repair_reserved") or not state.get("round_anchor_ref"):
        return False
    anchor = read_ref_json(state["round_anchor_ref"])
    marker = str(anchor.get("repair_marker") or "")
    if not marker:
        return False
    old = set(anchor.get("activity_keys") or [])
    activities = ordered_activities([x for x in (logs.get("activities") or []) if isinstance(x, dict)])
    new_activities = [a for a in activities if activity_key(a) not in old]

    if any(
        (isinstance(a.get("agentMessaged"), dict) and marker in activity_text(a))
        or bool(patch_candidates([a]))
        for a in new_activities
    ):
        return False
    reserved_at = anchor.get("reserved_at")
    if not reserved_at:
        return False
    elapsed = (dt.datetime.now(dt.timezone.utc) - parse_utc(str(reserved_at))).total_seconds()
    timeout = float(ctl.config.get("timeouts.repair_activation_seconds", 600))
    if elapsed < timeout:
        return False
    fallback = str(ctl.config.get("workflow.repair_fallback", "blocked")).lower()
    if fallback == "new_session":
        ctl.dispatch_repair_fallback_session(state)
        return True
    ctl.block(
        state,
        "Repair message did not produce provable new Jules work before activation timeout",
        evidence={"session_id": state.get("jules_session_id"), "marker": marker, "elapsed_seconds": elapsed},
    )
    return True


def dispatch_repair_fallback_session(ctl: LabeebController, state: dict[str, Any]) -> None:
    """Spawn a clean fallback Jules session when continuity is unprovable."""
    contract = read_ref_json(state["contract_ref"])
    plan = read_ref_json(state["plan_ref"])
    repair_message = read_ref_text(state["repair_request_ref"]) if state.get("repair_request_ref") else ""
    if not repair_message:
        ctl.block(state, "Repair fallback requested but original repair request is unavailable")
        return
    op_id = new_operation_id("jules-repair-fallback")
    marker = safe_name(f"LABEEB-{ctl.goal_id}-REPAIR-FALLBACK-{op_id}", 120)
    original = str((plan.get("execution") or {}).get("jules_prompt") or "")
    prompt = textwrap.dedent(
        f"""
        This is a replacement Jules session because continuity of the completed session could not be proven.
        Re-implement the bounded original task from the configured base branch, applying the targeted correction below.

        ORIGINAL EXECUTION CONTRACT:
        {original}

        TARGETED CORRECTION:
        {repair_message}

        Remote-write boundary: no push, no PR creation, no merge, no production mutation, no remote ref changes.
        """
    ).strip()
    anchor = {
        "activity_keys": [],
        "repair_marker": "",
        "fallback_new_session": True,
        "reserved_at": utc_now(),
    }
    state["round_anchor_ref"] = ctl.store.write_json(ctl.paths.evidence / f"repair-fallback-anchor-{op_id}.json", anchor)
    ctl.store.save(state)
    ctl.prepare_effect(
        state,
        "jules_create",
        {
            "repo": contract["repo"],
            "branch": contract["branch"],
            "marker": marker,
            "prompt": prompt,
            "require_approval": bool(ctl.config.get("workflow.jules_require_plan_approval", False)),
        },
        "WAITING_JULES",
    )

"""Transient brain failure detection, auto-retry execution, and goal recovery operations.

Adheres to Single Responsibility Principle (SRP) by isolating recovery mechanisms
from the top-level LabeebController coordinator.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from labeeb.storage.goal_store import read_ref_json
from labeeb.core.state_machine import event_brain_prompt, initial_brain_prompt
from labeeb.errors import ControllerError
from labeeb.models import new_operation_id, task_name

if TYPE_CHECKING:
    from labeeb.core.controller import LabeebController

RETRYABLE_BRAIN_PATTERNS: tuple[str, ...] = (
    "thread-store conflict",
    "already has an active writer",
    "connection refused",
    "connection reset",
    "ECONNRESET",
    "ECONNREFUSED",
    "ETIMEDOUT",
    "socket hang up",
)


def is_retryable_brain_failure(done: dict[str, Any]) -> bool:
    """Return True if the brain failure looks transient and worth retrying."""
    error_text = str(done.get("error") or done.get("lastMessage") or "").lower()
    return any(pat.lower() in error_text for pat in RETRYABLE_BRAIN_PATTERNS)


def format_brain_failure_reason(done: dict[str, Any]) -> str:
    """Extract a human-readable failure reason from the brain task result."""
    error = done.get("error") or done.get("lastMessage") or ""
    lines = [ln.strip() for ln in str(error).splitlines() if ln.strip()]
    for line in reversed(lines):
        if line.startswith("Error:"):
            return f"Brain task failed: {line}"
    if lines:
        last = lines[-1]
        if len(last) > 300:
            last = last[:300] + "..."
        return f"Brain task failed: {last}"
    status = done.get("status", "unknown")
    return f"Brain task did not succeed (status: {status})"


def retry_brain_with_fresh_thread(
    ctl: LabeebController,
    state: dict[str, Any],
    done: dict[str, Any],
) -> bool:
    """Attempt to launch a fresh brain thread after a transient failure.

    Returns True if a new brain effect was prepared, False if retries exhausted.
    """
    limit = int(ctl.config.get("controller.brain_transient_retry_limit", 3))
    retries = int(state.get("brain_transient_retries", 0))
    if retries >= limit:
        return False

    backoff_base = float(ctl.config.get("controller.brain_retry_backoff_seconds", 5))
    wait = backoff_base * (retries + 1)
    retries += 1
    state["brain_transient_retries"] = retries
    ctl.store.save(state)

    error_snippet = str(done.get("error") or done.get("lastMessage") or "")[:200]
    ctl.store.append_log(
        f"Brain transient failure (retry {retries}/{limit}, "
        f"backoff {wait:.0f}s): {error_snippet}"
    )
    ctl.record_event(
        "brain.retry",
        {"retry": retries, "limit": limit, "error": error_snippet},
    )
    time.sleep(wait)

    contract = read_ref_json(state["contract_ref"])
    plan = read_ref_json(state["plan_ref"]) if state.get("plan_ref") else {}
    phase = state["phase"]
    if phase == "REVIEWING":
        review = read_ref_json(state["review_ref"]) if state.get("review_ref") else {}
        prompt = event_brain_prompt(
            {"type": "COMPLETED", "round": review.get("event", {}).get("round", "initial")},
            review,
            contract,
            plan,
            ctl.config,
        )
    elif phase == "THINKING":
        prompt = initial_brain_prompt(contract, ctl.config)
    else:
        prompt = initial_brain_prompt(contract, ctl.config)

    state["brain_thread_generation"] = int(state.get("brain_thread_generation", 1)) + 1
    op_id = new_operation_id("brain-retry")
    ctl.prepare_effect(
        state,
        "brain_launch",
        {
            "role": str(ctl.config.get("workflow.brain_role", "brain")),
            "prompt": prompt,
            "task_name": task_name(ctl.goal_id, "brain", op_id),
            "workspace": contract["workspace"],
        },
        phase,
    )
    return True


def unblock_goal(ctl: LabeebController, *, background: bool = False) -> dict[str, Any]:
    """Reset a BLOCKED goal back to its pre-block phase and optionally re-launch."""
    with ctl.store.locked():
        state = ctl.store.load()
        if state["phase"] != "BLOCKED":
            raise ControllerError(f"Goal is not BLOCKED (phase={state['phase']})")
        if state.get("blocked_action") or (
            isinstance(state.get("pending_action"), dict)
            and state["pending_action"].get("stage") == "AMBIGUOUS"
        ):
            raise ControllerError(
                "Cannot unblock goal with an unresolved ambiguous write. "
                "Please reconcile the pending action before retrying."
            )
        ctl.clear_stop_request()
        state["brain_transient_retries"] = 0
        if state.get("jules_session_id") and state.get("plan_ref"):
            target_phase = "REVIEWING"
        elif state.get("plan_ref"):
            target_phase = "THINKING"
        else:
            target_phase = "CREATED"
        state["phase"] = target_phase
        state["pending_action"] = None
        state["active_task"] = None
        ctl.store.save(state)
        ctl.store.append_log(f"Unblocked by user; returning to {target_phase}")
        ctl.record_event(
            "goal.unblocked",
            {"target_phase": target_phase},
        )
    pid = None
    if background:
        pid = ctl.start_background()
    return {"goal_id": ctl.goal_id, "phase": target_phase, "background_pid": pid}

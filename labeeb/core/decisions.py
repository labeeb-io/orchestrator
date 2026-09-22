"""Decision processing and worker/critic dispatching.

Adheres to Single Responsibility Principle (SRP) by isolating planning decisions,
review decisions, critic reviews, and dispatching operations from LabeebController.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from labeeb.config import critic_needed
from labeeb.core.events import DomainEvent, global_event_bus
from labeeb.core.state_machine import (
    convergence_prompt,
    critic_prompt,
    event_brain_prompt,
    task_name,
)
from labeeb.errors import ControllerError
from labeeb.models import (
    CRITIC_END,
    CRITIC_START,
    new_operation_id,
    safe_name,
    utc_now,
)
from labeeb.providers.base import extract_enveloped_json
from labeeb.providers.jules import path_allowed
from labeeb.storage.goal_store import read_ref_json

if TYPE_CHECKING:
    from labeeb.core.controller import LabeebController


def handle_plan_decision(ctl: LabeebController, state: dict[str, Any], decision: dict[str, Any]) -> None:
    """Process structured planning decision from the Brain."""
    if decision.get("action") == "BLOCKED":
        ctl.block(state, str(decision.get("reason") or "Planning blocked"), evidence=decision)
        return
    if decision.get("action") != "PLAN_READY":
        ctl.block(state, f"Unexpected planning action: {decision.get('action')}", evidence=decision)
        return
    contract_seed = read_ref_json(state["contract_ref"])
    goal_contract = decision.get("goal_contract") or {}
    execution = decision.get("execution") or {}
    merged_contract = dict(contract_seed)
    merged_contract["goal_contract"] = goal_contract

    seed_allowed = list(contract_seed.get("allowed_paths") or [])
    model_allowed = list(execution.get("allowed_paths") or [])
    if seed_allowed:
        for path in model_allowed:
            if not path_allowed(path, seed_allowed):
                ctl.block(state, f"Plan widened allowed path outside user authority: {path}")
                return
        allowed_paths = model_allowed or seed_allowed
    else:
        allowed_paths = model_allowed
    seed_validation = list(contract_seed.get("validation_commands") or [])
    validation = seed_validation or list(execution.get("validation_commands") or [])
    risk_tags = sorted(set(contract_seed.get("risk_tags") or []) | set(execution.get("risk_tags") or []))
    merged_contract["allowed_paths"] = allowed_paths
    merged_contract["validation_commands"] = validation
    merged_contract["risk_tags"] = risk_tags
    state["contract_ref"] = ctl.store.write_json(ctl.paths.contract, merged_contract)
    plan_payload = {
        "plan_summary": decision.get("plan_summary"),
        "execution": {
            **execution,
            "allowed_paths": allowed_paths,
            "validation_commands": validation,
            "risk_tags": risk_tags,
        },
        "planning_decision": decision,
        "at": utc_now(),
    }
    state["plan_ref"] = ctl.store.write_json(ctl.paths.plan, plan_payload)

    if not state.get("pre_critic_done") and (
        critic_needed(ctl.config, "pre", merged_contract) or bool(execution.get("needs_pre_critic"))
    ):
        critique = ctl.perform_critic("pre", merged_contract, plan_payload, None)
        state["pre_critic_done"] = True
        source_task = state["latest_codex_task_id"]
        prompt = convergence_prompt(merged_contract, plan_payload, critique)
        op_id = new_operation_id("brain-converge")
        ctl.prepare_effect(
            state,
            "brain_resume",
            {
                "role": str(ctl.config.get("workflow.brain_role", "brain")),
                "source_task_id": source_task,
                "prompt": prompt,
                "task_name": task_name(ctl.goal_id, "brain", op_id),
            },
            "THINKING",
        )
        return
    state["phase"] = "PLAN_GATE"
    ctl.store.save(state)
    ctl.record_event("plan.ready", {"plan": plan_payload})


def perform_critic(
    ctl: LabeebController,
    when: str,
    contract: dict[str, Any],
    plan: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    """Execute critic role and record enveloped review critique."""
    op_id = new_operation_id(f"critic-{when}")
    prompt = critic_prompt(when, contract, plan, evidence, ctl.config)
    result = ctl.critic.review(prompt, contract["workspace"], ctl.goal_id, op_id)
    output = str(result.get("output") or "")
    try:
        critique = extract_enveloped_json(output, CRITIC_START, CRITIC_END)
    except ControllerError as exc:
        raise ControllerError(f"Critic returned invalid structured output: {exc}") from exc
    critique["runtime"] = result.get("task") or result.get("command")
    ref = ctl.store.write_json(ctl.paths.reviews / f"{op_id}.json", critique)
    critique["ref"] = ref
    return critique


def approve_plan_if_authorized(ctl: LabeebController, state: dict[str, Any]) -> None:
    """Auto-dispatch Jules if the contract preauthorizes bounded execution."""
    contract = read_ref_json(state["contract_ref"])
    preapproved = bool(contract.get("authority", {}).get("preauthorize_bounded_plan"))
    if not preapproved:
        return
    ctl.dispatch_jules(state)


def dispatch_jules(ctl: LabeebController, state: dict[str, Any]) -> None:
    """Prepare jules_create effect for approved plan."""
    contract = read_ref_json(state["contract_ref"])
    plan = read_ref_json(state["plan_ref"])
    execution = plan.get("execution") or {}
    prompt = str(execution.get("jules_prompt") or "").strip()
    if not prompt:
        ctl.block(state, "Approved plan has no Jules execution prompt")
        return
    op_id = new_operation_id("jules-create")
    marker = safe_name(f"LABEEB-{ctl.goal_id}-EXEC-{op_id}", 120)
    safety = "\n\nRemote-write boundary: no push, no PR creation, no merge, no production mutation, no remote ref changes. Return control if scope must widen."
    payload = {
        "repo": contract["repo"],
        "branch": contract["branch"],
        "marker": marker,
        "prompt": prompt + safety,
        "require_approval": bool(ctl.config.get("workflow.jules_require_plan_approval", False)),
    }
    ctl.prepare_effect(state, "jules_create", payload, "WAITING_JULES")
    ctl.record_event("jules.dispatch_prepared", {"marker": marker})


def handle_review_decision(ctl: LabeebController, state: dict[str, Any], decision: dict[str, Any]) -> None:
    """Process brain decision during review phase."""
    action = str(decision.get("action") or "")
    evidence = read_ref_json(state["review_ref"]) if state.get("review_ref") else {}
    if action == "PASS":
        validation = evidence.get("validation") or {}
        if bool(ctl.config.get("safety.require_validation_for_pass", True)) and validation.get("status") != "PASS":
            ctl.block(state, "Brain requested PASS without passing deterministic validation", evidence=decision)
            return
        ctl.pass_goal(state, decision, evidence)
        return
    if action == "REPAIR":
        if state.get("repair_reserved"):
            ctl.fail(state, "Result still requires repair after the single V1 repair budget was consumed", evidence=decision)
            return
        message = str(decision.get("repair_message") or "").strip()
        if not message:
            ctl.block(state, "REPAIR decision missing repair_message")
            return
        ctl.reserve_and_send_repair(state, message)
        return
    if action == "APPROVE_JULES_PLAN":
        sid = state.get("jules_session_id")
        if not sid:
            ctl.block(state, "No Jules session to approve")
            return
        ctl.prepare_effect(state, "jules_approve", {"session_id": sid}, "WAITING_JULES")
        return
    if action == "ANSWER_JULES":
        sid = state.get("jules_session_id")
        message = str(decision.get("answer_message") or "").strip()
        if not sid or not message:
            ctl.block(state, "ANSWER_JULES missing session/message")
            return
        op_id = new_operation_id("jules-answer")
        marker = f"[LABEEB-ANSWER:{ctl.goal_id}:{op_id}]"
        ctl.prepare_effect(
            state,
            "jules_message",
            {"session_id": sid, "marker": marker, "message": marker + "\n" + message},
            "WAITING_JULES",
        )
        return
    if action == "BLOCKED":
        ctl.block(state, str(decision.get("reason") or "Review blocked"), evidence=decision)
        return
    if action == "FAIL":
        ctl.fail(state, str(decision.get("reason") or "Goal failed"), evidence=decision)
        return
    ctl.block(state, f"Unexpected review action: {action}", evidence=decision)


def wake_brain_for_event(ctl: LabeebController, state: dict[str, Any], event: dict[str, Any], evidence: dict[str, Any]) -> None:
    """Wake brain to review a meaningful Jules or validation event."""
    source = state.get("latest_codex_task_id")
    if not source:
        ctl.block(state, "No completed brain task available for resume")
        return
    op_id = new_operation_id("brain-event")
    contract = read_ref_json(state["contract_ref"])
    plan = read_ref_json(state["plan_ref"]) if state.get("plan_ref") else {}
    prompt = event_brain_prompt(event, evidence, contract, plan, ctl.config)
    max_resumes = int(ctl.config.get("workflow.max_brain_resumes_per_thread", 0))
    if max_resumes > 0 and int(state.get("brain_resume_count", 0)) >= max_resumes:
        state["brain_thread_generation"] = int(state.get("brain_thread_generation", 1)) + 1
        ctl.prepare_effect(
            state,
            "brain_launch",
            {
                "role": str(ctl.config.get("workflow.brain_role", "brain")),
                "prompt": prompt,
                "task_name": task_name(ctl.goal_id, "brain", op_id),
                "workspace": contract["workspace"],
            },
            "REVIEWING",
        )
    else:
        ctl.prepare_effect(
            state,
            "brain_resume",
            {
                "role": str(ctl.config.get("workflow.brain_role", "brain")),
                "source_task_id": source,
                "prompt": prompt,
                "task_name": task_name(ctl.goal_id, "brain", op_id),
            },
            "REVIEWING",
        )

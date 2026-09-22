"""Decision processing and worker/critic dispatching.

Adheres to Single Responsibility Principle (SRP) by isolating planning decisions,
review decisions, critic reviews, and dispatching operations from LabeebController.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from labeeb.config import critic_needed
from labeeb.core.artifacts import (
    BASELINE_RESULT,
    CRITIC_REVIEW,
    DELIVERY_REVIEW,
    PRODUCT_CONTRACT,
    SECOND_AUDIT,
    SOLUTION_CANDIDATES,
)
from labeeb.core.convergence import ReasoningProgressTracker
from labeeb.core.readiness import baseline_passed, evaluate_implementation_readiness
from labeeb.core.state_machine import (
    activity_brain_prompt,
    convergence_prompt,
    critic_prompt,
    event_brain_prompt,
    update_proof_path_lock,
    validate_activity_transition,
)
from labeeb.errors import ControllerError
from labeeb.models import (
    CRITIC_END,
    CRITIC_START,
    ArtifactStatus,
    ArtifactValidity,
    ReasoningActivity,
    ReasoningDecisionAction,
    new_operation_id,
    safe_name,
    task_name,
    utc_now,
)
from labeeb.providers.base import extract_enveloped_json
from labeeb.providers.jules import path_allowed
from labeeb.storage.goal_store import read_ref_json

if TYPE_CHECKING:
    from labeeb.core.controller import LabeebController


ACTIVITY_ARTIFACT_TYPES = {
    ReasoningActivity.PRODUCT_VALIDATION: PRODUCT_CONTRACT,
    ReasoningActivity.BASELINE: BASELINE_RESULT,
    ReasoningActivity.SOLUTION_EXPLORATION: SOLUTION_CANDIDATES,
    ReasoningActivity.SECOND_REALITY_AUDIT: SECOND_AUDIT,
    ReasoningActivity.DELIVERY_READINESS: DELIVERY_REVIEW,
    ReasoningActivity.INDEPENDENT_CRITIQUE: CRITIC_REVIEW,
}

ARTIFACT_TO_ACTIVITY = {v: k for k, v in ACTIVITY_ARTIFACT_TYPES.items()}


def handle_reasoning_decision(ctl: LabeebController, state: dict[str, Any], decision: dict[str, Any]) -> None:
    """Process structured reasoning activity decision envelope."""
    action = str(decision.get("decision") or decision.get("action") or "").strip()
    reason = str(decision.get("reason") or "")

    if action == ReasoningDecisionAction.BLOCKED:
        ctl.block(state, reason or "Reasoning blocked", evidence=decision)
        return

    if action == ReasoningDecisionAction.FAIL:
        ctl.fail(state, reason or "Reasoning failed", evidence=decision)
        return

    current_activity = str(
        decision.get("current_activity") or state.get("current_activity") or ReasoningActivity.GOAL_CONTRACT
    )
    state["reasoning_graph_active"] = True
    activity_status = str(decision.get("activity_status") or ArtifactStatus.SATISFIED)
    not_applicable_reason = decision.get("not_applicable_reason")
    next_activity = decision.get("next_activity")
    invalidate_roots = list(decision.get("invalidate_roots") or [])
    evidence_refs = list(decision.get("evidence_refs") or [])
    produced = decision.get("produced_artifact")
    human_cp = decision.get("human_checkpoint") or {}

    # Check for human checkpoint request
    if human_cp.get("needed") or action == ReasoningDecisionAction.NEEDS_HUMAN:
        state["phase"] = "PLAN_GATE"
        state["human_checkpoint"] = human_cp
        ctl.store.save(state)
        ctl.record_event(
            "reasoning.human_checkpoint_needed",
            {"activity": current_activity, "checkpoint": human_cp},
        )
        return

    # Persist produced artifact if present
    produced_art_type: str | None = None
    if produced and isinstance(produced, dict):
        requested_type = produced.get("artifact_type")
        produced_art_type = (
            ACTIVITY_ARTIFACT_TYPES.get(current_activity, current_activity)
            if requested_type in {None, current_activity}
            else requested_type
        )
        art_data = produced.get("data") or {}
        try:
            ctl.artifact_store.write_artifact(
                state=state,
                artifact_type=produced_art_type,
                data=art_data,
                producer=str(ctl.config.get("workflow.brain_role", "brain")),
                evidence_refs=evidence_refs,
                activity_status=activity_status,
                not_applicable_reason=not_applicable_reason,
                allow_stale_dependency=False,
            )
        except ControllerError as exc:
            ctl.block(state, str(exc), evidence=decision)
            return

    # Invalidate requested roots and cascading downstream artifacts (excluding the artifact just produced)
    if invalidate_roots:
        exclude_set = [produced_art_type] if produced_art_type else None
        ctl.artifact_store.invalidate_artifacts(state, invalidate_roots, exclude=exclude_set)

    # Validate transition against transition matrix
    validate_activity_transition(current_activity, next_activity)

    # Check progress via ReasoningProgressTracker
    max_steps = int(ctl.config.get("planning.max_reasoning_steps", 25))
    max_repeats = int(ctl.config.get("planning.max_activity_repeats", 3))
    progress_ok, progress_msg = ReasoningProgressTracker.record_step(
        state=state,
        current_activity=current_activity,
        produced_artifact_data=produced.get("data") if produced else None,
        evidence_refs=evidence_refs,
        activity_status=activity_status,
        max_steps=max_steps,
        max_repeats=max_repeats,
    )
    if not progress_ok:
        ctl.block(state, progress_msg, evidence=decision)
        return

    # Record history
    history = state.setdefault("reasoning_history", [])
    history.append(
        {
            "activity": current_activity,
            "status": activity_status,
            "not_applicable_reason": not_applicable_reason,
            "next_activity": next_activity,
            "at": utc_now(),
        }
    )
    ctl.record_event(
        "reasoning.activity_completed",
        {
            "activity": current_activity,
            "status": activity_status,
            "next": next_activity,
        },
    )

    if action == ReasoningDecisionAction.PASS:
        if not baseline_passed(decision):
            ctl.block(state, "Only a passing baseline may terminate a reasoning goal", evidence=decision)
            return
        ctl.complete_baseline_shortcut(state, decision)
        return

    if action == ReasoningDecisionAction.IMPLEMENTATION_READY:
        handle_implementation_readiness_request(ctl, state, decision)
        return

    if action == ReasoningDecisionAction.CONTINUE_REASONING:
        if not next_activity:
            ctl.block(state, "CONTINUE_REASONING decision missing next_activity", evidence=decision)
            return
        state["current_activity"] = next_activity
        ctl.store.save(state)
        # Prepare brain resume for next activity
        source_task = state.get("latest_codex_task_id")
        contract = read_ref_json(state["contract_ref"])
        valid_artifacts = {
            k: read_ref_json(v["ref"])
            for k, v in state.get("artifacts", {}).items()
            if v.get("validity") == ArtifactValidity.VALID
        }
        prompt = activity_brain_prompt(next_activity, contract, valid_artifacts, ctl.config)
        op_id = new_operation_id(f"brain-{next_activity}")
        if source_task:
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
        else:
            ctl.prepare_effect(
                state,
                "brain_launch",
                {
                    "role": str(ctl.config.get("workflow.brain_role", "brain")),
                    "prompt": prompt,
                    "task_name": task_name(ctl.goal_id, "brain", op_id),
                    "workspace": contract["workspace"],
                },
                "THINKING",
            )
        return

    ctl.block(state, f"Unhandled reasoning action: {action}", evidence=decision)


def handle_implementation_readiness_request(
    ctl: LabeebController, state: dict[str, Any], decision: dict[str, Any]
) -> None:
    """Handle IMPLEMENTATION_READY decision."""
    contract_seed = read_ref_json(state["contract_ref"])
    produced = decision.get("produced_artifact") or {}
    execution = (
        decision.get("execution")
        or (produced.get("data", {}) if isinstance(produced, dict) else {}).get("execution")
        or {}
    )

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

    merged_contract = dict(contract_seed)
    merged_contract["allowed_paths"] = allowed_paths
    merged_contract["validation_commands"] = validation
    merged_contract["risk_tags"] = risk_tags
    state["contract_ref"] = ctl.store.write_json(ctl.paths.contract, merged_contract)

    plan_payload = {
        "plan_summary": decision.get("plan_summary") or decision.get("reason"),
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
    state["phase"] = "PLAN_GATE"
    ctl.store.save(state)
    ctl.record_event("plan.ready", {"plan": plan_payload})
    ctl.approve_plan_if_authorized(state)


def handle_plan_decision(ctl: LabeebController, state: dict[str, Any], decision: dict[str, Any]) -> None:
    """Process structured planning decision from the Brain."""
    if "decision" in decision or "current_activity" in decision:
        handle_reasoning_decision(ctl, state, decision)
        return

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


def approve_plan_if_authorized(
    ctl: LabeebController, state: dict[str, Any], *, manual_approval: bool = False
) -> None:
    """Dispatch only after V2 readiness and authority checks, or retain legacy V1 behavior."""
    contract = read_ref_json(state["contract_ref"])
    preapproved = bool(contract.get("authority", {}).get("preauthorize_bounded_plan"))
    if not state.get("reasoning_graph_active"):
        if preapproved or manual_approval:
            ctl.dispatch_jules(state)
        return

    plan = read_ref_json(state["plan_ref"])
    readiness = evaluate_implementation_readiness(
        state,
        contract,
        plan,
        max_execution_rounds=int(ctl.config.get("planning.max_execution_rounds", 2)),
        require_proof_path_locked=False,
    )
    if readiness["ready"] and not state.get("proof_path_locked"):
        update_proof_path_lock(state, {}, is_state_changing=True)
        readiness = evaluate_implementation_readiness(
            state,
            contract,
            plan,
            max_execution_rounds=int(ctl.config.get("planning.max_execution_rounds", 2)),
        )
    state["implementation_readiness"] = readiness
    state["change_authority"] = readiness["authority"]
    if not readiness["ready"]:
        state["phase"] = "PLAN_GATE"
        ctl.store.save(state)
        ctl.record_event("readiness.blocked", readiness)
        return

    authority = readiness["authority"]
    if authority in {"PRODUCTION", "INCIDENTAL"}:
        ctl.block(state, f"Change authority blocks autonomous execution: {authority}", evidence=readiness)
        return
    if authority == "GATED" and not manual_approval:
        state["phase"] = "PLAN_GATE"
        state["human_checkpoint"] = {
            "needed": True,
            "boundary_type": "change_authority",
            "question": "Approve the bounded gated implementation plan?",
        }
        ctl.store.save(state)
        ctl.record_event("readiness.human_gate", readiness)
        return
    if not preapproved and not manual_approval:
        state["phase"] = "PLAN_GATE"
        ctl.store.save(state)
        ctl.record_event("readiness.awaiting_approval", readiness)
        return

    state["macro_phase"] = "EXECUTE"
    state["current_activity"] = ReasoningActivity.EXECUTION_CONTRACT
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
    max_rounds = int(ctl.config.get("planning.max_execution_rounds", 2))
    current_rounds = int(state.get("execution_rounds", 0))
    if current_rounds >= max_rounds:
        ctl.fail(state, f"Execution rounds exhausted: reached maximum of {max_rounds} rounds")
        return
    state["execution_rounds"] = current_rounds + 1
    state["macro_phase"] = "EXECUTE"
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
    ctl.record_event("jules.dispatch_prepared", {"marker": marker, "execution_round": state["execution_rounds"]})


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
    if action in {"RETURN_TO_THINKING", ReasoningDecisionAction.RETURN_TO_THINKING}:
        max_rounds = int(ctl.config.get("planning.max_execution_rounds", 2))
        current_rounds = int(state.get("execution_rounds", 0))
        if current_rounds >= max_rounds:
            ctl.fail(state, f"Cannot return to thinking: reached maximum execution rounds ({max_rounds})", evidence=decision)
            return

        invalidate_roots = list(decision.get("invalidate_roots") or [])
        if not invalidate_roots:
            invalidate_roots = [SOLUTION_CANDIDATES]

        ctl.artifact_store.invalidate_artifacts(state, invalidate_roots)

        state["macro_phase"] = "THINKING"
        raw_target = invalidate_roots[0] if invalidate_roots else ReasoningActivity.SOLUTION_EXPLORATION
        target_activity = ARTIFACT_TO_ACTIVITY.get(raw_target, raw_target)
        state["current_activity"] = target_activity
        ctl.store.save(state)

        ctl.record_event(
            "reasoning.returned_to_thinking",
            {
                "reason": decision.get("reason"),
                "invalidate_roots": invalidate_roots,
                "target_activity": target_activity,
                "execution_rounds": current_rounds,
                "repair_reserved": bool(state.get("repair_reserved", False)),
            },
        )

        source_task = state.get("latest_codex_task_id")
        contract = read_ref_json(state["contract_ref"])
        valid_artifacts = {
            k: read_ref_json(v["ref"])
            for k, v in state.get("artifacts", {}).items()
            if v.get("validity") == ArtifactValidity.VALID
        }
        prompt = activity_brain_prompt(target_activity, contract, valid_artifacts, ctl.config)
        op_id = new_operation_id(f"brain-{target_activity}")
        if source_task:
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
        else:
            ctl.prepare_effect(
                state,
                "brain_launch",
                {
                    "role": str(ctl.config.get("workflow.brain_role", "brain")),
                    "prompt": prompt,
                    "task_name": task_name(ctl.goal_id, "brain", op_id),
                    "workspace": contract["workspace"],
                },
                "THINKING",
            )
        return
    if action in {"REPAIR", ReasoningDecisionAction.TARGETED_REPAIR}:
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

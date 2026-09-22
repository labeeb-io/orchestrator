"""Prompt formatting, structured decision parsing, and review transitions."""
from __future__ import annotations

import textwrap
from typing import Any

from labeeb.config import Config
from labeeb.errors import ControllerError
from labeeb.models import (
    CRITIC_END,
    CRITIC_START,
    DECISION_END,
    DECISION_START,
    ArtifactStatus,
    HumanCheckpoint,
    PathIntegrityStatus,
    ReasoningActivity,
    ReasoningDecision,
    format_json_for_prompt,
    utc_now,
)
from labeeb.providers.base import extract_enveloped_json


def initial_brain_prompt(contract_seed: dict[str, Any], config: Config) -> str:
    custom = config.get("prompts.brain_initial", "")
    if custom:
        return str(custom).format(contract_json=format_json_for_prompt(contract_seed))
    return activity_brain_prompt(ReasoningActivity.AUTHORITY_CONTEXT, contract_seed, {}, config)


def convergence_prompt(contract: dict[str, Any], plan: dict[str, Any], critique: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""
        Continue the SAME Labeeb goal. An independent read-only critic reviewed the candidate plan.
        Resolve every material finding against repository evidence. Reject unsupported criticism explicitly; change the plan only when evidence requires it.

        Goal contract:
        {format_json_for_prompt(contract)}

        Candidate plan:
        {format_json_for_prompt(plan)}

        Critic result:
        {format_json_for_prompt(critique)}

        Return exactly:
        {DECISION_START}
        {{
          "action": "PLAN_READY" | "BLOCKED",
          "reason": "...",
          "goal_contract": {{...complete contract...}},
          "execution": {{
            "jules_prompt": "complete bounded execution contract",
            "validation_commands": ["..."],
            "allowed_paths": ["..."],
            "risk_tags": ["..."],
            "needs_pre_critic": false,
            "needs_post_critic": true|false
          }},
          "plan_summary": "...",
          "critic_resolution": [{{"finding":"...","resolution":"accepted|rejected|blocked","evidence":"..."}}]
        }}
        {DECISION_END}
        No prose outside the envelope.
        """
    ).strip()


def event_brain_prompt(
    event: dict[str, Any],
    evidence: dict[str, Any],
    contract: dict[str, Any],
    plan: dict[str, Any],
    config: Config,
) -> str:
    max_chars = int(config.get("controller.max_prompt_evidence_chars", 70000))
    return textwrap.dedent(
        f"""
        Continue the SAME Labeeb goal. A deterministic controller woke you for a meaningful event.
        Do not execute Jules write commands yourself. Do not push, create a PR, merge, or mutate production.

        Contract:
        {format_json_for_prompt(contract)}

        Approved plan/execution contract:
        {format_json_for_prompt(plan)}

        Event:
        {format_json_for_prompt(event)}

        Evidence packet:
        {format_json_for_prompt(evidence, max_chars=max_chars)}

        Return exactly one action envelope:
        {DECISION_START}
        {{
          "action": "PASS" | "REPAIR" | "RETURN_TO_THINKING" | "APPROVE_JULES_PLAN" | "ANSWER_JULES" | "BLOCKED" | "FAIL",
          "reason": "...",
          "repair_message": "required only for REPAIR",
          "answer_message": "required only for ANSWER_JULES",
          "invalidate_roots": ["artifact_type_to_invalidate_if_assumption_disproved"],
          "evidence_assessment": "..."
        }}
        {DECISION_END}

        Rules:
        - PASS only if the original Goal Contract is actually evidenced, not because Jules says completed.
        - RETURN_TO_THINKING if validation or proof reveals an invalidated planning assumption rather than a code defect. Specify invalidate_roots. This preserves the single repair budget.
        - REPAIR must be a targeted correction of a concrete mismatch; no redesign or scope widening.
        - If authority/scope/evidence is insufficient, BLOCKED.
        - No prose outside the envelope.
        """
    ).strip()


def critic_prompt(
    when: str,
    contract: dict[str, Any],
    plan: dict[str, Any],
    evidence: dict[str, Any] | None,
    config: Config,
) -> str:
    if when == "pre":
        task = "Try to disprove or simplify the proposed plan before implementation."
        focus = "missed reuse, hidden coupling, secondary callers, concurrency/state invariants, excessive scope, missing validation, architecture mismatch"
    else:
        task = "Try to find a material reason the actual implementation does not satisfy the original Goal Contract."
        focus = "requirement omissions, scope drift, hidden regressions, unplanned files, stale assumptions, false-positive tests, validation gaps"
    max_chars = int(config.get("controller.max_prompt_evidence_chars", 70000))
    return textwrap.dedent(
        f"""
        You are an independent READ-ONLY engineering critic. {task}
        Do not edit files. Do not use write-capable tools. Do not contact Jules. Do not redesign merely for preference.
        Focus on: {focus}.

        Goal contract:
        {format_json_for_prompt(contract)}

        Plan:
        {format_json_for_prompt(plan)}

        Evidence:
        {format_json_for_prompt(evidence or {}, max_chars=max_chars)}

        Return exactly:
        {CRITIC_START}
        {{
          "material_findings": [
            {{"finding":"...","location":"...","evidence":"...","impact":"...","test":"..."}}
          ],
          "notes": ""
        }}
        {CRITIC_END}
        No prose outside the envelope.
        """
    ).strip()


ALLOWED_ACTIVITY_TRANSITIONS: dict[str, set[str]] = {
    ReasoningActivity.AUTHORITY_CONTEXT: {
        ReasoningActivity.GOAL_CONTRACT,
    },
    ReasoningActivity.GOAL_CONTRACT: {
        ReasoningActivity.PRODUCT_VALIDATION,
        ReasoningActivity.AUTHORITY_CONTEXT,
    },
    ReasoningActivity.PRODUCT_VALIDATION: {
        ReasoningActivity.PROOF_CONTRACT,
        ReasoningActivity.GOAL_CONTRACT,
        ReasoningActivity.AUTHORITY_CONTEXT,
    },
    ReasoningActivity.PROOF_CONTRACT: {
        ReasoningActivity.REALITY_AUDIT,
        ReasoningActivity.PRODUCT_VALIDATION,
        ReasoningActivity.GOAL_CONTRACT,
    },
    ReasoningActivity.REALITY_AUDIT: {
        ReasoningActivity.MUTATION_PREFLIGHT,
        ReasoningActivity.BASELINE,
        ReasoningActivity.PROOF_CONTRACT,
        ReasoningActivity.GOAL_CONTRACT,
    },
    ReasoningActivity.MUTATION_PREFLIGHT: {
        ReasoningActivity.BASELINE,
        ReasoningActivity.REALITY_AUDIT,
        ReasoningActivity.PROOF_CONTRACT,
    },
    ReasoningActivity.BASELINE: {
        ReasoningActivity.DIAGNOSIS,
        ReasoningActivity.SOLUTION_EXPLORATION,
        ReasoningActivity.MUTATION_PREFLIGHT,
        ReasoningActivity.REALITY_AUDIT,
        ReasoningActivity.PROOF_CONTRACT,
        ReasoningActivity.GOAL_CONTRACT,
    },
    ReasoningActivity.DIAGNOSIS: {
        ReasoningActivity.SOLUTION_EXPLORATION,
        ReasoningActivity.BASELINE,
        ReasoningActivity.REALITY_AUDIT,
    },
    ReasoningActivity.SOLUTION_EXPLORATION: {
        ReasoningActivity.SECOND_REALITY_AUDIT,
        ReasoningActivity.DIAGNOSIS,
        ReasoningActivity.BASELINE,
        ReasoningActivity.REALITY_AUDIT,
    },
    ReasoningActivity.SECOND_REALITY_AUDIT: {
        ReasoningActivity.DELIVERY_READINESS,
        ReasoningActivity.SOLUTION_EXPLORATION,
        ReasoningActivity.DIAGNOSIS,
        ReasoningActivity.REALITY_AUDIT,
    },
    ReasoningActivity.DELIVERY_READINESS: {
        ReasoningActivity.INDEPENDENT_CRITIQUE,
        ReasoningActivity.CONVERGENCE,
        ReasoningActivity.SECOND_REALITY_AUDIT,
        ReasoningActivity.SOLUTION_EXPLORATION,
        ReasoningActivity.PRODUCT_VALIDATION,
        ReasoningActivity.GOAL_CONTRACT,
    },
    ReasoningActivity.INDEPENDENT_CRITIQUE: {
        ReasoningActivity.CONVERGENCE,
        ReasoningActivity.DELIVERY_READINESS,
        ReasoningActivity.SECOND_REALITY_AUDIT,
        ReasoningActivity.SOLUTION_EXPLORATION,
    },
    ReasoningActivity.CONVERGENCE: {
        ReasoningActivity.CHANGE_AUTHORITY,
        ReasoningActivity.INDEPENDENT_CRITIQUE,
        ReasoningActivity.SECOND_REALITY_AUDIT,
        ReasoningActivity.SOLUTION_EXPLORATION,
    },
    ReasoningActivity.CHANGE_AUTHORITY: {
        ReasoningActivity.IMPLEMENTATION_READINESS,
        ReasoningActivity.CONVERGENCE,
        ReasoningActivity.DIAGNOSIS,
        ReasoningActivity.SOLUTION_EXPLORATION,
    },
    ReasoningActivity.IMPLEMENTATION_READINESS: {
        ReasoningActivity.EXECUTION_CONTRACT,
        ReasoningActivity.CHANGE_AUTHORITY,
        ReasoningActivity.CONVERGENCE,
        ReasoningActivity.DELIVERY_READINESS,
        ReasoningActivity.SECOND_REALITY_AUDIT,
        ReasoningActivity.SOLUTION_EXPLORATION,
        ReasoningActivity.DIAGNOSIS,
        ReasoningActivity.BASELINE,
        ReasoningActivity.REALITY_AUDIT,
        ReasoningActivity.PROOF_CONTRACT,
        ReasoningActivity.PRODUCT_VALIDATION,
        ReasoningActivity.GOAL_CONTRACT,
        ReasoningActivity.AUTHORITY_CONTEXT,
    },
    ReasoningActivity.EXECUTION_CONTRACT: {
        ReasoningActivity.IMPLEMENTATION_READINESS,
        ReasoningActivity.CHANGE_AUTHORITY,
        ReasoningActivity.CONVERGENCE,
    },
}


def validate_activity_transition(current_activity: str, next_activity: str | None) -> bool:
    """Validate whether next_activity is an allowed forward transition or valid backtrack."""
    if next_activity is None:
        return True
    allowed = ALLOWED_ACTIVITY_TRANSITIONS.get(current_activity)
    if allowed is None:
        raise ControllerError(f"Unknown current activity: {current_activity}")
    if next_activity not in allowed:
        raise ControllerError(
            f"Invalid activity transition: from '{current_activity}' to '{next_activity}'. Allowed: {sorted(allowed)}"
        )
    return True


def compile_authority_context(contract: dict[str, Any], config: Config) -> dict[str, Any]:
    """Compile non-secret authority and policy context."""
    return {
        "repository_policy": {
            "source": "AGENTS.md",
            "consulted": True,
            "rules": [
                "Preserve existing architecture boundaries",
                "Follow evidence broadly; change only the owning boundary",
                "Discovery authority is not change authority",
            ],
        },
        "environment": {
            "workspace": contract.get("workspace"),
            "repo": contract.get("repo"),
            "branch": contract.get("branch"),
        },
        "allowed_mutation_scope": {
            "allowed_paths": list(contract.get("allowed_paths") or []),
            "risk_tags": list(contract.get("risk_tags") or []),
        },
        "remote_write_boundary": {
            "allow_push": False,
            "allow_pr": False,
            "allow_merge": False,
            "allow_production_mutation": False,
        },
        "user_authority": {
            "preauthorize_bounded_plan": bool(
                contract.get("authority", {}).get("preauthorize_bounded_plan", False)
            ),
            "allow_workspace_edits": bool(
                contract.get("authority", {}).get("allow_workspace_edits", True)
            ),
        },
        "role_policy": {
            "brain_role": str(config.get("workflow.brain_role", "brain")),
            "critic_role": str(config.get("workflow.critic_role", "critic")),
            "implementer_role": str(config.get("workflow.implementer_role", "implementer")),
        },
        "captured_at": utc_now(),
    }


def evaluate_mutation_preflight(
    proof_contract: dict[str, Any],
    preflight_data: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]]:
    """Evaluate safety and bounds of mutation preflight before baseline."""
    is_read_only = bool(preflight_data.get("is_read_only", False))
    if is_read_only:
        return True, ArtifactStatus.NOT_APPLICABLE, {
            "is_read_only": True,
            "safe": True,
            "reason": "Baseline is read-only; no mutation preflight required",
        }

    mutation_target = str(preflight_data.get("mutation_target") or "").strip()
    limit_semantics = str(preflight_data.get("limit_semantics") or "").strip()
    async_continuation = bool(preflight_data.get("async_continuation", False))
    unbounded_fan_out = bool(preflight_data.get("unbounded_fan_out", False))
    stable_identifier = str(preflight_data.get("stable_identifier") or "").strip()
    cleanup_defined = bool(preflight_data.get("cleanup_defined", False))

    issues = []
    if not mutation_target:
        issues.append("Missing mutation target")
    if not limit_semantics:
        issues.append("Missing limit semantics")
    if unbounded_fan_out:
        issues.append("Unbounded fan-out across records/queues detected")
    if not stable_identifier:
        issues.append("Missing stable test item identifier or cohort")

    if issues:
        return False, ArtifactStatus.BLOCKED, {
            "is_read_only": False,
            "safe": False,
            "issues": issues,
            "reason": f"Mutation preflight blocked: {', '.join(issues)}",
        }

    return True, ArtifactStatus.SATISFIED, {
        "is_read_only": False,
        "safe": True,
        "mutation_target": mutation_target,
        "limit_semantics": limit_semantics,
        "async_continuation": async_continuation,
        "stable_identifier": stable_identifier,
        "cleanup_defined": cleanup_defined,
    }


def update_proof_path_lock(
    state: dict[str, Any],
    new_proof_contract_data: dict[str, Any],
    *,
    is_state_changing: bool = False,
    is_diagnostic_only: bool = False,
) -> tuple[bool, str]:
    """Manage proof-path locking and integrity status."""
    is_locked = bool(state.get("proof_path_locked", False))
    current_status = state.get("path_integrity_status", PathIntegrityStatus.ORIGINAL)

    if is_locked:
        if is_diagnostic_only:
            state["path_integrity_status"] = PathIntegrityStatus.ALTERNATE_DIAGNOSTIC_ONLY
            return True, PathIntegrityStatus.ALTERNATE_DIAGNOSTIC_ONLY
        else:
            raise ControllerError(
                "Cannot replace locked original proof path after state-changing baseline has started"
            )

    # Not yet locked
    if is_state_changing:
        state["proof_path_locked"] = True
        return True, current_status
    else:
        # Read-only recompile before baseline allowed
        if new_proof_contract_data.get("recompiled_from_original"):
            state["path_integrity_status"] = PathIntegrityStatus.RECOMPILED_BEFORE_BASELINE
            return True, PathIntegrityStatus.RECOMPILED_BEFORE_BASELINE
        return True, PathIntegrityStatus.ORIGINAL


def activity_brain_prompt(
    activity_name: str,
    contract_seed: dict[str, Any],
    valid_upstream_artifacts: dict[str, Any],
    config: Config,
) -> str:
    """Generate concise activity prompt referencing skill semantics."""
    artifact_type = {
        ReasoningActivity.PRODUCT_VALIDATION: "product_contract",
        ReasoningActivity.BASELINE: "baseline_result",
        ReasoningActivity.SOLUTION_EXPLORATION: "solution_candidates",
        ReasoningActivity.SECOND_REALITY_AUDIT: "second_audit",
        ReasoningActivity.DELIVERY_READINESS: "delivery_review",
        ReasoningActivity.INDEPENDENT_CRITIQUE: "critic_review",
    }.get(activity_name, activity_name)
    skill_guidance = {
        ReasoningActivity.AUTHORITY_CONTEXT: "Apply the semantics of /labeeb-engineering-goal (authority bootstrap).",
        ReasoningActivity.GOAL_CONTRACT: "Apply the semantics of /labeeb-orchestrator (Goal Contract compilation).",
        ReasoningActivity.PRODUCT_VALIDATION: "Apply the semantics of /labeeb-product-auditor (user journey, observable outcome, functional acceptance criteria). If task has no user-facing behavior, emit status NOT_APPLICABLE.",
        ReasoningActivity.PROOF_CONTRACT: "Apply the semantics of /labeeb-engineering-goal (supported entrypoint, completion probe, observable checkpoints).",
        ReasoningActivity.REALITY_AUDIT: "Apply the semantics of /labeeb-orchestrator (inspect repository reality, deliberate reuse search).",
        ReasoningActivity.MUTATION_PREFLIGHT: "Apply the semantics of /labeeb-engineering-goal (mutation preflight: bounds, limits, fan-out, cohort).",
        ReasoningActivity.BASELINE: "Apply the semantics of /labeeb-engineering-goal (exercise entrypoint before code changes; stop at first broken boundary). If baseline passes completely, output action PASS and data.status=PASS.",
        ReasoningActivity.DIAGNOSIS: "Apply the semantics of /labeeb-engineering-goal (diagnose first broken boundary; root cause evidence).",
        ReasoningActivity.SOLUTION_EXPLORATION: "Apply the semantics of /labeeb-orchestrator (evaluate meaningful alternatives, prioritize reuse and minimal scope).",
        ReasoningActivity.SECOND_REALITY_AUDIT: "Apply the semantics of /labeeb-orchestrator (actively attempt to disprove preferred solution; check hidden coupling and secondary callers).",
        ReasoningActivity.DELIVERY_READINESS: "Apply the semantics of /labeeb-product-auditor (verify solution delivers requested behavior against acceptance criteria).",
        ReasoningActivity.INDEPENDENT_CRITIQUE: "Apply the semantics of /labeeb-orchestrator (adversarial critique of candidate plan).",
        ReasoningActivity.CONVERGENCE: "Apply the semantics of /labeeb-orchestrator (resolve all material criticism against repository evidence).",
        ReasoningActivity.CHANGE_AUTHORITY: "Apply the semantics of /labeeb-engineering-goal (classify repair authority: LOCAL, GATED, INCIDENTAL, PRODUCTION).",
        ReasoningActivity.IMPLEMENTATION_READINESS: "Apply the semantics of /labeeb-orchestrator (verify all required artifacts, bounded scope, validation plan).",
        ReasoningActivity.EXECUTION_CONTRACT: "Apply the semantics of /labeeb-orchestrator (bounded execution contract with strict remote-write boundary).",
    }.get(activity_name, "Apply Labeeb engineering principles.")

    return textwrap.dedent(
        f"""
        You are the Labeeb engineering decision brain. Work READ-ONLY in the repository during this turn.
        Active activity: {activity_name}
        Guidance: {skill_guidance}

        Initial request:
        {format_json_for_prompt(contract_seed)}

        Current valid upstream artifacts:
        {format_json_for_prompt(valid_upstream_artifacts)}

        Return exactly one structured decision envelope:
        {DECISION_START}
        {{
          "decision": "CONTINUE_REASONING" | "IMPLEMENTATION_READY" | "NEEDS_HUMAN" | "RETURN_TO_THINKING" | "BLOCKED" | "PASS" | "FAIL",
          "current_activity": "{activity_name}",
          "activity_status": "SATISFIED" | "NOT_APPLICABLE" | "NEEDS_WORK" | "BLOCKED",
          "not_applicable_reason": "required if activity_status is NOT_APPLICABLE",
          "next_activity": "next activity name or null",
          "reason": "summary of findings and rationale",
          "produced_artifact": {{
            "artifact_type": "{artifact_type}",
            "data": {{}}
          }},
          "invalidate_roots": ["artifact_type_to_invalidate_if_assumption_disproved"],
          "evidence_refs": ["file:path/to/code#L10-L20"],
          "human_checkpoint": {{
            "needed": false,
            "boundary_type": null,
            "question": null
          }}
        }}
        {DECISION_END}
        No prose outside the envelope.
        """
    ).strip()


def parse_reasoning_decision(output: str) -> ReasoningDecision:
    """Parse structured reasoning decision envelope from Brain output."""
    raw = extract_enveloped_json(output, DECISION_START, DECISION_END)
    decision = str(raw.get("decision") or raw.get("action") or "").strip()
    if not decision:
        raise ControllerError("Decision envelope missing 'decision' field")
    current_activity = str(raw.get("current_activity") or "").strip()
    if not current_activity:
        current_activity = ReasoningActivity.GOAL_CONTRACT

    activity_status = str(raw.get("activity_status") or ArtifactStatus.SATISFIED)
    next_activity = raw.get("next_activity")
    reason = str(raw.get("reason") or "")
    not_applicable_reason = raw.get("not_applicable_reason")
    produced_artifact = raw.get("produced_artifact")
    invalidate_roots = list(raw.get("invalidate_roots") or [])
    evidence_refs = list(raw.get("evidence_refs") or [])
    human_cp_raw = raw.get("human_checkpoint") or {}
    human_checkpoint = HumanCheckpoint(
        needed=bool(human_cp_raw.get("needed", False)),
        boundary_type=human_cp_raw.get("boundary_type"),
        question=human_cp_raw.get("question"),
    )

    return ReasoningDecision(
        decision=decision,
        current_activity=current_activity,
        activity_status=activity_status,
        next_activity=next_activity,
        reason=reason,
        not_applicable_reason=not_applicable_reason,
        produced_artifact=produced_artifact,
        invalidate_roots=invalidate_roots,
        evidence_refs=evidence_refs,
        human_checkpoint=human_checkpoint,
    )

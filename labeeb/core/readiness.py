"""Deterministic implementation-readiness checks for V2 reasoning goals."""
from __future__ import annotations

from typing import Any

from labeeb.core.artifacts import (
    AUTHORITY_CONTEXT,
    BASELINE_RESULT,
    CHANGE_AUTHORITY,
    CONVERGENCE,
    CRITIC_REVIEW,
    GOAL_CONTRACT,
    MUTATION_PREFLIGHT,
    PROOF_CONTRACT,
    REALITY_AUDIT,
    SECOND_AUDIT,
    SOLUTION_CANDIDATES,
)
from labeeb.models import ArtifactStatus, ArtifactValidity, ChangeAuthorityLevel
from labeeb.providers.jules import path_allowed
from labeeb.storage.goal_store import read_ref_json


def baseline_passed(decision: dict[str, Any]) -> bool:
    """Return whether a reasoning decision proves the original baseline already passes."""
    if decision.get("current_activity") != "baseline":
        return False
    produced = decision.get("produced_artifact") or {}
    data = produced.get("data") if isinstance(produced, dict) else None
    return (
        str(decision.get("activity_status") or ArtifactStatus.SATISFIED) == ArtifactStatus.SATISFIED
        and isinstance(data, dict)
        and str(data.get("status") or "").upper() == "PASS"
    )


def classify_change_authority(contract: dict[str, Any], execution: dict[str, Any]) -> str:
    """Classify execution using controller-visible authority rather than model preference."""
    authority = contract.get("authority") or {}
    if any(
        bool(execution.get(key))
        for key in ("production_mutation", "remote_write", "allow_push", "allow_pr", "allow_merge")
    ):
        return ChangeAuthorityLevel.PRODUCTION
    if bool(execution.get("incidental_scope")):
        return ChangeAuthorityLevel.INCIDENTAL
    if bool(execution.get("requires_human_approval")) or not bool(authority.get("preauthorize_bounded_plan")):
        return ChangeAuthorityLevel.GATED
    return ChangeAuthorityLevel.LOCAL


def evaluate_implementation_readiness(
    state: dict[str, Any],
    contract: dict[str, Any],
    plan: dict[str, Any],
    *,
    max_execution_rounds: int = 2,
    require_proof_path_locked: bool = True,
) -> dict[str, Any]:
    """Evaluate the V2 invariants before a worker may receive a write-capable task."""
    artifacts = state.get("artifacts") or {}
    reasons: list[str] = []
    required = (
        AUTHORITY_CONTEXT,
        GOAL_CONTRACT,
        PROOF_CONTRACT,
        REALITY_AUDIT,
        MUTATION_PREFLIGHT,
        BASELINE_RESULT,
        SOLUTION_CANDIDATES,
        SECOND_AUDIT,
        CHANGE_AUTHORITY,
    )
    for artifact_type in required:
        entry = artifacts.get(artifact_type) or {}
        if entry.get("validity") != ArtifactValidity.VALID:
            reasons.append(f"Required artifact is missing or stale: {artifact_type}")

    preflight = artifacts.get(MUTATION_PREFLIGHT) or {}
    if preflight.get("activity_status") not in {ArtifactStatus.SATISFIED, ArtifactStatus.NOT_APPLICABLE}:
        reasons.append("Mutation preflight is not satisfied")

    if CRITIC_REVIEW in artifacts and (artifacts.get(CONVERGENCE) or {}).get("validity") != ArtifactValidity.VALID:
        reasons.append("Critic review has not converged")

    execution = plan.get("execution") or {}
    allowed_paths = list(execution.get("allowed_paths") or [])
    seed_allowed = list(contract.get("allowed_paths") or [])
    if seed_allowed and any(not path_allowed(path, seed_allowed) for path in allowed_paths):
        reasons.append("Plan widens allowed paths outside user authority")
    if not list(execution.get("validation_commands") or []):
        reasons.append("Deterministic validation commands are required")

    if any(bool(execution.get(key)) for key in ("remote_write", "allow_push", "allow_pr", "allow_merge")):
        reasons.append("Execution contract permits a remote write")
    if require_proof_path_locked and not bool(state.get("proof_path_locked")):
        reasons.append("Original proof path is not locked")
    if int(state.get("execution_rounds", 0)) >= max_execution_rounds:
        reasons.append("Execution round budget is exhausted")

    authority = classify_change_authority(contract, execution)
    change_authority = artifacts.get(CHANGE_AUTHORITY) or {}
    claimed = None
    if change_authority.get("ref"):
        claimed = (read_ref_json(change_authority["ref"]).get("data") or {}).get("classification")
    if claimed and claimed != authority:
        reasons.append(f"Change authority claim does not match controller classification: {claimed}")

    return {"ready": not reasons, "reasons": reasons, "authority": authority}

"""Phase 5 evidence compilation helpers for final reports."""
from __future__ import annotations

from typing import Any, Callable


def compile_phase5_evidence(
    *,
    state: dict[str, Any],
    contract: dict[str, Any],
    plan_summary: dict[str, Any],
    validation_summary: dict[str, Any],
    ev_dict: dict[str, Any],
    review: dict[str, Any],
    terminal_status: str,
    terminal_path: str,
    execution_rounds: int,
    path_integrity: str,
    get_artifact_data_fn: Callable[[str], dict[str, Any] | None],
) -> tuple[dict[str, Any], bool]:
    """Compile Phase 5 evidence dictionaries and determine proof_passed status."""
    _get_artifact_data = get_artifact_data_fn

    # Diagnosis / Root Cause
    diag = _get_artifact_data("diagnosis")
    if diag:
        diagnosis_ev = {
            "status": "Available",
            "root_cause": str(diag.get("root_cause") or diag.get("diagnosis") or diag.get("cause") or "Root cause analyzed"),
            "failure_symptoms": list(diag.get("failure_symptoms") or diag.get("symptoms") or []),
            "causal_chain": list(diag.get("causal_chain") or []),
        }
    else:
        diagnosis_ev = {"status": "Unavailable", "root_cause": "Unavailable", "reason": "No diagnosis artifact recorded"}

    # Solution Candidates
    sol = _get_artifact_data("solution_candidates")
    if sol:
        solutions_ev = {
            "status": "Available",
            "selected_approach": str(sol.get("selected_approach") or sol.get("approach") or sol.get("selected_candidate") or "Selected approach documented"),
            "rejected_alternatives": list(sol.get("rejected_alternatives") or sol.get("alternatives") or sol.get("rejected_candidates") or []),
            "trade_offs": str(sol.get("trade_offs") or ""),
        }
    else:
        solutions_ev = {"status": "Unavailable", "selected_approach": "Unavailable", "reason": "No solution candidates artifact recorded"}

    # Change Authority
    ca = _get_artifact_data("change_authority")
    if ca:
        change_authority_ev = {
            "status": "Available",
            "classification": str(ca.get("classification") or ca.get("authority_level") or "LOCAL"),
            "justification": str(ca.get("justification") or ca.get("reason") or ""),
            "allowed_paths": list(ca.get("allowed_paths") or plan_summary["allowed_paths"]),
        }
    else:
        change_authority_ev = {"status": "Unavailable", "classification": "LOCAL", "reason": "No change authority artifact recorded"}

    # Goal Proof
    gp = _get_artifact_data("goal_proof") or ev_dict.get("goal_proof") or {}
    if gp:
        goal_proof_ev = {
            "status": "Available",
            "entrypoint": gp.get("entrypoint") or (contract.get("proof_contract") or {}).get("entrypoint") or plan_summary["validation_commands"],
            "completion_probe": gp.get("completion_probe") or (contract.get("proof_contract") or {}).get("completion_probe"),
            "proof_passed": bool(gp.get("proof_passed", False)),
            "exit_code": gp.get("entrypoint_exit_code", validation_summary["exit_code"]),
            "path_integrity_status": str(gp.get("path_integrity_status") or path_integrity),
            "reason": str(gp.get("reason") or ""),
        }
        proof_passed = bool(goal_proof_ev["proof_passed"])
    elif terminal_path == "BASELINE_SHORTCUT" or bool(state.get("baseline_shortcut")):
        goal_proof_ev = {
            "status": "Available",
            "entrypoint": (contract.get("proof_contract") or {}).get("entrypoint") or plan_summary["validation_commands"],
            "completion_probe": None,
            "proof_passed": True,
            "exit_code": 0,
            "path_integrity_status": str(path_integrity),
            "reason": "Baseline shortcut verified original code satisfied requirements",
        }
        proof_passed = True
    else:
        val_passed = bool(validation_summary.get("status") == "PASS" and validation_summary.get("exit_code") == 0)
        goal_proof_ev = {
            "status": "Unavailable",
            "proof_passed": val_passed,
            "reason": "Validation exit code 0" if val_passed else "No goal proof artifact recorded",
        }
        proof_passed = val_passed

    # Baseline vs Final Delta
    base_res = _get_artifact_data("baseline_result")
    if base_res:
        base_st = base_res.get("status", "UNKNOWN")
        base_rc = base_res.get("exit_code")
        final_rc = goal_proof_ev.get("exit_code", validation_summary["exit_code"])
        baseline_delta_ev = {
            "status": "Available",
            "baseline_status": base_st,
            "baseline_exit_code": base_rc,
            "final_status": terminal_status,
            "final_exit_code": final_rc,
            "summary": f"Baseline: {base_st} (exit code {base_rc}) -> Final: {terminal_status} (exit code {final_rc})",
        }
    else:
        baseline_delta_ev = {
            "status": "Unavailable",
            "summary": f"Baseline not recorded -> Final: {terminal_status}",
        }

    # Critic Findings
    crit = _get_artifact_data("critic_review") or ev_dict.get("critic") or review.get("critic")
    if isinstance(crit, dict) and crit:
        critic_findings_ev = {
            "status": "Available",
            "findings": list(crit.get("findings") or crit.get("objections") or []),
            "verdict": str(crit.get("verdict") or crit.get("status") or "Completed"),
            "resolutions": crit.get("resolutions") or crit.get("convergence") or "Adversarial review completed",
        }
    else:
        critic_findings_ev = {"status": "Unavailable", "reason": "No critic review findings recorded"}

    # Backtracking
    reasoning_trajectory = list(state.get("reasoning_history") or [])
    backtrack_steps = []
    for step in reasoning_trajectory:
        action_name = str(step.get("action") or step.get("decision") or "")
        if "RETURN" in action_name or "BACKTRACK" in action_name:
            backtrack_steps.append({
                "activity": step.get("activity") or step.get("current_activity"),
                "action": action_name,
                "reason": step.get("reason", ""),
                "at": step.get("at", ""),
            })
    backtracking_ev = {
        "occurred": len(backtrack_steps) > 0 or execution_rounds > 1,
        "steps": backtrack_steps,
        "summary": f"{len(backtrack_steps)} backtracking event(s) recorded" if backtrack_steps else "No backtracking occurred",
    }

    # Risks
    preflight = _get_artifact_data("mutation_preflight") or {}
    risks_ev = {
        "risk_tags": list(plan_summary.get("risk_tags") or []),
        "preflight_safe": bool(preflight.get("safe", True)),
        "issues": list(preflight.get("issues") or []),
    }

    # Artifact References
    art_refs = []
    for art_type, entry in sorted((state.get("artifacts") or {}).items()):
        if isinstance(entry, dict) and entry.get("ref"):
            art_refs.append(f"{art_type}.v{entry.get('version', 1)}: {entry['ref']}")

    phase5_evidence = {
        "baseline_delta": baseline_delta_ev,
        "diagnosis": diagnosis_ev,
        "solution_candidates": solutions_ev,
        "change_authority": change_authority_ev,
        "goal_proof": goal_proof_ev,
        "critic_findings": critic_findings_ev,
        "backtracking": backtracking_ev,
        "risks": risks_ev,
        "artifact_references": art_refs,
    }

    return phase5_evidence, proof_passed

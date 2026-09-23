"""Deterministic final report generation (structured JSON + Obsidian-compatible Markdown)."""
from __future__ import annotations

import contextlib
import dataclasses
from typing import TYPE_CHECKING, Any

from labeeb.core.artifacts import FINAL_REPORT
from labeeb.core.report_templates import render_markdown
from labeeb.models import (
    ArtifactStatus,
    ArtifactValidity,
    PathIntegrityStatus,
    utc_now,
)
from labeeb.storage.goal_store import (
    atomic_json_write,
    atomic_text_write,
    file_ref,
    read_ref_json,
)

if TYPE_CHECKING:
    from labeeb.core.controller import LabeebController


@dataclasses.dataclass
class FinalReport:
    """In-memory representation of compiled final report."""
    goal_id: str
    status: str
    reason: str
    intent: str
    terminal_path: str
    macro_phase: str
    created_at: str
    completed_at: str
    duration_seconds: float
    execution_rounds: int
    max_execution_rounds: int
    repair_reserved: bool
    proof_passed: bool
    path_integrity_status: str
    contract_summary: dict[str, Any]
    plan_summary: dict[str, Any]
    validation_summary: dict[str, Any]
    patch_summary: dict[str, Any]
    artifacts_provenance: list[dict[str, Any]]
    reasoning_trajectory: list[dict[str, Any]]
    phase5_evidence: dict[str, Any] = dataclasses.field(default_factory=dict)
    markdown_content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "status": self.status,
            "reason": self.reason,
            "intent": self.intent,
            "terminal_path": self.terminal_path,
            "macro_phase": self.macro_phase,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "execution_rounds": self.execution_rounds,
            "max_execution_rounds": self.max_execution_rounds,
            "repair_reserved": self.repair_reserved,
            "proof_passed": self.proof_passed,
            "path_integrity_status": self.path_integrity_status,
            "contract_summary": self.contract_summary,
            "plan_summary": self.plan_summary,
            "validation_summary": self.validation_summary,
            "patch_summary": self.patch_summary,
            "artifacts_provenance": self.artifacts_provenance,
            "reasoning_trajectory": self.reasoning_trajectory,
            "phase5_evidence": self.phase5_evidence,
        }


class FinalReportGenerator:
    """100% deterministic report compiler with zero runtime LLM dependencies."""

    def __init__(self, ctl: LabeebController) -> None:
        self.ctl = ctl

    def generate_and_save(
        self,
        state: dict[str, Any],
        terminal_status: str,
        reason: str = "",
        evidence: Any = None,
    ) -> tuple[dict[str, Any], str]:
        """Compile and persist both JSON and Obsidian Markdown final reports."""
        state["macro_phase"] = "REPORTING"
        report = self.compile_report(state, terminal_status, reason, evidence)
        report_dict = report.to_dict()
        report_md = report.markdown_content

        # 1. Write immutable versioned artifact into GoalArtifactStore
        with contextlib.suppress(Exception):
            self.ctl.artifact_store.write_artifact(
                state=state,
                artifact_type=FINAL_REPORT,
                data=report_dict,
                producer="controller",
                allow_stale_dependency=True,
            )

        # 2. Write root final_report.json and final_report.md
        atomic_json_write(self.ctl.paths.final_report_json, report_dict)
        atomic_text_write(self.ctl.paths.final_report_md, report_md)

        state["final_report_ref"] = file_ref(self.ctl.paths.final_report_json)
        state["final_report_md_ref"] = file_ref(self.ctl.paths.final_report_md)

        self.ctl.record_event(
            "reporting.completed",
            {
                "status": terminal_status,
                "terminal_path": report.terminal_path,
                "execution_rounds": report.execution_rounds,
                "artifacts_count": len(report.artifacts_provenance),
                "duration_seconds": report.duration_seconds,
            },
        )

        return report_dict, report_md

    def compile_report(
        self,
        state: dict[str, Any],
        terminal_status: str,
        reason: str = "",
        evidence: Any = None,
    ) -> FinalReport:
        """Deterministically compile report fields and markdown representation."""
        goal_id = str(self.ctl.goal_id)
        created_at = str(state.get("created_at") or utc_now())
        completed_at = utc_now()

        # Calculate duration
        duration_seconds = 0.0
        try:
            from datetime import datetime
            t0 = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            duration_seconds = max(0.0, round((t1 - t0).total_seconds(), 2))
        except Exception:
            duration_seconds = 0.0

        # Safe contract parsing
        contract: dict[str, Any] = {}
        if state.get("contract_ref"):
            with contextlib.suppress(Exception):
                contract = read_ref_json(state["contract_ref"])

        # Safe plan parsing
        plan: dict[str, Any] = {}
        if state.get("plan_ref"):
            with contextlib.suppress(Exception):
                plan = read_ref_json(state["plan_ref"])

        # Safe evidence parsing
        ev_dict: dict[str, Any] = evidence if isinstance(evidence, dict) else {}
        review: dict[str, Any] = {}
        if state.get("review_ref"):
            with contextlib.suppress(Exception):
                review = read_ref_json(state["review_ref"])

        # Terminal path derivation
        if state.get("baseline_shortcut") or ev_dict.get("terminal_path") == "BASELINE_SHORTCUT":
            terminal_path = "BASELINE_SHORTCUT"
        elif ev_dict.get("terminal_path"):
            terminal_path = str(ev_dict["terminal_path"])
        else:
            terminal_path = "STANDARD_EXECUTION"

        def _get_artifact_data(art_type: str) -> dict[str, Any] | None:
            entry = (state.get("artifacts") or {}).get(art_type)
            if isinstance(entry, dict) and entry.get("ref"):
                with contextlib.suppress(Exception):
                    d = read_ref_json(entry["ref"]).get("data")
                    if isinstance(d, dict):
                        return d
            return None

        # 1. Resolve Contract Fields (nested goal_contract or artifact takes precedence over top-level fallback)
        gc = contract.get("goal_contract") if isinstance(contract.get("goal_contract"), dict) else {}
        gc_art_data: dict[str, Any] = _get_artifact_data("goal_contract") or {}

        intent = str(contract.get("intent") or state.get("intent") or "Unspecified engineering goal")
        observable_outcome = str(
            gc.get("observable_outcome")
            or gc_art_data.get("observable_outcome")
            or contract.get("observable_outcome")
            or "Observable outcome not specified"
        )
        expected_behavior = str(
            gc.get("expected_behavior")
            or gc_art_data.get("expected_behavior")
            or contract.get("expected_behavior")
            or "Expected behavior not specified"
        )
        current_behavior = str(
            gc.get("current_behavior")
            or gc_art_data.get("current_behavior")
            or contract.get("current_behavior")
            or "Baseline defect not specified"
        )
        acceptance_criteria = list(
            gc.get("acceptance_criteria")
            or gc_art_data.get("acceptance_criteria")
            or contract.get("acceptance_criteria")
            or []
        )
        constraints = list(
            gc.get("constraints")
            or gc_art_data.get("constraints")
            or contract.get("constraints")
            or []
        )
        non_goals = list(
            gc.get("non_goals")
            or gc_art_data.get("non_goals")
            or contract.get("non_goals")
            or []
        )

        execution_rounds = int(state.get("execution_rounds", 0))
        max_execution_rounds = int(self.ctl.config.get("planning.max_execution_rounds", 2))
        repair_reserved = bool(state.get("repair_reserved", False))
        path_integrity = str(state.get("path_integrity_status") or PathIntegrityStatus.ORIGINAL)

        # Plan summary
        execution = plan.get("execution") or {}
        plan_summary = {
            "summary": str(plan.get("plan_summary") or "No plan summary recorded"),
            "allowed_paths": list(execution.get("allowed_paths") or contract.get("allowed_paths") or []),
            "validation_commands": list(execution.get("validation_commands") or contract.get("validation_commands") or []),
            "risk_tags": list(execution.get("risk_tags") or contract.get("risk_tags") or []),
        }

        # Validation summary
        val_data = ev_dict.get("validation") or review.get("validation") or {}
        has_val = bool(val_data)
        explicit_val_status = val_data.get("status")
        val_exit_code = val_data.get("exit_code") if has_val else None

        if not has_val:
            val_status = "UNEXECUTED"
        elif explicit_val_status is not None:
            val_status = str(explicit_val_status)
        elif val_exit_code == 0:
            val_status = "PASS"
        else:
            val_status = "UNKNOWN"

        validation_summary = {
            "status": val_status,
            "exit_code": val_exit_code,
            "duration_seconds": float(val_data.get("duration_seconds") or 0.0),
            "commands": list(val_data.get("commands") or plan_summary["validation_commands"]),
            "error": val_data.get("error"),
            "stdout_preview": str(val_data.get("stdout") or "")[:2000],
            "stderr_preview": str(val_data.get("stderr") or "")[:2000],
        }

        # Patch summary
        patch_summary = {
            "session_id": state.get("jules_session_id"),
            "patch_hash": ev_dict.get("patch_hash") or review.get("patch_hash"),
            "base_commit": ev_dict.get("base_commit") or review.get("base_commit"),
            "files": list(ev_dict.get("files") or review.get("files") or []),
        }

        # Artifacts provenance
        artifacts_provenance: list[dict[str, Any]] = []
        raw_artifacts = state.get("artifacts") or {}
        for art_type, entry in sorted(raw_artifacts.items()):
            if isinstance(entry, dict):
                artifacts_provenance.append(
                    {
                        "artifact_type": art_type,
                        "version": entry.get("version", 1),
                        "status": entry.get("activity_status") or entry.get("status", ArtifactStatus.SATISFIED),
                        "validity": entry.get("validity", ArtifactValidity.VALID),
                        "sha256": entry.get("sha256", "")[:12],
                        "updated_at": entry.get("updated_at", ""),
                        "not_applicable_reason": entry.get("not_applicable_reason"),
                        "ref": entry.get("ref", ""),
                    }
                )

        # Reasoning trajectory
        reasoning_trajectory = list(state.get("reasoning_history") or [])

        # Contract summary
        contract_summary = {
            "repo": contract.get("repo", "local"),
            "branch": contract.get("branch", "main"),
            "workspace": contract.get("workspace", ""),
            "observable_outcome": observable_outcome,
            "expected_behavior": expected_behavior,
            "current_behavior": current_behavior,
            "acceptance_criteria": acceptance_criteria,
            "constraints": constraints,
            "non_goals": non_goals,
        }

        # 2. Extract Phase 5 Evidence
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
            # When no goal proof artifact is recorded, do NOT fall back to terminal_status.
            # Only consider proof passed if explicit validation evidence passed with exit code 0.
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

        # Compile markdown
        markdown_content = render_markdown(
            goal_id=goal_id,
            status=terminal_status,
            reason=reason,
            intent=intent,
            terminal_path=terminal_path,
            created_at=created_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            execution_rounds=execution_rounds,
            max_execution_rounds=max_execution_rounds,
            repair_reserved=repair_reserved,
            proof_passed=proof_passed,
            path_integrity=path_integrity,
            contract_summary=contract_summary,
            plan_summary=plan_summary,
            validation_summary=validation_summary,
            patch_summary=patch_summary,
            artifacts_provenance=artifacts_provenance,
            reasoning_trajectory=reasoning_trajectory,
            phase5_evidence=phase5_evidence,
        )

        return FinalReport(
            goal_id=goal_id,
            status=terminal_status,
            reason=reason,
            intent=intent,
            terminal_path=terminal_path,
            macro_phase="REPORTING",
            created_at=created_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            execution_rounds=execution_rounds,
            max_execution_rounds=max_execution_rounds,
            repair_reserved=repair_reserved,
            proof_passed=proof_passed,
            path_integrity_status=path_integrity,
            contract_summary=contract_summary,
            plan_summary=plan_summary,
            validation_summary=validation_summary,
            patch_summary=patch_summary,
            artifacts_provenance=artifacts_provenance,
            reasoning_trajectory=reasoning_trajectory,
            phase5_evidence=phase5_evidence,
            markdown_content=markdown_content,
        )

"""Deterministic final report generation (structured JSON + Obsidian-compatible Markdown)."""
from __future__ import annotations

import contextlib
import dataclasses
from typing import TYPE_CHECKING, Any

from labeeb.core.artifacts import FINAL_REPORT
from labeeb.core.report_evidence import compile_phase5_evidence
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

        # 2. Extract Phase 5 Evidence via report_evidence helper
        phase5_evidence, proof_passed = compile_phase5_evidence(
            state=state,
            contract=contract,
            plan_summary=plan_summary,
            validation_summary=validation_summary,
            ev_dict=ev_dict,
            review=review,
            terminal_status=terminal_status,
            terminal_path=terminal_path,
            execution_rounds=execution_rounds,
            path_integrity=path_integrity,
            get_artifact_data_fn=_get_artifact_data,
        )

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

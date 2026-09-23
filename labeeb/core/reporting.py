"""Deterministic final report generation (structured JSON + Obsidian-compatible Markdown)."""
from __future__ import annotations

import contextlib
import dataclasses
from typing import TYPE_CHECKING, Any

from labeeb.core.artifacts import FINAL_REPORT
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
        markdown_content = self.render_markdown(
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

    def render_markdown(
        self,
        *,
        goal_id: str,
        status: str,
        reason: str,
        intent: str,
        terminal_path: str,
        created_at: str,
        completed_at: str,
        duration_seconds: float,
        execution_rounds: int,
        max_execution_rounds: int,
        repair_reserved: bool,
        proof_passed: bool,
        path_integrity: str,
        contract_summary: dict[str, Any],
        plan_summary: dict[str, Any],
        validation_summary: dict[str, Any],
        patch_summary: dict[str, Any],
        artifacts_provenance: list[dict[str, Any]],
        reasoning_trajectory: list[dict[str, Any]],
        phase5_evidence: dict[str, Any] | None = None,
    ) -> str:
        """Render rich, Obsidian-compatible markdown."""
        status_upper = status.upper()
        tag_status = status.lower()
        ev = phase5_evidence or {}

        # Banner style
        if status_upper == "PASS":
            callout = f"> [!SUCCESS] Goal PASSED\n> **Outcome**: {contract_summary.get('observable_outcome') or 'Criteria fully evidenced.'}"
        elif status_upper == "FAIL":
            callout = f"> [!FAILURE] Goal FAILED\n> **Reason**: {reason or 'Validation or budget constraint violated.'}"
        else:
            callout = f"> [!WARNING] Goal BLOCKED\n> **Reason**: {reason or 'Human escalation or safety constraint boundary reached.'}"

        # Markdown lines
        lines: list[str] = [
            "---",
            f'goal_id: "{goal_id}"',
            f'status: "{status_upper}"',
            'macro_phase: "REPORTING"',
            f'terminal_path: "{terminal_path}"',
            f'created_at: "{created_at}"',
            f'completed_at: "{completed_at}"',
            f"duration_seconds: {duration_seconds}",
            f"execution_rounds: {execution_rounds}",
            f"repair_reserved: {str(repair_reserved).lower()}",
            f"proof_passed: {str(proof_passed).lower()}",
            f'path_integrity: "{path_integrity}"',
            "tags:",
            "  - labeeb-goal",
            f"  - status/{tag_status}",
            f"  - path/{terminal_path.lower()}",
            "---",
            "",
            f"# Goal Report: {intent}",
            "",
            callout,
            "",
            "## 1. Executive Summary",
            "",
            "| Metric | Value |",
            "| :--- | :--- |",
            f"| **Goal ID** | `{goal_id}` |",
            f"| **Terminal Verdict** | **{status_upper}** |",
            f"| **Terminal Path** | `{terminal_path}` |",
            f"| **Execution Rounds** | `{execution_rounds} / {max_execution_rounds}` |",
            f"| **Targeted Repairs** | `{'1 / 1' if repair_reserved else '0 / 1'}` |",
            f"| **Proof Path Integrity** | `{path_integrity}` |",
            f"| **Total Duration** | `{duration_seconds}s` |",
            f"| **Artifacts Generated** | `{len(artifacts_provenance)}` |",
            "",
            "## 2. Goal Contract & Acceptance Criteria",
            "",
            f"- **Repository**: `{contract_summary.get('repo')}` (`{contract_summary.get('branch')}`)",
            f"- **Workspace**: `{contract_summary.get('workspace')}`",
            f"- **Current Defect / Baseline**: {contract_summary.get('current_behavior')}",
            f"- **Expected Behavior**: {contract_summary.get('expected_behavior')}",
            "",
            "### Acceptance Criteria Checklist",
        ]

        criteria = contract_summary.get("acceptance_criteria") or []
        if criteria:
            for item in criteria:
                mark = "[x]" if proof_passed else "[ ]"
                lines.append(f"- {mark} {item}")
        else:
            lines.append("- *(No explicit acceptance criteria specified in contract)*")

        lines.extend([
            "",
            "### Constraints & Non-Goals",
        ])
        constraints = contract_summary.get("constraints") or []
        if constraints:
            for c in constraints:
                lines.append(f"- ⚠️ **Constraint**: {c}")
        non_goals = contract_summary.get("non_goals") or []
        if non_goals:
            for ng in non_goals:
                lines.append(f"- 🚫 **Non-Goal**: {ng}")
        if not constraints and not non_goals:
            lines.append("- *(Standard repository bounds apply)*")

        # Section 3: Deterministic Validation & Proof
        lines.extend([
            "",
            "## 3. Deterministic Validation & Proof",
            "",
            f"- **Validation Status**: `{validation_summary.get('status')}`",
            f"- **Subprocess Exit Code**: `{'N/A' if validation_summary.get('exit_code') is None else validation_summary.get('exit_code')}`",
            f"- **Execution Duration**: `{validation_summary.get('duration_seconds')}s`",
        ])
        gp_ev = ev.get("goal_proof") or {}
        if gp_ev.get("status") == "Available":
            lines.append(f"- **Goal Proof Passed**: `{gp_ev.get('proof_passed')}`")
            lines.append(f"- **Proof Entrypoint**: `{gp_ev.get('entrypoint')}`")
            if gp_ev.get("completion_probe"):
                lines.append(f"- **Completion Probe**: `{gp_ev.get('completion_probe')}`")
            lines.append(f"- **Path Integrity**: `{gp_ev.get('path_integrity_status')}`")
            if gp_ev.get("reason"):
                lines.append(f"- **Diagnostic Note**: {gp_ev['reason']}")
        elif gp_ev.get("proof_passed") is True:
            lines.append(f"- **Goal Proof Passed**: `True` *(Legacy flow / {gp_ev.get('reason', 'Validation exit code 0')})*")
        else:
            lines.append(f"- **Goal Proof Passed**: `Unavailable (Unverified)` *({gp_ev.get('reason', 'No goal proof artifact recorded')})*")

        lines.extend([
            "",
            "### Commands Executed",
        ])
        cmds = validation_summary.get("commands") or []
        if cmds:
            for cmd in cmds:
                lines.append(f"```bash\n{cmd}\n```")
        else:
            lines.append("*(No validation commands executed)*")

        if validation_summary.get("stdout_preview"):
            lines.extend([
                "",
                "### Validation Output (Stdout Preview)",
                "```text",
                validation_summary["stdout_preview"],
                "```",
            ])
        if validation_summary.get("stderr_preview"):
            lines.extend([
                "",
                "### Validation Diagnostic (Stderr Preview)",
                "```text",
                validation_summary["stderr_preview"],
                "```",
            ])

        # Section 4: Implementation Telemetry
        lines.extend([
            "",
            "## 4. Implementation & Code Mutation Telemetry",
            "",
            f"- **Jules Session**: `{patch_summary.get('session_id') or 'N/A (No external mutator session)'}`",
            f"- **Patch SHA256**: `{patch_summary.get('patch_hash') or 'N/A'}`",
            f"- **Base Commit**: `{patch_summary.get('base_commit') or 'N/A'}`",
            "",
            "### Files Modified",
        ])
        files = patch_summary.get("files") or []
        if files:
            for f in files:
                lines.append(f"- `{f}`")
        else:
            if terminal_path == "BASELINE_SHORTCUT":
                lines.append("*(Zero code mutations applied: baseline shortcut verified original code satisfied requirements)*")
            else:
                lines.append("*(No files modified)*")

        # Section 5: Artifact Provenance Index
        lines.extend([
            "",
            "## 5. Artifact Provenance & Reasoning Graph",
            "",
            "| Artifact Type | Version | Activity Status | Validity | SHA256 | Updated At |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        if artifacts_provenance:
            for art in artifacts_provenance:
                lines.append(
                    f"| `{art['artifact_type']}` | `v{art['version']}` | `{art['status']}` | `{art['validity']}` | `{art['sha256']}` | {art['updated_at']} |"
                )
        else:
            lines.append("| *(None)* | - | - | - | - | - |")

        # Section 6: Reasoning Trajectory Timeline
        lines.extend([
            "",
            "## 6. Reasoning Trajectory Timeline",
            "",
            "| Step | Activity | Status | Target Next | Timestamp |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        if reasoning_trajectory:
            for idx, step in enumerate(reasoning_trajectory, start=1):
                act = step.get("activity", "unknown")
                st = step.get("status", "SATISFIED")
                nxt = step.get("next_activity") or "-"
                at = step.get("at", "")
                lines.append(f"| {idx} | `{act}` | `{st}` | `{nxt}` | {at} |")
        else:
            lines.append("| 1 | *(Direct execution / legacy flow)* | - | - | - |")

        # Section 7: Reality Audit & Diagnosis
        lines.extend([
            "",
            "## 7. Reality Audit & Diagnosis",
            "",
        ])
        diag_ev = ev.get("diagnosis") or {}
        if diag_ev.get("status") == "Available":
            lines.append(f"- **Root Cause**: {diag_ev.get('root_cause')}")
            symptoms = diag_ev.get("failure_symptoms") or []
            if symptoms:
                lines.append(f"- **Observed Symptoms**: {', '.join(str(s) for s in symptoms)}")
            chain = diag_ev.get("causal_chain") or []
            if chain:
                lines.append("- **Causal Chain**:")
                for item in chain:
                    lines.append(f"  - {item}")
        else:
            lines.append(f"- *(Diagnosis details unavailable: {diag_ev.get('reason', 'Not recorded')})*")

        base_ev = ev.get("baseline_delta") or {}
        lines.append(f"- **Baseline vs Final Delta**: {base_ev.get('summary', 'Unavailable')}")

        # Section 8: Solution Exploration & Approach Selection
        lines.extend([
            "",
            "## 8. Solution Exploration & Approach Selection",
            "",
        ])
        sol_ev = ev.get("solution_candidates") or {}
        if sol_ev.get("status") == "Available":
            lines.append(f"- **Selected Approach**: {sol_ev.get('selected_approach')}")
            alts = sol_ev.get("rejected_alternatives") or []
            if alts:
                lines.append("- **Evaluated & Rejected Alternatives**:")
                for alt in alts:
                    lines.append(f"  - {alt}")
            if sol_ev.get("trade_offs"):
                lines.append(f"- **Trade-Offs**: {sol_ev['trade_offs']}")
        else:
            lines.append(f"- *(Solution exploration unavailable: {sol_ev.get('reason', 'Not recorded')})*")

        # Section 9: Change Authority & Scope Bounds
        lines.extend([
            "",
            "## 9. Change Authority & Blast Radius",
            "",
        ])
        ca_ev = ev.get("change_authority") or {}
        if ca_ev.get("status") == "Available":
            lines.append(f"- **Authority Classification**: `{ca_ev.get('classification')}`")
            if ca_ev.get("justification"):
                lines.append(f"- **Justification**: {ca_ev['justification']}")
        else:
            lines.append(f"- **Authority Classification**: `{ca_ev.get('classification', 'LOCAL')}` *(default / {ca_ev.get('reason', 'Not recorded')})*")
        paths_bound = plan_summary.get("allowed_paths") or []
        lines.append(f"- **Allowed Scope Bounds**: {', '.join(f'`{p}`' for p in paths_bound) if paths_bound else '*(No path restrictions)*'}")

        # Section 10: Adversarial Critic Review
        lines.extend([
            "",
            "## 10. Adversarial Critic Review",
            "",
        ])
        crit_ev = ev.get("critic_findings") or {}
        if crit_ev.get("status") == "Available":
            lines.append(f"- **Review Verdict**: `{crit_ev.get('verdict')}`")
            findings = crit_ev.get("findings") or []
            if findings:
                lines.append("- **Findings & Objections**:")
                for fnd in findings:
                    lines.append(f"  - {fnd}")
            if crit_ev.get("resolutions"):
                lines.append(f"- **Reconciliation / Resolution**: {crit_ev['resolutions']}")
        else:
            lines.append(f"- *(Adversarial review findings unavailable: {crit_ev.get('reason', 'Not recorded')})*")

        # Section 11: Backtracking & Anti-Loop History
        lines.extend([
            "",
            "## 11. Backtracking & Anti-Loop History",
            "",
        ])
        bt_ev = ev.get("backtracking") or {}
        lines.append(f"- **Backtracking Summary**: {bt_ev.get('summary', 'No backtracking occurred')}")
        if bt_ev.get("steps"):
            for s in bt_ev["steps"]:
                lines.append(f"- Round {s.get('round', 1)}: `{s.get('action')}` ({s.get('reason')})")

        lines.append("")
        return "\n".join(lines)

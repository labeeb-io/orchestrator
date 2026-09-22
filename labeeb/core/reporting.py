"""Deterministic final report generation (structured JSON + Obsidian-compatible Markdown)."""
from __future__ import annotations

import contextlib
import dataclasses
import pathlib
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
            from datetime import datetime, timezone
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

        intent = str(contract.get("intent") or state.get("intent") or "Unspecified engineering goal")
        observable_outcome = str(contract.get("observable_outcome") or "Observable outcome not specified")
        expected_behavior = str(contract.get("expected_behavior") or "Expected behavior not specified")
        current_behavior = str(contract.get("current_behavior") or "Baseline defect not specified")
        acceptance_criteria = list(contract.get("acceptance_criteria") or [])
        constraints = list(contract.get("constraints") or [])
        non_goals = list(contract.get("non_goals") or [])

        execution_rounds = int(state.get("execution_rounds", 0))
        max_execution_rounds = int(self.ctl.config.get("planning.max_execution_rounds", 2))
        repair_reserved = bool(state.get("repair_reserved", False))
        proof_passed = terminal_status == "PASS"
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
        validation_summary = {
            "status": str(val_data.get("status") or ("PASS" if proof_passed else "UNKNOWN")),
            "exit_code": val_data.get("exit_code", 0 if proof_passed else 1),
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
                        "status": entry.get("status", ArtifactStatus.SATISFIED),
                        "validity": entry.get("validity", ArtifactValidity.VALID),
                        "sha256": entry.get("sha256", "")[:12],
                        "updated_at": entry.get("updated_at", ""),
                        "not_applicable_reason": entry.get("not_applicable_reason"),
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
    ) -> str:
        """Render rich, Obsidian-compatible markdown."""
        status_upper = status.upper()
        tag_status = status.lower()

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

        lines.extend([
            "",
            "## 3. Deterministic Validation & Proof",
            "",
            f"- **Validation Status**: `{validation_summary.get('status')}`",
            f"- **Subprocess Exit Code**: `{validation_summary.get('exit_code')}`",
            f"- **Execution Duration**: `{validation_summary.get('duration_seconds')}s`",
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

        lines.append("")
        return "\n".join(lines)

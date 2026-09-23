"""Obsidian-compatible Markdown template renderer for FinalReport."""
from __future__ import annotations

from typing import Any


def render_markdown(
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

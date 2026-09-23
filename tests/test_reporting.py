"""Phase 5 Deterministic Final Reporting test suite."""
from __future__ import annotations

import pathlib
import tempfile
from typing import Any
import pytest

from labeeb.config import Config
from labeeb.errors import ControllerError
from labeeb.core.artifacts import (
    AUTHORITY_CONTEXT,
    BASELINE_RESULT,
    FINAL_REPORT,
    GOAL_CONTRACT,
    PROOF_CONTRACT,
    REALITY_AUDIT,
    SOLUTION_CANDIDATES,
)
from labeeb.core.controller import LabeebController
from labeeb.core.reporting import FinalReportGenerator
from labeeb.models import (
    PathIntegrityStatus,
)
from labeeb.storage.goal_store import read_ref_json


@pytest.fixture
def controller():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        config = Config(
            root / "config.toml",
            {
                "controller": {"state_root": str(root)},
                "planning": {"max_execution_rounds": 2},
                "roles": {
                    "brain": {"transport": "orchestrator", "runtime": "codex"},
                    "critic": {"transport": "direct", "command": ["false"]},
                    "implementer": {"transport": "jules", "command": "cjules"},
                },
            },
        )
        yield LabeebController.create_goal(
            config,
            intent="Fix a bounded local defect in parser",
            workspace=str(root),
            repo="owner/repo",
            branch="main",
            risk_tags=["parser"],
            allowed_paths=["src", "tests"],
            validation_commands=[".venv/bin/pytest tests/test_parser.py"],
            preauthorize_plan=True,
        )


def _populate_test_goal(ctl: LabeebController, state: dict[str, Any]) -> None:
    contract = read_ref_json(state["contract_ref"])
    contract["observable_outcome"] = "Parser correctly handles UTF-8 BOM"
    contract["expected_behavior"] = "BOM is stripped before parsing"
    contract["current_behavior"] = "BOM causes UnicodeDecodeError"
    contract["acceptance_criteria"] = [
        "test_parser_utf8_bom passes",
        "no regression on ascii files",
    ]
    contract["constraints"] = ["Do not introduce third-party libraries"]
    contract["non_goals"] = ["Support UTF-16 or UTF-32"]
    state["contract_ref"] = ctl.store.write_json(ctl.paths.contract, contract)

    plan = {
        "plan_summary": "Update strip_bom function in src/parser.py",
        "execution": {
            "allowed_paths": ["src/parser.py", "tests/test_parser.py"],
            "validation_commands": [".venv/bin/pytest tests/test_parser.py"],
            "risk_tags": ["parser"],
        },
    }
    state["plan_ref"] = ctl.store.write_json(ctl.paths.plan, plan)

    write = ctl.artifact_store.write_artifact
    write(state, AUTHORITY_CONTEXT, {}, "controller")
    write(state, GOAL_CONTRACT, contract, "brain")
    write(state, PROOF_CONTRACT, {"entrypoint": ".venv/bin/pytest tests/test_parser.py"}, "brain")
    write(state, REALITY_AUDIT, {"target_file": "src/parser.py"}, "brain")
    write(state, BASELINE_RESULT, {"status": "FAIL", "exit_code": 1}, "brain")
    write(state, SOLUTION_CANDIDATES, {"approach": "codecs.BOM_UTF8 check"}, "brain")

    state["reasoning_history"] = [
        {"activity": "authority_context", "status": "SATISFIED", "next_activity": "goal_contract", "at": "2026-09-23T00:00:00Z"},
        {"activity": "goal_contract", "status": "SATISFIED", "next_activity": "proof_contract", "at": "2026-09-23T00:00:10Z"},
        {"activity": "proof_contract", "status": "SATISFIED", "next_activity": "reality_audit", "at": "2026-09-23T00:00:20Z"},
    ]


def test_final_report_generation_on_pass(controller):
    state = controller.store.load()
    _populate_test_goal(controller, state)
    state["execution_rounds"] = 1
    state["repair_reserved"] = False
    state["path_integrity_status"] = PathIntegrityStatus.ORIGINAL

    evidence = {
        "patch_hash": "sha256-abcdef123456",
        "files": ["src/parser.py", "tests/test_parser.py"],
        "base_commit": "commit-1234",
        "validation": {
            "status": "PASS",
            "exit_code": 0,
            "duration_seconds": 1.45,
            "commands": [".venv/bin/pytest tests/test_parser.py"],
            "stdout": "================ 2 passed in 0.12s ================",
            "stderr": "",
        },
    }

    controller.pass_goal(
        state,
        {"reason": "All 2 acceptance criteria verified with exit code 0"},
        evidence,
    )

    # 1. State assertions
    assert state["phase"] == "PASS"
    assert state["macro_phase"] == "REPORTING"
    assert FINAL_REPORT in state.get("artifacts", {})
    assert state.get("final_report_ref") is not None
    assert state.get("final_report_md_ref") is not None

    # 2. JSON report file assertions
    assert controller.paths.final_report_json.exists()
    report_json = read_ref_json(state["final_report_ref"])
    assert report_json["status"] == "PASS"
    assert report_json["goal_id"] == controller.goal_id
    assert report_json["proof_passed"] is True
    assert report_json["execution_rounds"] == 1
    assert report_json["repair_reserved"] is False
    assert report_json["validation_summary"]["status"] == "PASS"
    assert report_json["validation_summary"]["exit_code"] == 0
    assert len(report_json["artifacts_provenance"]) >= 6

    # 3. Markdown report file assertions
    assert controller.paths.final_report_md.exists()
    md_text = controller.paths.final_report_md.read_text(encoding="utf-8")
    assert 'status: "PASS"' in md_text
    assert "> [!SUCCESS] Goal PASSED" in md_text
    assert "## 1. Executive Summary" in md_text
    assert "## 2. Goal Contract & Acceptance Criteria" in md_text
    assert "- [x] test_parser_utf8_bom passes" in md_text
    assert "- [x] no regression on ascii files" in md_text
    assert "⚠️ **Constraint**: Do not introduce third-party libraries" in md_text
    assert "🚫 **Non-Goal**: Support UTF-16 or UTF-32" in md_text
    assert "## 3. Deterministic Validation & Proof" in md_text
    assert "================ 2 passed in 0.12s ================" in md_text
    assert "## 4. Implementation & Code Mutation Telemetry" in md_text
    assert "src/parser.py" in md_text
    assert "## 5. Artifact Provenance & Reasoning Graph" in md_text
    assert "## 6. Reasoning Trajectory Timeline" in md_text

    # 4. Status method includes final report
    st = controller.status()
    assert st["final_report"]["status"] == "PASS"
    assert "Goal Report" in st["final_report_md"]


def test_final_report_generation_on_fail(controller):
    state = controller.store.load()
    _populate_test_goal(controller, state)
    state["execution_rounds"] = 2
    state["repair_reserved"] = True

    evidence = {
        "validation": {
            "status": "FAIL",
            "exit_code": 1,
            "duration_seconds": 2.1,
            "commands": [".venv/bin/pytest tests/test_parser.py"],
            "stdout": "FAILED tests/test_parser.py::test_parser_utf8_bom",
            "stderr": "AssertionError: Expected None, got UnicodeDecodeError",
        },
    }

    controller.fail(
        state,
        "Validation failed: test_parser_utf8_bom asserted failure",
        evidence=evidence,
    )

    assert state["phase"] == "FAIL"
    assert state["macro_phase"] == "REPORTING"
    assert controller.paths.final_report_md.exists()

    md_text = controller.paths.final_report_md.read_text(encoding="utf-8")
    assert 'status: "FAIL"' in md_text
    assert "> [!FAILURE] Goal FAILED" in md_text
    assert "- [ ] test_parser_utf8_bom passes" in md_text
    assert "AssertionError: Expected None, got UnicodeDecodeError" in md_text


def test_final_report_generation_on_block(controller):
    state = controller.store.load()
    _populate_test_goal(controller, state)

    controller.block(
        state,
        "Gated boundary reached: Requires human authorization for production ref mutation",
    )

    assert state["phase"] == "BLOCKED"
    assert state["macro_phase"] == "REPORTING"
    assert controller.paths.final_report_md.exists()

    md_text = controller.paths.final_report_md.read_text(encoding="utf-8")
    assert 'status: "BLOCKED"' in md_text
    assert "> [!WARNING] Goal BLOCKED" in md_text
    assert "Requires human authorization" in md_text


def test_deterministic_fallback_with_empty_or_corrupt_state(controller):
    # Minimal state without any artifacts or refs
    state = {"phase": "PLANNING", "created_at": "2026-09-23T00:00:00Z"}
    generator = FinalReportGenerator(controller)

    # Must never raise an exception even on completely blank state
    report_dict, report_md = generator.generate_and_save(
        state,
        terminal_status="FAIL",
        reason="Early bootstrap abort",
        evidence=None,
    )

    assert report_dict["status"] == "FAIL"
    assert report_dict["goal_id"] == controller.goal_id
    assert report_dict["artifacts_provenance"] == []
    assert report_dict["reasoning_trajectory"] == []
    assert "Early bootstrap abort" in report_md
    assert "> [!FAILURE] Goal FAILED" in report_md


def test_baseline_shortcut_final_report(controller):
    state = controller.store.load()
    write = controller.artifact_store.write_artifact
    write(state, AUTHORITY_CONTEXT, {}, "controller")
    write(state, GOAL_CONTRACT, {"intent": "Verify smoke test"}, "brain")
    write(state, PROOF_CONTRACT, {"entrypoint": "python -m unittest tests/test_smoke.py"}, "brain")
    write(state, REALITY_AUDIT, {}, "brain")

    controller.complete_baseline_shortcut(
        state,
        {"reason": "Smoke test already green on base commit"},
    )

    assert state["phase"] == "PASS"
    assert state["macro_phase"] == "REPORTING"
    assert state["baseline_shortcut"] is True

    report_json = read_ref_json(state["final_report_ref"])
    assert report_json["terminal_path"] == "BASELINE_SHORTCUT"
    assert report_json["execution_rounds"] == 0
    assert report_json["proof_passed"] is True

    md_text = controller.paths.final_report_md.read_text(encoding="utf-8")
    assert 'terminal_path: "BASELINE_SHORTCUT"' in md_text
    assert "Zero code mutations applied: baseline shortcut verified original code satisfied requirements" in md_text


def test_terminal_result_aborts_if_report_generation_fails(controller):
    """P0 regression: Terminal results must require durable reports.

    If report generation fails, result.json must NOT be written, state phase must NOT
    be set to terminal status, and no recursive block/fail calls must occur.
    """
    state = controller.store.load()
    _populate_test_goal(controller, state)

    # Force report generation to fail
    def _exploding_generate(*args, **kwargs):
        raise OSError("Simulated disk I/O failure during report write")

    controller.report_generator.generate_and_save = _exploding_generate

    # Attempt to fail the goal
    with pytest.raises(ControllerError, match="Durable report generation failed"):
        controller.fail(state, "A planned failure reason")

    # Verify safe failure invariants
    assert not controller.paths.results.exists(), "result.json must not be written if reporting fails"
    assert state.get("phase") != "FAIL", "State phase must not be set to terminal FAIL"
    assert state.get("result_ref") is None, "State result_ref must remain None"

    # Event logged
    events = controller.store.read_events()
    assert any(e.get("event_type") == "reporting.failed" for e in events)


def test_final_report_populates_from_v2_goal_contract_and_evidence(controller):
    """P1 regression: Populate final report from real nested contract and Phase 5 evidence."""
    state = controller.store.load()

    # Nested goal_contract in contract.json
    contract = read_ref_json(state["contract_ref"])
    contract["goal_contract"] = {
        "observable_outcome": "Nested: CLI displays progress bar",
        "expected_behavior": "Nested: Progress bar renders smoothly",
        "current_behavior": "Nested: CLI output is completely silent",
        "acceptance_criteria": [
            "Nested: Progress bar animation reaches 100%",
            "Nested: No flicker in terminal",
        ],
        "constraints": ["Nested: Standard library only"],
        "non_goals": ["Nested: Web GUI rendering"],
    }
    state["contract_ref"] = controller.store.write_json(controller.paths.contract, contract)

    # Write Phase 5 artifacts
    write = controller.artifact_store.write_artifact
    write(state, AUTHORITY_CONTEXT, {}, "controller")
    write(state, GOAL_CONTRACT, contract["goal_contract"], "brain")
    write(state, PROOF_CONTRACT, {"entrypoint": "python -m unittest tests/test_ui.py"}, "brain")
    write(state, REALITY_AUDIT, {"scanned_files": ["ui.py"]}, "brain")
    write(state, BASELINE_RESULT, {"status": "FAIL", "exit_code": 2}, "brain")
    write(
        state,
        "diagnosis",
        {
            "root_cause": "Buffer flush not called after write",
            "failure_symptoms": ["Silent stdout", "Zero byte output"],
            "causal_chain": ["sys.stdout.write without sys.stdout.flush"],
        },
        "brain",
    )
    write(
        state,
        "solution_candidates",
        {
            "selected_approach": "Call sys.stdout.flush() on tick",
            "rejected_alternatives": ["Switch to print() with flush=True", "Custom curses UI"],
            "trade_offs": "Minimal overhead, zero dependencies",
        },
        "brain",
    )
    write(
        state,
        "change_authority",
        {
            "classification": "LOCAL",
            "justification": "Touches only ui.py within allowed_paths",
            "allowed_paths": ["src", "tests"],
        },
        "brain",
    )
    write(
        state,
        "critic_review",
        {
            "verdict": "ACCEPTED_WITH_CONVERGENCE",
            "findings": ["Ensure Windows console supports ANSI escapes"],
            "resolutions": "Handled with colorama fallback check",
        },
        "critic",
    )
    write(
        state,
        "goal_proof",
        {
            "entrypoint": "python -m unittest tests/test_ui.py",
            "proof_passed": True,
            "entrypoint_exit_code": 0,
            "path_integrity_status": PathIntegrityStatus.ORIGINAL,
        },
        "controller",
    )

    state["reasoning_history"] = [
        {"activity": "authority_context", "status": "SATISFIED", "at": "2026-09-23T00:00:00Z"},
        {"activity": "diagnosis", "status": "SATISFIED", "at": "2026-09-23T00:01:00Z"},
    ]

    evidence = {
        "validation": {
            "status": "PASS",
            "exit_code": 0,
            "duration_seconds": 1.2,
            "commands": ["python -m unittest tests/test_ui.py"],
            "stdout": "OK",
            "stderr": "",
        },
        "goal_proof": {
            "entrypoint": "python -m unittest tests/test_ui.py",
            "proof_passed": True,
            "entrypoint_exit_code": 0,
            "path_integrity_status": PathIntegrityStatus.ORIGINAL,
        },
    }

    controller.pass_goal(state, {"reason": "Verified UI progress"}, evidence)

    report_json = read_ref_json(state["final_report_ref"])
    cs = report_json["contract_summary"]
    assert cs["observable_outcome"] == "Nested: CLI displays progress bar"
    assert cs["expected_behavior"] == "Nested: Progress bar renders smoothly"
    assert cs["current_behavior"] == "Nested: CLI output is completely silent"
    assert "Nested: Progress bar animation reaches 100%" in cs["acceptance_criteria"]

    p5 = report_json["phase5_evidence"]
    assert p5["diagnosis"]["root_cause"] == "Buffer flush not called after write"
    assert "Silent stdout" in p5["diagnosis"]["failure_symptoms"]
    assert p5["solution_candidates"]["selected_approach"] == "Call sys.stdout.flush() on tick"
    assert len(p5["solution_candidates"]["rejected_alternatives"]) == 2
    assert p5["change_authority"]["classification"] == "LOCAL"
    assert p5["goal_proof"]["proof_passed"] is True
    assert p5["goal_proof"]["entrypoint"] == "python -m unittest tests/test_ui.py"
    assert "Ensure Windows console supports ANSI escapes" in p5["critic_findings"]["findings"]
    assert "Baseline: FAIL (exit code 2) -> Final: PASS (exit code 0)" in p5["baseline_delta"]["summary"]

    md_text = controller.paths.final_report_md.read_text(encoding="utf-8")
    assert "Buffer flush not called after write" in md_text
    assert "Call sys.stdout.flush() on tick" in md_text
    assert "Switch to print() with flush=True" in md_text
    assert "Ensure Windows console supports ANSI escapes" in md_text
    assert "Baseline: FAIL (exit code 2) -> Final: PASS (exit code 0)" in md_text


def test_durable_pending_terminal_recovery_on_report_failure(tmp_path, monkeypatch):
    """Verify that report generation failure durably preserves pending_terminal and resumes to PASS."""
    raw_cfg = {
        "controller": {"state_root": str(tmp_path / "state")},
        "executables": {"orchestrator": "true", "cjules": "true"},
        "roles": {
            "brain": {"transport": "orchestrator", "runtime": "mock"},
            "implementer": {"transport": "jules", "command": "cjules"},
        },
    }
    config = Config(tmp_path / "config.toml", raw_cfg)
    controller = LabeebController.create_goal(
        config,
        intent="Test durable report recovery",
        workspace=str(tmp_path),
        repo="owner/repo",
        branch="main",
        risk_tags=[],
        allowed_paths=[],
        validation_commands=[],
        preauthorize_plan=True,
    )
    state = controller.store.load()
    state["phase"] = "REVIEWING"

    decision = {"action": "PASS", "reason": "All checks passed"}
    evidence = {"validation": {"status": "PASS", "exit_code": 0}}

    # Simulate report generation failure
    def fail_generate(*args, **kwargs):
        raise IOError("Disk full during report generation")

    monkeypatch.setattr(controller.report_generator, "generate_and_save", fail_generate)

    # Calling pass_goal raises ControllerError
    with pytest.raises(ControllerError, match="Durable report generation failed"):
        controller.pass_goal(state, decision, evidence)

    # Verify pending_terminal was durably persisted in state.json
    persisted = controller.store.load()
    assert persisted.get("pending_terminal") is not None
    assert persisted["pending_terminal"]["status"] == "PASS"
    assert persisted["pending_terminal"]["reason"] == "All checks passed"
    assert persisted["phase"] == "REVIEWING"

    # Restore normal report generator and resume controller step
    monkeypatch.undo()
    resumed = controller.step(persisted)
    assert resumed is True

    # Goal is now cleanly PASS, with result.json and cleared pending_terminal
    final_state = controller.store.load()
    assert final_state["phase"] == "PASS"
    assert final_state.get("pending_terminal") is None
    assert controller.paths.results.exists()
    result_data = read_ref_json(final_state["result_ref"])
    assert result_data["status"] == "PASS"
    assert result_data["reason"] == "All checks passed"


def test_accurate_evidence_reporting_without_inference(tmp_path):
    """Verify that absent validation and goal proof are reported as UNEXECUTED / Unavailable without inference."""
    raw_cfg = {
        "controller": {"state_root": str(tmp_path / "state")},
        "executables": {"orchestrator": "true", "cjules": "true"},
        "roles": {
            "brain": {"transport": "orchestrator", "runtime": "mock"},
            "implementer": {"transport": "jules", "command": "cjules"},
        },
    }
    config = Config(tmp_path / "config.toml", raw_cfg)
    controller = LabeebController.create_goal(
        config,
        intent="Test unobserved evidence reporting",
        workspace=str(tmp_path),
        repo="owner/repo",
        branch="main",
        risk_tags=[],
        allowed_paths=[],
        validation_commands=[],
        preauthorize_plan=True,
    )
    state = controller.store.load()

    # Record an artifact with activity_status="NOT_APPLICABLE"
    controller.artifact_store.write_artifact(
        state,
        "product_contract",
        {"status": "NOT_APPLICABLE"},
        "brain",
        activity_status="NOT_APPLICABLE",
        not_applicable_reason="Backend only",
    )

    # Generate report with NO validation evidence and NO goal proof
    report_gen = FinalReportGenerator(controller)
    report_data, _ = report_gen.generate_and_save(state, "PASS", reason="Direct PASS without validation")

    # Assert validation is UNEXECUTED, exit code is None
    val_sum = report_data["validation_summary"]
    assert val_sum["status"] == "UNEXECUTED"
    assert val_sum["exit_code"] is None

    # Assert goal proof is Unavailable and proof_passed is False
    gp = report_data["phase5_evidence"]["goal_proof"]
    assert gp["status"] == "Unavailable"
    assert gp["proof_passed"] is False

    # Assert artifacts provenance accurately preserves activity_status
    prov = {entry["artifact_type"]: entry for entry in report_data["artifacts_provenance"]}
    assert "product_contract" in prov
    assert prov["product_contract"]["status"] == "NOT_APPLICABLE"

    # Assert markdown displays N/A for exit code
    md_text = controller.paths.final_report_md.read_text(encoding="utf-8")
    assert "- **Validation Status**: `UNEXECUTED`" in md_text
    assert "- **Subprocess Exit Code**: `N/A`" in md_text
    assert "- **Goal Proof Passed**: `Unavailable (Unverified)`" in md_text


def test_partial_validation_evidence_reported_as_unknown(controller):
    """P1 regression: Partial validation evidence without explicit status or exit code 0

    must be reported as UNKNOWN, not PASS, even if the terminal status is PASS.
    """
    state = controller.store.load()

    # Partial validation evidence with commands but no status or exit_code
    partial_val = {
        "commands": ["pytest tests/test_smoke.py"],
        "stdout": "running tests...",
    }

    report_gen = FinalReportGenerator(controller)
    report_data, _ = report_gen.generate_and_save(
        state,
        "PASS",
        reason="Terminal status PASS with partial validation",
        evidence={"validation": partial_val},
    )

    val_sum = report_data["validation_summary"]
    assert val_sum["status"] == "UNKNOWN", "Partial validation without status or exit_code 0 must report UNKNOWN"
    assert val_sum["exit_code"] is None
    assert report_data["proof_passed"] is False

    md_text = controller.paths.final_report_md.read_text(encoding="utf-8")
    assert "- **Validation Status**: `UNKNOWN`" in md_text
    assert "- **Goal Proof Passed**: `Unavailable (Unverified)`" in md_text
    assert "- **Goal Proof Passed**: `True`" not in md_text


def test_pass_without_goal_proof_renders_unverified_markdown(controller):
    """P1 regression: A PASS goal without recorded goal proof must render

    'Goal Proof Passed: Unavailable (Unverified)', not 'Goal Proof Passed: True'.
    """
    state = controller.store.load()

    report_gen = FinalReportGenerator(controller)
    report_data, _ = report_gen.generate_and_save(
        state,
        "PASS",
        reason="Claimed success without empirical proof artifact",
    )

    assert report_data["proof_passed"] is False
    md_text = controller.paths.final_report_md.read_text(encoding="utf-8")
    assert "- **Goal Proof Passed**: `Unavailable (Unverified)`" in md_text
    assert "- **Goal Proof Passed**: `True`" not in md_text





"""Phase 5 Deterministic Final Reporting test suite."""
from __future__ import annotations

import pathlib
import tempfile
from typing import Any
import pytest

from labeeb.config import Config
from labeeb.core.artifacts import (
    AUTHORITY_CONTEXT,
    BASELINE_RESULT,
    FINAL_REPORT,
    GOAL_CONTRACT,
    GOAL_PROOF,
    PROOF_CONTRACT,
    REALITY_AUDIT,
    SOLUTION_CANDIDATES,
)
from labeeb.core.controller import LabeebController
from labeeb.core.reporting import FinalReportGenerator
from labeeb.models import (
    ArtifactStatus,
    ArtifactValidity,
    PathIntegrityStatus,
    ReasoningActivity,
    ReasoningDecisionAction,
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

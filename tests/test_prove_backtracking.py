"""Phase 4 Execute & Prove Backtracking test suite."""
from __future__ import annotations

import pathlib
import tempfile
from typing import Any
import pytest

from labeeb.config import Config
from labeeb.core.artifacts import (
    AUTHORITY_CONTEXT,
    BASELINE_RESULT,
    CHANGE_AUTHORITY,
    CONVERGENCE,
    CRITIC_REVIEW,
    DELIVERY_REVIEW,
    DIAGNOSIS,
    EXECUTION_CONTRACT,
    GOAL_CONTRACT,
    GOAL_PROOF,
    IMPLEMENTATION_READINESS,
    IMPLEMENTATION_RESULT,
    MUTATION_PREFLIGHT,
    PRODUCT_CONTRACT,
    PROOF_CONTRACT,
    REALITY_AUDIT,
    SECOND_AUDIT,
    SOLUTION_CANDIDATES,
    VALIDATION_RESULT,
)
from labeeb.core.controller import LabeebController
from labeeb.core.decisions import handle_reasoning_decision, handle_review_decision
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
            intent="Fix a bounded local defect",
            workspace=str(root),
            repo="owner/repo",
            branch="main",
            risk_tags=[],
            allowed_paths=["src", "tests"],
            validation_commands=["true"],
            preauthorize_plan=True,
        )


def _setup_approved_plan(ctl: LabeebController, state: dict[str, Any]) -> None:
    contract = read_ref_json(state["contract_ref"])
    contract["allowed_paths"] = ["src", "tests"]
    contract["validation_commands"] = ["true"]
    state["contract_ref"] = ctl.store.write_json(ctl.paths.contract, contract)
    plan_payload = {
        "plan_summary": "Test execution plan",
        "execution": {
            "jules_prompt": "Apply targeted change to src/main.py",
            "allowed_paths": ["src", "tests"],
            "validation_commands": ["true"],
        },
    }
    state["plan_ref"] = ctl.store.write_json(ctl.paths.plan, plan_payload)


def _write_full_reasoning_chain(ctl: LabeebController, state: dict[str, Any]) -> None:
    write = ctl.artifact_store.write_artifact
    write(state, AUTHORITY_CONTEXT, {}, "controller")
    write(state, GOAL_CONTRACT, {}, "brain")
    write(state, PRODUCT_CONTRACT, {}, "brain")
    write(state, PROOF_CONTRACT, {"entrypoint": "pytest"}, "brain")
    write(state, REALITY_AUDIT, {}, "brain")
    write(state, MUTATION_PREFLIGHT, {}, "brain", activity_status=ArtifactStatus.NOT_APPLICABLE)
    write(state, BASELINE_RESULT, {"status": "FAIL"}, "brain")
    write(state, DIAGNOSIS, {}, "brain")
    write(state, SOLUTION_CANDIDATES, {"approach": "candidate A"}, "brain")
    write(state, SECOND_AUDIT, {}, "brain")
    write(state, DELIVERY_REVIEW, {}, "brain")
    write(state, CRITIC_REVIEW, {}, "critic")
    write(state, CONVERGENCE, {}, "brain")
    write(state, CHANGE_AUTHORITY, {"classification": "LOCAL"}, "brain")
    write(state, IMPLEMENTATION_READINESS, {"ready": True}, "brain")
    write(state, EXECUTION_CONTRACT, {"prompt": "bounded prompt"}, "brain")
    state["reasoning_graph_active"] = True


def test_execution_rounds_tracking_and_cap(controller):
    state = controller.store.load()
    _setup_approved_plan(controller, state)

    assert state.get("execution_rounds", 0) == 0

    # First dispatch -> round 1
    controller.dispatch_jules(state)
    assert state["execution_rounds"] == 1
    assert state["macro_phase"] == "EXECUTE"
    assert state["pending_action"]["kind"] == "jules_create"

    # Second dispatch -> round 2 (max rounds reached)
    state.pop("pending_action", None)
    controller.dispatch_jules(state)
    assert state["execution_rounds"] == 2
    assert state["macro_phase"] == "EXECUTE"
    assert state["pending_action"]["kind"] == "jules_create"

    # Third dispatch -> exceeds max_execution_rounds (2) -> FAIL
    state.pop("pending_action", None)
    controller.dispatch_jules(state)
    assert state["phase"] == "FAIL"
    assert "Execution rounds exhausted" in read_ref_json(state["result_ref"])["reason"]


def test_return_to_thinking_preserves_repair_budget(controller):
    state = controller.store.load()
    _setup_approved_plan(controller, state)
    _write_full_reasoning_chain(controller, state)

    state["macro_phase"] = "PROVE"
    state["phase"] = "REVIEW"
    state["execution_rounds"] = 1
    state["repair_reserved"] = False

    handle_review_decision(
        controller,
        state,
        {
            "action": "RETURN_TO_THINKING",
            "reason": "Validation revealed an invalid planning assumption regarding concurrency",
            "invalidate_roots": [SOLUTION_CANDIDATES],
        },
    )

    # 1. Macro phase transitions back to THINKING
    assert state["macro_phase"] == "THINKING"
    assert state["phase"] == "EFFECT"
    assert state["pending_action"]["kind"] in {"brain_launch", "brain_resume"}

    # 2. Activity normalized to ReasoningActivity
    assert state["current_activity"] == ReasoningActivity.SOLUTION_EXPLORATION

    # 3. CRITICAL: Repair budget was NOT consumed
    assert state.get("repair_reserved") is False

    # 4. Invalidation cascaded correctly
    # Solution candidates and its downstream dependencies must be STALE
    assert state["artifacts"][SOLUTION_CANDIDATES]["validity"] == ArtifactValidity.STALE
    assert state["artifacts"][SECOND_AUDIT]["validity"] == ArtifactValidity.STALE
    assert state["artifacts"][DELIVERY_REVIEW]["validity"] == ArtifactValidity.STALE
    assert state["artifacts"][CRITIC_REVIEW]["validity"] == ArtifactValidity.STALE
    assert state["artifacts"][CONVERGENCE]["validity"] == ArtifactValidity.STALE
    assert state["artifacts"][CHANGE_AUTHORITY]["validity"] == ArtifactValidity.STALE
    assert state["artifacts"][IMPLEMENTATION_READINESS]["validity"] == ArtifactValidity.STALE
    assert state["artifacts"][EXECUTION_CONTRACT]["validity"] == ArtifactValidity.STALE

    # Upstream dependencies must remain VALID
    assert state["artifacts"][AUTHORITY_CONTEXT]["validity"] == ArtifactValidity.VALID
    assert state["artifacts"][GOAL_CONTRACT]["validity"] == ArtifactValidity.VALID
    assert state["artifacts"][PRODUCT_CONTRACT]["validity"] == ArtifactValidity.VALID
    assert state["artifacts"][PROOF_CONTRACT]["validity"] == ArtifactValidity.VALID
    assert state["artifacts"][REALITY_AUDIT]["validity"] == ArtifactValidity.VALID
    assert state["artifacts"][DIAGNOSIS]["validity"] == ArtifactValidity.VALID

    # Event logged
    events = controller.store.read_events()
    assert any(e.get("event_type") == "reasoning.returned_to_thinking" for e in events)


def test_return_to_thinking_fails_when_execution_rounds_exhausted(controller):
    state = controller.store.load()
    _setup_approved_plan(controller, state)
    _write_full_reasoning_chain(controller, state)

    state["macro_phase"] = "PROVE"
    state["phase"] = "REVIEW"
    state["execution_rounds"] = 2  # Already at ceiling

    handle_review_decision(
        controller,
        state,
        {
            "action": "RETURN_TO_THINKING",
            "reason": "Assumption disproved again",
            "invalidate_roots": [SOLUTION_CANDIDATES],
        },
    )

    assert state["phase"] == "FAIL"
    assert "maximum execution rounds" in read_ref_json(state["result_ref"])["reason"].lower()


def test_targeted_repair_budget_exhaustion(controller):
    state = controller.store.load()
    _setup_approved_plan(controller, state)
    state["macro_phase"] = "PROVE"
    state["phase"] = "REVIEW"
    state["repair_reserved"] = False
    state["jules_session_id"] = "test-jules-session"
    controller.jules.get_logs = lambda sid: {"activities": [{"id": "act-1"}]}

    # Turn 1: First repair is permitted and reserves the single repair budget
    handle_review_decision(
        controller,
        state,
        {
            "action": "REPAIR",
            "repair_message": "Fix typo in variable name on line 42",
        },
    )

    assert state.get("repair_reserved") is True
    assert state["phase"] == "EFFECT"
    assert state["pending_action"]["kind"] == "jules_message"

    # Turn 2: Second repair attempt is rejected by single repair ceiling
    handle_review_decision(
        controller,
        state,
        {
            "action": "REPAIR",
            "repair_message": "Fix secondary typo",
        },
    )

    assert state["phase"] == "FAIL"
    assert "single V1 repair budget was consumed" in read_ref_json(state["result_ref"])["reason"]


def test_baseline_shortcut_records_goal_proof(controller):
    state = controller.store.load()
    write = controller.artifact_store.write_artifact
    write(state, AUTHORITY_CONTEXT, {}, "controller")
    write(state, GOAL_CONTRACT, {}, "brain")
    write(state, PROOF_CONTRACT, {"entrypoint": "pytest tests/test_smoke.py"}, "brain")
    write(state, REALITY_AUDIT, {}, "brain")
    write(state, MUTATION_PREFLIGHT, {}, "brain", activity_status=ArtifactStatus.NOT_APPLICABLE)

    handle_reasoning_decision(
        controller,
        state,
        {
            "decision": ReasoningDecisionAction.PASS,
            "current_activity": ReasoningActivity.BASELINE,
            "activity_status": ArtifactStatus.SATISFIED,
            "next_activity": None,
            "reason": "The original baseline test suite passes with zero mutations.",
            "produced_artifact": {"artifact_type": "baseline_result", "data": {"status": "PASS"}},
        },
    )

    assert state["phase"] == "PASS"
    assert state["baseline_shortcut"] is True
    assert GOAL_PROOF in state.get("artifacts", {})

    proof_entry = state["artifacts"][GOAL_PROOF]
    assert proof_entry["validity"] == ArtifactValidity.VALID
    proof_payload = read_ref_json(proof_entry["ref"])
    proof_data = proof_payload.get("data", proof_payload)
    assert proof_data["proof_passed"] is True
    assert proof_data["terminal_path"] == "BASELINE_SHORTCUT"
    assert proof_data["execution_rounds"] == 0
    assert proof_data["path_integrity_status"] == PathIntegrityStatus.ORIGINAL


def test_goal_proof_artifact_generation_in_validating(controller, monkeypatch):
    state = controller.store.load()
    _setup_approved_plan(controller, state)
    _write_full_reasoning_chain(controller, state)

    # Setup state as validating completed Jules implementation
    state["macro_phase"] = "PROVE"
    state["phase"] = "VALIDATING"
    state["execution_rounds"] = 1
    state["jules_session_id"] = "jules-session-123"

    # Create dummy patch file
    patch_path = controller.paths.evidence / "patch-test.diff"
    patch_path.write_text("--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n")
    state["patch_ref"] = f"file:{patch_path}"

    evidence = {
        "patch_ref": state["patch_ref"],
        "patch_hash": "sha256-test",
        "files": ["src/main.py"],
        "base_commit": "HEAD",
        "validation": {
            "status": "PASS",
            "exit_code": 0,
            "commands": ["true"],
            "stdout": "All tests passed",
            "stderr": "",
            "duration_seconds": 1.2,
        },
        "goal_proof": {
            "entrypoint": "pytest tests/test_smoke.py",
            "proof_passed": True,
            "entrypoint_exit_code": 0,
            "path_integrity_status": PathIntegrityStatus.ORIGINAL,
        },
    }
    state["review_ref"] = controller.store.write_json(controller.paths.reviews / "test-review.json", evidence)

    # Mock validate_evidence in controller to return pre-computed evidence
    monkeypatch.setattr("labeeb.core.controller.validate_evidence", lambda *args, **kwargs: evidence)

    # Execute step_validating
    res = controller.step_validating(state)
    assert res is True

    # Verify implementation_result, validation_result, and goal_proof artifacts exist
    assert IMPLEMENTATION_RESULT in state["artifacts"]
    assert VALIDATION_RESULT in state["artifacts"]
    assert GOAL_PROOF in state["artifacts"]

    proof_entry = state["artifacts"][GOAL_PROOF]
    assert proof_entry["validity"] == ArtifactValidity.VALID
    proof_payload = read_ref_json(proof_entry["ref"])
    proof_data = proof_payload.get("data", proof_payload)
    assert proof_data["proof_passed"] is True
    assert proof_data["validation_status"] == "PASS"
    assert proof_data["validation_exit_code"] == 0
    assert proof_data["execution_rounds"] == 1
    assert proof_data["repair_reserved"] is False


def test_goal_proof_failure_blocks_pass_even_when_validation_passes(controller):
    """P0 regression: General validation passes, but goal proof fails -> PASS must be BLOCKED."""
    state = controller.store.load()
    _setup_approved_plan(controller, state)
    _write_full_reasoning_chain(controller, state)

    state["macro_phase"] = "PROVE"
    state["phase"] = "REVIEW"
    state["execution_rounds"] = 1

    evidence = {
        "validation": {
            "status": "PASS",
            "exit_code": 0,
            "commands": ["true"],
        },
        "goal_proof": {
            "proof_passed": False,
            "entrypoint": "python -m unittest tests/test_smoke.py",
            "entrypoint_exit_code": 1,
            "reason": "Proof entrypoint exited with code 1",
            "path_integrity_status": PathIntegrityStatus.ORIGINAL,
        },
    }
    state["review_ref"] = controller.store.write_json(controller.paths.reviews / "test-review-gp-fail.json", evidence)

    # Brain attempts to PASS
    handle_review_decision(
        controller,
        state,
        {
            "action": "PASS",
            "reason": "All good from brain perspective",
        },
    )

    # Must be BLOCKED, cannot PASS
    assert state["phase"] == "BLOCKED"
    last_event = controller.store.read_events()[-1]
    event_reason = str(last_event.get("data", {}).get("reason", ""))
    assert "goal proof failed" in event_reason.lower() or "goal proof failed" in state.get("blocked_action", {}).get("reason", "").lower()


def test_missing_or_stale_proof_contract_blocks_pass(controller):
    """P0 regression: Missing or stale proof contract must not produce a passing proof artifact."""
    state = controller.store.load()
    _setup_approved_plan(controller, state)
    _write_full_reasoning_chain(controller, state)

    state["macro_phase"] = "PROVE"
    state["phase"] = "REVIEW"
    state["execution_rounds"] = 1

    # Proof contract marked STALE
    state["artifacts"][PROOF_CONTRACT]["validity"] = ArtifactValidity.STALE

    evidence = {
        "validation": {"status": "PASS", "exit_code": 0, "commands": ["true"]},
        "goal_proof": {
            "proof_passed": False,
            "reason": "Proof contract artifact is STALE or invalid",
            "path_integrity_status": PathIntegrityStatus.ORIGINAL,
        },
    }
    state["review_ref"] = controller.store.write_json(controller.paths.reviews / "test-review-stale.json", evidence)

    handle_review_decision(controller, state, {"action": "PASS"})
    assert state["phase"] == "BLOCKED"


def test_execution_rounds_includes_repair_and_enforces_ceiling(controller):
    """P1 regression: Count actual execution rounds including repair and enforce max-2 ceiling."""
    state = controller.store.load()
    _setup_approved_plan(controller, state)

    assert state.get("execution_rounds", 0) == 0

    # 1. First execution round via initial Jules dispatch
    controller.dispatch_jules(state)
    assert state["execution_rounds"] == 1
    assert state.get("repair_reserved", False) is False

    # Simulate completed Jules work and state in REVIEW
    state["macro_phase"] = "PROVE"
    state["phase"] = "REVIEW"
    state["jules_session_id"] = "session-123"
    state.pop("pending_action", None)

    # 2. Targeted repair sent to existing session -> execution_rounds must increment to 2
    # Mock jules.get_logs so reserve_and_send_repair succeeds
    controller.jules.get_logs = lambda sid: {"activities": [{"id": "act-1"}]}

    handle_review_decision(
        controller,
        state,
        {
            "action": "REPAIR",
            "repair_message": "Fix edge-case off-by-one bug",
        },
    )

    assert state["execution_rounds"] == 2
    assert state["repair_reserved"] is True
    assert state["pending_action"]["kind"] == "jules_message"

    # 3. Attempting extra execution:
    # A) Attempting another repair when ceiling is reached (and repair already reserved) -> fails
    state["macro_phase"] = "PROVE"
    state["phase"] = "REVIEW"
    state.pop("pending_action", None)

    handle_review_decision(
        controller,
        state,
        {
            "action": "REPAIR",
            "repair_message": "Fix another bug",
        },
    )
    assert state["phase"] == "FAIL"

    # B) Attempting another dispatch when execution_rounds == 2 -> fails
    state2 = controller.store.load()
    _setup_approved_plan(controller, state2)
    state2["execution_rounds"] = 2
    controller.dispatch_jules(state2)
    assert state2["phase"] == "FAIL"
    assert "Execution rounds exhausted" in read_ref_json(state2["result_ref"])["reason"]


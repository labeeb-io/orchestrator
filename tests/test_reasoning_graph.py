"""Unit tests for reasoning activity graph, decision envelope, preflight, and anti-loop engine."""
from __future__ import annotations

import pathlib
import tempfile
import pytest

from labeeb.config import Config
from labeeb.core.artifacts import (
    AUTHORITY_CONTEXT,
    GOAL_CONTRACT,
    PROOF_CONTRACT,
    REALITY_AUDIT,
    SOLUTION_CANDIDATES,
)
from labeeb.core.controller import LabeebController
from labeeb.core.convergence import ReasoningProgressTracker
from labeeb.core.decisions import handle_reasoning_decision
from labeeb.core.state_machine import (
    compile_authority_context,
    evaluate_mutation_preflight,
    parse_reasoning_decision,
    update_proof_path_lock,
    validate_activity_transition,
)
from labeeb.errors import ControllerError
from labeeb.models import (
    DECISION_END,
    DECISION_START,
    ArtifactStatus,
    ArtifactValidity,
    PathIntegrityStatus,
    ReasoningActivity,
)


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield pathlib.Path(tmp)


def test_activity_transition_validation():
    # Valid forward transitions
    assert validate_activity_transition(ReasoningActivity.AUTHORITY_CONTEXT, ReasoningActivity.GOAL_CONTRACT)
    assert validate_activity_transition(ReasoningActivity.GOAL_CONTRACT, ReasoningActivity.PRODUCT_VALIDATION)
    assert validate_activity_transition(ReasoningActivity.GOAL_CONTRACT, ReasoningActivity.PROOF_CONTRACT)  # headless skip
    assert validate_activity_transition(ReasoningActivity.PRODUCT_VALIDATION, ReasoningActivity.PROOF_CONTRACT)
    assert validate_activity_transition(ReasoningActivity.REALITY_AUDIT, ReasoningActivity.BASELINE)
    assert validate_activity_transition(ReasoningActivity.MUTATION_PREFLIGHT, ReasoningActivity.DIAGNOSIS)  # baseline skip
    assert validate_activity_transition(ReasoningActivity.SOLUTION_EXPLORATION, ReasoningActivity.SECOND_REALITY_AUDIT)
    assert validate_activity_transition(ReasoningActivity.SECOND_REALITY_AUDIT, ReasoningActivity.INDEPENDENT_CRITIQUE)
    assert validate_activity_transition(ReasoningActivity.SECOND_REALITY_AUDIT, ReasoningActivity.CHANGE_AUTHORITY)

    # Valid self-transitions (refinement / multi-turn)
    assert validate_activity_transition(ReasoningActivity.SOLUTION_EXPLORATION, ReasoningActivity.SOLUTION_EXPLORATION)
    assert validate_activity_transition(ReasoningActivity.REALITY_AUDIT, ReasoningActivity.REALITY_AUDIT)

    # Valid transitions using AGENTS.md aliases
    assert validate_activity_transition("second_audit", "critic_review")
    assert validate_activity_transition("solution_candidates", "second_audit")
    assert validate_activity_transition("baseline_result", "diagnosis")

    # Valid backward transitions
    assert validate_activity_transition(ReasoningActivity.SECOND_REALITY_AUDIT, ReasoningActivity.REALITY_AUDIT)
    assert validate_activity_transition(ReasoningActivity.DELIVERY_READINESS, ReasoningActivity.PRODUCT_VALIDATION)
    assert validate_activity_transition(ReasoningActivity.CONVERGENCE, ReasoningActivity.SOLUTION_EXPLORATION)

    # None target (terminal within reasoning)
    assert validate_activity_transition(ReasoningActivity.EXECUTION_CONTRACT, None)

    # Invalid jump forward
    with pytest.raises(ControllerError, match="Invalid activity transition"):
        validate_activity_transition(ReasoningActivity.AUTHORITY_CONTEXT, ReasoningActivity.EXECUTION_CONTRACT)

    # Invalid jump backward
    with pytest.raises(ControllerError, match="Invalid activity transition"):
        validate_activity_transition(ReasoningActivity.GOAL_CONTRACT, ReasoningActivity.CONVERGENCE)


def test_authority_context_compilation_and_no_secrets(temp_dir):
    config = Config(
        temp_dir / "config.toml",
        {
            "workflow": {
                "brain_role": "codex-brain",
                "critic_role": "claude-critic",
                "implementer_role": "jules-impl",
            }
        },
    )
    contract = {
        "workspace": "/home/user/project",
        "repo": "owner/repo",
        "branch": "feat",
        "allowed_paths": ["app/Models", "tests"],
        "risk_tags": ["architecture"],
        "authority": {"preauthorize_bounded_plan": True, "allow_workspace_edits": True},
        "api_key": "SUPER_SECRET_TOKEN_DO_NOT_COPY",
    }

    ctx = compile_authority_context(contract, config)
    assert ctx["repository_policy"]["source"] == "AGENTS.md"
    assert ctx["environment"]["workspace"] == "/home/user/project"
    assert ctx["allowed_mutation_scope"]["allowed_paths"] == ["app/Models", "tests"]
    assert ctx["remote_write_boundary"]["allow_push"] is False
    assert ctx["user_authority"]["preauthorize_bounded_plan"] is True
    assert ctx["role_policy"]["brain_role"] == "codex-brain"

    # Crucial: verify secrets from seed contract are NOT copied
    raw_str = str(ctx)
    assert "SUPER_SECRET_TOKEN_DO_NOT_COPY" not in raw_str


def test_mutation_preflight_evaluation():
    proof_contract = {"entrypoint": "api/test"}

    # Read-only case
    ok, status, data = evaluate_mutation_preflight(proof_contract, {"is_read_only": True})
    assert ok is True
    assert status == ArtifactStatus.NOT_APPLICABLE
    assert data["safe"] is True

    # Safe bounded mutation
    preflight_safe = {
        "is_read_only": False,
        "mutation_target": "tests/temp_cohort",
        "limit_semantics": "count <= 1",
        "async_continuation": False,
        "unbounded_fan_out": False,
        "stable_identifier": "test-run-123",
        "cleanup_defined": True,
    }
    ok, status, data = evaluate_mutation_preflight(proof_contract, preflight_safe)
    assert ok is True
    assert status == ArtifactStatus.SATISFIED
    assert data["safe"] is True

    # Unsafe unbounded fan-out
    preflight_unsafe = {
        "is_read_only": False,
        "mutation_target": "production_db",
        "limit_semantics": "",
        "unbounded_fan_out": True,
        "stable_identifier": "",
    }
    ok, status, data = evaluate_mutation_preflight(proof_contract, preflight_unsafe)
    assert ok is False
    assert status == ArtifactStatus.BLOCKED
    assert data["safe"] is False
    assert len(data["issues"]) > 0


def test_proof_path_locking_and_path_integrity():
    state = {}

    # Initial state
    ok, status = update_proof_path_lock(state, {"entrypoint": "api/search"})
    assert ok is True
    assert status == PathIntegrityStatus.ORIGINAL
    assert state.get("proof_path_locked") is not True

    # Recompile before baseline allowed
    ok, status = update_proof_path_lock(state, {"entrypoint": "api/search2", "recompiled_from_original": True})
    assert ok is True
    assert status == PathIntegrityStatus.RECOMPILED_BEFORE_BASELINE
    assert state["path_integrity_status"] == PathIntegrityStatus.RECOMPILED_BEFORE_BASELINE

    # Baseline action starts -> locks path
    ok, status = update_proof_path_lock(state, {}, is_state_changing=True)
    assert ok is True
    assert state["proof_path_locked"] is True

    # Diagnostic-only alternate path after locking is allowed
    ok, status = update_proof_path_lock(state, {"entrypoint": "api/diagnostic"}, is_diagnostic_only=True)
    assert ok is True
    assert status == PathIntegrityStatus.ALTERNATE_DIAGNOSTIC_ONLY

    # Attempting to replace locked original proof path without diagnostic flag raises error
    with pytest.raises(ControllerError, match="Cannot replace locked original proof path"):
        update_proof_path_lock(state, {"entrypoint": "api/new"}, is_diagnostic_only=False)


def test_anti_loop_cycle_detection_blocks():
    state = {}

    # Step 1: Initial visit with artifact data
    ok, msg = ReasoningProgressTracker.record_step(
        state=state,
        current_activity="reality_audit",
        produced_artifact_data={"version": 1},
        evidence_refs=["file:a.py#L1"],
        max_repeats=2,
        max_steps=10,
    )
    assert ok is True

    # Step 2: Second visit with SAME data and no new evidence
    ok, msg = ReasoningProgressTracker.record_step(
        state=state,
        current_activity="reality_audit",
        produced_artifact_data={"version": 1},
        evidence_refs=["file:a.py#L1"],
        max_repeats=2,
        max_steps=10,
    )
    assert ok is True

    # Step 3: Third visit exceeding max_repeats (2) without progress
    ok, msg = ReasoningProgressTracker.record_step(
        state=state,
        current_activity="reality_audit",
        produced_artifact_data={"version": 1},
        evidence_refs=["file:a.py#L1"],
        max_repeats=2,
        max_steps=10,
    )
    assert ok is False
    assert "convergence stalled" in msg

    # Ping-Pong Cycle detection: A -> B -> A -> B without progress
    cycle_state = {}
    ReasoningProgressTracker.record_step(cycle_state, "reality_audit", {"d": 1}, ["ref1"], max_repeats=5)
    ReasoningProgressTracker.record_step(cycle_state, "solution_exploration", {"d": 2}, ["ref2"], max_repeats=5)
    ReasoningProgressTracker.record_step(cycle_state, "reality_audit", {"d": 1}, ["ref1"], max_repeats=5)
    ok, msg = ReasoningProgressTracker.record_step(
        cycle_state, "solution_exploration", {"d": 2}, ["ref2"], max_repeats=5
    )
    assert ok is False
    assert "Reasoning cycle detected" in msg


def test_parse_reasoning_decision_envelope():
    raw_output = f"""
    Here is my thinking about the task...
    {DECISION_START}
    {{
      "decision": "CONTINUE_REASONING",
      "current_activity": "second_reality_audit",
      "activity_status": "SATISFIED",
      "next_activity": "delivery_readiness",
      "reason": "Disproved naive assumption.",
      "produced_artifact": {{
        "artifact_type": "second_audit",
        "data": {{"survived": true}}
      }},
      "invalidate_roots": ["candidate_plan"],
      "evidence_refs": ["file:app/Foo.php#L20"],
      "human_checkpoint": {{
        "needed": false,
        "boundary_type": null,
        "question": null
      }}
    }}
    {DECISION_END}
    More notes outside.
    """
    decision = parse_reasoning_decision(raw_output)
    assert decision.decision == "CONTINUE_REASONING"
    assert decision.current_activity == "second_reality_audit"
    assert decision.next_activity == "delivery_readiness"
    assert decision.activity_status == "SATISFIED"
    assert decision.invalidate_roots == ["candidate_plan"]
    assert decision.evidence_refs == ["file:app/Foo.php#L20"]
    assert decision.human_checkpoint.needed is False


def test_handle_reasoning_decision_continue_and_invalidate(temp_dir):
    raw_cfg = {
        "controller": {"state_root": str(temp_dir)},
        "planning": {"max_reasoning_steps": 25, "max_activity_repeats": 3},
        "roles": {
            "brain": {"transport": "orchestrator", "runtime": "codex"},
            "critic": {"transport": "direct", "command": ["false"]},
            "implementer": {"transport": "jules", "command": "cjules"},
        },
    }
    config = Config(temp_dir / "config.toml", raw_cfg)
    ctl = LabeebController.create_goal(
        config,
        intent="Test reasoning decision",
        workspace=str(temp_dir),
        repo="owner/repo",
        branch="main",
        risk_tags=[],
        allowed_paths=[],
        validation_commands=[],
        preauthorize_plan=True,
    )

    state = ctl.store.load()

    # Populate upstream artifacts
    ctl.artifact_store.write_artifact(state, AUTHORITY_CONTEXT, {}, "controller")
    ctl.artifact_store.write_artifact(state, GOAL_CONTRACT, {}, "brain")
    ctl.artifact_store.write_artifact(state, PROOF_CONTRACT, {}, "brain")
    ctl.artifact_store.write_artifact(state, REALITY_AUDIT, {"initial": True}, "brain")
    ctl.artifact_store.write_artifact(state, SOLUTION_CANDIDATES, {"choice": "A"}, "brain")

    # Brain decision: completed SECOND_REALITY_AUDIT, disproved solution_candidates, advances to DELIVERY_READINESS
    decision = {
        "decision": "CONTINUE_REASONING",
        "current_activity": "second_reality_audit",
        "activity_status": "SATISFIED",
        "next_activity": "delivery_readiness",
        "reason": "Completed second audit.",
        "produced_artifact": {
            "artifact_type": "second_audit",
            "data": {"survived": True},
        },
        "invalidate_roots": ["solution_candidates"],
        "evidence_refs": ["file:app/Bar.php#L10"],
    }

    handle_reasoning_decision(ctl, state, decision)

    # Verify second_audit artifact written
    assert "second_audit" in state["artifacts"]
    assert state["artifacts"]["second_audit"]["validity"] == ArtifactValidity.VALID

    # Verify solution_candidates was invalidated to STALE
    assert state["artifacts"]["solution_candidates"]["validity"] == ArtifactValidity.STALE

    # Verify state advanced to next activity
    assert state["current_activity"] == "delivery_readiness"

    # Verify effect prepared to resume brain for next activity
    pending = state.get("pending_action")
    assert pending is not None
    assert pending["kind"] in {"brain_resume", "brain_launch"}


def test_human_checkpoint_decision(temp_dir):
    raw_cfg = {
        "controller": {"state_root": str(temp_dir)},
        "roles": {
            "brain": {"transport": "orchestrator", "runtime": "codex"},
            "critic": {"transport": "direct", "command": ["false"]},
            "implementer": {"transport": "jules", "command": "cjules"},
        },
    }
    config = Config(temp_dir / "config.toml", raw_cfg)
    ctl = LabeebController.create_goal(
        config,
        intent="Test human checkpoint",
        workspace=str(temp_dir),
        repo="owner/repo",
        branch="main",
        risk_tags=[],
        allowed_paths=[],
        validation_commands=[],
        preauthorize_plan=True,
    )
    state = ctl.store.load()

    decision = {
        "decision": "NEEDS_HUMAN",
        "current_activity": "goal_contract",
        "reason": "Material business ambiguity regarding billing policy.",
        "human_checkpoint": {
            "needed": True,
            "boundary_type": "material_product_decision",
            "question": "Should monthly tier include unlimited search?",
        },
    }

    handle_reasoning_decision(ctl, state, decision)

    # Goal pauses at PLAN_GATE
    assert state["phase"] == "PLAN_GATE"
    assert state.get("human_checkpoint", {}).get("question") == "Should monthly tier include unlimited search?"


def test_jules_prompt_synthesis_from_execution_contract_data(temp_dir):
    raw_cfg = {
        "controller": {"state_root": str(temp_dir)},
        "roles": {
            "brain": {"transport": "orchestrator", "runtime": "codex"},
            "critic": {"transport": "direct", "command": ["false"]},
            "implementer": {"transport": "jules", "command": "cjules"},
        },
    }
    config = Config(temp_dir / "config.toml", raw_cfg)
    ctl = LabeebController.create_goal(
        config,
        intent="Test prompt synthesis",
        workspace=str(temp_dir),
        repo="owner/repo",
        branch="main",
        risk_tags=[],
        allowed_paths=["tests/test_smoke_env.py"],
        validation_commands=["pytest tests/test_smoke_env.py"],
        preauthorize_plan=True,
    )
    state = ctl.store.load()

    # Emulate decision envelope as returned in session 1050ccca:
    # no top-level "execution.jules_prompt", but full details inside produced_artifact.data
    decision = {
        "decision": "IMPLEMENTATION_READY",
        "current_activity": "execution_contract",
        "activity_status": "SATISFIED",
        "next_activity": None,
        "reason": "Execution is fully bounded and preauthorized.",
        "produced_artifact": {
            "artifact_type": "execution_contract",
            "data": {
                "goal": "Create smoke test for environment sanity.",
                "approved_direction": "Create tests/test_smoke_env.py using unittest.",
                "implementation": [
                    "Import unittest",
                    "Assert sys.version_info >= (3, 10)",
                ],
                "allowed_paths": ["tests/test_smoke_env.py"],
                "validation_commands": ["pytest tests/test_smoke_env.py"],
                "acceptance_criteria": [
                    "The authorized file exists.",
                    "Validation command exits 0.",
                ],
            },
        },
    }

    handle_reasoning_decision(ctl, state, decision)

    # Verify plan was written and jules_prompt was synthesized
    from labeeb.storage.goal_store import read_ref_json
    plan = read_ref_json(state["plan_ref"])
    assert plan is not None
    assert "execution" in plan
    prompt = plan["execution"].get("jules_prompt")
    assert prompt is not None
    assert "Goal:\nCreate smoke test" in prompt
    assert "Implementation Steps:" in prompt
    assert "Assert sys.version_info" in prompt
    assert "Acceptance Criteria:" in prompt
    assert state.get("phase") != "BLOCKED"


def test_locked_proof_contract_ref_pinning_and_validation(temp_dir):
    from labeeb.core.validation import validate_evidence

    state = {
        "artifacts": {
            "proof_contract": {
                "ref": f"file:{temp_dir}/proof.json#sha256=1111",
                "sha256": "1111",
                "validity": ArtifactValidity.VALID,
                "data": {"entrypoint": "echo original"},
            }
        }
    }

    # Lock proof path with original proof contract
    ok, status = update_proof_path_lock(state, {}, is_state_changing=True)
    assert ok is True
    assert state["proof_path_locked"] is True
    assert state["locked_proof_contract_ref"] == f"file:{temp_dir}/proof.json#sha256=1111"
    assert state["locked_proof_contract_sha256"] == "1111"

    # Divergent proof contract replacement attempt
    state["artifacts"]["proof_contract"] = {
        "ref": f"file:{temp_dir}/proof_v2.json#sha256=2222",
        "sha256": "2222",
        "validity": ArtifactValidity.VALID,
        "data": {"entrypoint": "echo replaced"},
    }

    # Calling update_proof_path_lock without diagnostic flag raises ControllerError
    with pytest.raises(ControllerError, match="Cannot replace locked original proof path"):
        update_proof_path_lock(state, state["artifacts"]["proof_contract"]["data"], is_diagnostic_only=False)

    raw_cfg = {
        "controller": {"state_root": str(temp_dir / "state")},
        "executables": {"orchestrator": "true", "cjules": "true"},
        "roles": {
            "brain": {"transport": "orchestrator", "runtime": "mock"},
            "implementer": {"transport": "jules", "command": "cjules"},
        },
    }
    config = Config(temp_dir / "config.toml", raw_cfg)
    ctl = LabeebController.create_goal(
        config,
        intent="Test locked proof contract",
        workspace=str(temp_dir),
        repo="owner/repo",
        branch="main",
        risk_tags=[],
        allowed_paths=[],
        validation_commands=[],
        preauthorize_plan=True,
    )
    patch_path = temp_dir / "patch.diff"
    patch_path.write_text("--- a/f\n+++ b/f\n")
    patch_ref = f"file:{patch_path}#sha256=3333"

    ctl.git.create_worktree = lambda ws, wt, commit: wt.mkdir(parents=True, exist_ok=True)
    ctl.git.check_patch = lambda *args: None
    ctl.git.apply_patch = lambda *args: None

    state["contract_ref"] = ctl.store.write_json(
        ctl.paths.contract,
        {
            "workspace": str(temp_dir),
            "validation_commands": ["true"],
        },
    )

    ev_in = {
        "patch_ref": patch_ref,
        "base_commit": "abcdef123456",
    }

    # In validate_evidence, divergence from locked_proof_contract_ref is detected
    evidence = validate_evidence(state, ev_in, ctl.config, ctl.paths, ctl.git)
    assert evidence["goal_proof"]["proof_passed"] is False
    assert "diverges from locked original proof ref" in evidence["goal_proof"]["reason"]



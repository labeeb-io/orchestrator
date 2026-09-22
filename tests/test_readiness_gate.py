"""Phase 3 readiness and autonomous-dispatch tests."""
from __future__ import annotations

import pathlib
import tempfile

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
    GOAL_CONTRACT,
    MUTATION_PREFLIGHT,
    PRODUCT_CONTRACT,
    PROOF_CONTRACT,
    REALITY_AUDIT,
    SECOND_AUDIT,
)
from labeeb.core.decisions import handle_reasoning_decision
from labeeb.core.artifacts import SOLUTION_CANDIDATES
from labeeb.core.controller import LabeebController
from labeeb.core.state_machine import initial_brain_prompt
from labeeb.models import ArtifactStatus, ArtifactValidity, ReasoningActivity, ReasoningDecisionAction
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


def _write_readiness_chain(
    ctl: LabeebController, state: dict, *, classification: str = "LOCAL", proof_path_locked: bool = True
) -> None:
    write = ctl.artifact_store.write_artifact
    write(state, AUTHORITY_CONTEXT, {}, "controller")
    write(state, GOAL_CONTRACT, {}, "brain")
    write(state, PRODUCT_CONTRACT, {}, "brain")
    write(state, PROOF_CONTRACT, {"entrypoint": "pytest"}, "brain")
    write(state, REALITY_AUDIT, {}, "brain")
    write(state, MUTATION_PREFLIGHT, {}, "brain", activity_status=ArtifactStatus.NOT_APPLICABLE)
    write(state, BASELINE_RESULT, {"status": "FAIL"}, "brain")
    write(state, DIAGNOSIS, {}, "brain")
    write(state, SOLUTION_CANDIDATES, {}, "brain")
    write(state, SECOND_AUDIT, {}, "brain")
    write(state, DELIVERY_REVIEW, {}, "brain")
    write(state, CRITIC_REVIEW, {}, "critic")
    write(state, CONVERGENCE, {}, "brain")
    write(state, CHANGE_AUTHORITY, {"classification": classification}, "brain")
    state["proof_path_locked"] = proof_path_locked


def _implementation_ready(execution: dict) -> dict:
    return {
        "decision": ReasoningDecisionAction.IMPLEMENTATION_READY,
        "current_activity": ReasoningActivity.IMPLEMENTATION_READINESS,
        "activity_status": ArtifactStatus.SATISFIED,
        "next_activity": None,
        "reason": "Ready for bounded execution",
        "produced_artifact": {"artifact_type": "implementation_readiness", "data": {}},
        "execution": execution,
    }


def test_initial_prompt_starts_reasoning_graph(controller):
    state = controller.store.load()
    prompt = initial_brain_prompt(read_ref_json(state["contract_ref"]), controller.config)

    assert "Active activity: authority_context" in prompt


def test_baseline_pass_skips_jules(controller):
    state = controller.store.load()
    write = controller.artifact_store.write_artifact
    write(state, AUTHORITY_CONTEXT, {}, "controller")
    write(state, GOAL_CONTRACT, {}, "brain")
    write(state, PROOF_CONTRACT, {"entrypoint": "pytest"}, "brain")
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
            "reason": "The original proof already passes.",
            "produced_artifact": {"artifact_type": "baseline_result", "data": {"status": "PASS"}},
        },
    )

    assert state["phase"] == "PASS"
    assert state["baseline_shortcut"] is True
    assert state.get("pending_action") is None
    assert state["artifacts"][BASELINE_RESULT]["validity"] == ArtifactValidity.VALID


def test_stale_artifact_blocks_readiness(controller):
    state = controller.store.load()
    _write_readiness_chain(controller, state)
    controller.artifact_store.invalidate_artifacts(state, [SOLUTION_CANDIDATES])

    handle_reasoning_decision(
        controller,
        state,
        _implementation_ready({"jules_prompt": "fix", "allowed_paths": ["src"], "validation_commands": ["true"]}),
    )

    assert state["phase"] == "BLOCKED"
    assert state.get("pending_action") is None


def test_scope_widening_blocks_readiness(controller):
    state = controller.store.load()
    _write_readiness_chain(controller, state)

    handle_reasoning_decision(
        controller,
        state,
        _implementation_ready({"jules_prompt": "fix", "allowed_paths": ["docs"], "validation_commands": ["true"]}),
    )

    assert state["phase"] == "BLOCKED"
    assert state.get("pending_action") is None


def test_gated_change_pauses_for_human_approval(controller):
    state = controller.store.load()
    _write_readiness_chain(controller, state, classification="GATED")

    handle_reasoning_decision(
        controller,
        state,
        _implementation_ready(
            {
                "jules_prompt": "fix",
                "allowed_paths": ["src"],
                "validation_commands": ["true"],
                "requires_human_approval": True,
            }
        ),
    )

    assert state["phase"] == "PLAN_GATE"
    assert state["change_authority"] == "GATED"
    assert state.get("pending_action") is None


def test_failed_readiness_does_not_lock_proof_path(controller):
    state = controller.store.load()
    _write_readiness_chain(controller, state, classification="PRODUCTION", proof_path_locked=False)

    handle_reasoning_decision(
        controller,
        state,
        _implementation_ready(
            {
                "jules_prompt": "fix",
                "allowed_paths": ["src"],
                "validation_commands": ["true"],
                "remote_write": True,
            }
        ),
    )

    assert state["phase"] == "PLAN_GATE"
    assert state["proof_path_locked"] is False


def test_preauthorized_local_change_dispatches_jules(controller):
    state = controller.store.load()
    _write_readiness_chain(controller, state)

    handle_reasoning_decision(
        controller,
        state,
        _implementation_ready({"jules_prompt": "fix", "allowed_paths": ["src"], "validation_commands": ["true"]}),
    )

    assert state["phase"] == "EFFECT"
    assert state["macro_phase"] == "EXECUTE"
    assert state["pending_action"]["kind"] == "jules_create"

"""Unit tests for immutable artifact store, versioning, and cascading invalidation."""
from __future__ import annotations

import json
import pathlib
import tempfile
import pytest

from labeeb.config import Config
from labeeb.core.artifacts import (
    AUTHORITY_CONTEXT,
    DELIVERY_REVIEW,
    EXECUTION_CONTRACT,
    GOAL_CONTRACT,
    PRODUCT_CONTRACT,
    PROOF_CONTRACT,
    REALITY_AUDIT,
    SECOND_AUDIT,
    SOLUTION_CANDIDATES,
    GoalArtifactStore,
)
from labeeb.core.controller import LabeebController
from labeeb.errors import ControllerError
from labeeb.models import (
    ArtifactStatus,
    ArtifactValidity,
)
from labeeb.storage.goal_store import (
    GoalPaths,
    GoalStore,
)


@pytest.fixture
def temp_goal_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield pathlib.Path(tmp)


@pytest.fixture
def artifact_store(temp_goal_dir):
    paths = GoalPaths(temp_goal_dir)
    store = GoalStore(paths)
    store.init_dirs()
    return GoalArtifactStore(paths, store), paths, store


def test_immutable_artifact_versioning(artifact_store):
    store_art, paths, store = artifact_store
    state = {"artifacts": {}}

    # Write version 1
    ref1, v1 = store_art.write_artifact(
        state=state,
        artifact_type=GOAL_CONTRACT,
        data={"objective": "Test Goal V1"},
        producer="brain:codex",
    )
    assert v1 == 1
    v1_file = paths.artifacts / "goal_contract.v1.json"
    assert v1_file.exists()
    v1_content_before = v1_file.read_text(encoding="utf-8")

    # Verify state index
    assert state["artifacts"][GOAL_CONTRACT]["version"] == 1
    assert state["artifacts"][GOAL_CONTRACT]["validity"] == ArtifactValidity.VALID
    assert state["artifacts"][GOAL_CONTRACT]["ref"] == ref1

    # Write version 2
    ref2, v2 = store_art.write_artifact(
        state=state,
        artifact_type=GOAL_CONTRACT,
        data={"objective": "Test Goal V2"},
        producer="brain:codex",
    )
    assert v2 == 2
    v2_file = paths.artifacts / "goal_contract.v2.json"
    assert v2_file.exists()
    assert ref1 != ref2

    # Immutability check: v1 file MUST be byte-for-byte unchanged
    assert v1_file.read_text(encoding="utf-8") == v1_content_before

    # Verify state index updated to version 2
    assert state["artifacts"][GOAL_CONTRACT]["version"] == 2
    assert state["artifacts"][GOAL_CONTRACT]["ref"] == ref2

    # Verify reading latest vs specific version
    latest = store_art.get_latest_artifact(state, GOAL_CONTRACT)
    assert latest["data"]["objective"] == "Test Goal V2"
    assert latest["version"] == 2

    older = store_art.get_artifact_version(GOAL_CONTRACT, 1)
    assert older["data"]["objective"] == "Test Goal V1"
    assert older["version"] == 1


def test_controller_cascading_invalidation(artifact_store):
    store_art, paths, store = artifact_store
    state = {"artifacts": {}}

    # Build a realistic dependency chain
    store_art.write_artifact(state, AUTHORITY_CONTEXT, {"policy": "AGENTS.md"}, "controller")
    store_art.write_artifact(state, GOAL_CONTRACT, {"goal": "Search"}, "brain")
    store_art.write_artifact(state, PROOF_CONTRACT, {"entrypoint": "api/search"}, "brain")
    store_art.write_artifact(state, REALITY_AUDIT, {"found": True}, "brain")
    store_art.write_artifact(state, SOLUTION_CANDIDATES, {"choice": "Option A"}, "brain")
    store_art.write_artifact(state, SECOND_AUDIT, {"survived": True}, "brain")
    store_art.write_artifact(state, DELIVERY_REVIEW, {"user_ok": True}, "brain")
    store_art.write_artifact(state, EXECUTION_CONTRACT, {"prompt": "do it"}, "brain")

    # Verify all are initially VALID
    for art in [
        AUTHORITY_CONTEXT,
        GOAL_CONTRACT,
        PROOF_CONTRACT,
        REALITY_AUDIT,
        SOLUTION_CANDIDATES,
        SECOND_AUDIT,
        DELIVERY_REVIEW,
        EXECUTION_CONTRACT,
    ]:
        assert state["artifacts"][art]["validity"] == ArtifactValidity.VALID

    # Now invalidate REALITY_AUDIT
    affected = store_art.invalidate_artifacts(state, [REALITY_AUDIT])

    # Downstream artifacts should be invalidated
    expected_stale = {
        REALITY_AUDIT,
        SOLUTION_CANDIDATES,
        SECOND_AUDIT,
        DELIVERY_REVIEW,
        EXECUTION_CONTRACT,
    }
    assert expected_stale.issubset(affected)

    for art in expected_stale:
        assert state["artifacts"][art]["validity"] == ArtifactValidity.STALE

    # Upstream artifacts MUST remain VALID
    assert state["artifacts"][AUTHORITY_CONTEXT]["validity"] == ArtifactValidity.VALID
    assert state["artifacts"][GOAL_CONTRACT]["validity"] == ArtifactValidity.VALID
    assert state["artifacts"][PROOF_CONTRACT]["validity"] == ArtifactValidity.VALID

    # Files on disk must NOT be deleted or mutated
    for art in expected_stale:
        v1_path = paths.artifacts / f"{art}.v1.json"
        assert v1_path.exists()
        raw = json.loads(v1_path.read_text(encoding="utf-8"))
        # File payload on disk retains its original creation validity
        assert raw["version"] == 1


def test_model_cannot_preserve_stale_dependency(artifact_store):
    store_art, paths, store = artifact_store
    state = {"artifacts": {}}

    store_art.write_artifact(state, AUTHORITY_CONTEXT, {}, "controller")
    store_art.write_artifact(state, GOAL_CONTRACT, {}, "brain")
    store_art.write_artifact(state, PROOF_CONTRACT, {}, "brain")
    store_art.write_artifact(state, REALITY_AUDIT, {}, "brain")

    # Invalidate reality audit
    store_art.invalidate_artifacts(state, [REALITY_AUDIT])
    assert state["artifacts"][REALITY_AUDIT]["validity"] == ArtifactValidity.STALE

    # Attempting to write a downstream artifact (solution_candidates) whose dependency is STALE
    # must be blocked by the Controller
    with pytest.raises(ControllerError, match="dependency 'reality_audit' is STALE"):
        store_art.write_artifact(state, SOLUTION_CANDIDATES, {"choice": "invalid"}, "brain")


def test_not_applicable_status_and_reason(artifact_store):
    store_art, paths, store = artifact_store
    state = {"artifacts": {}}

    ref, v = store_art.write_artifact(
        state=state,
        artifact_type=PRODUCT_CONTRACT,
        data={"applicable": False},
        producer="brain",
        activity_status=ArtifactStatus.NOT_APPLICABLE,
        not_applicable_reason="Internal parser bug; no user-visible product behavior.",
    )

    assert v == 1
    assert state["artifacts"][PRODUCT_CONTRACT]["activity_status"] == ArtifactStatus.NOT_APPLICABLE
    assert (
        state["artifacts"][PRODUCT_CONTRACT]["not_applicable_reason"]
        == "Internal parser bug; no user-visible product behavior."
    )

    persisted = store_art.get_latest_artifact(state, PRODUCT_CONTRACT)
    assert persisted["activity_status"] == ArtifactStatus.NOT_APPLICABLE
    assert (
        persisted["not_applicable_reason"]
        == "Internal parser bug; no user-visible product behavior."
    )


def test_list_versions(artifact_store):
    store_art, paths, store = artifact_store
    state = {"artifacts": {}}

    store_art.write_artifact(state, REALITY_AUDIT, {"turn": 1}, "brain")
    store_art.invalidate_artifacts(state, [REALITY_AUDIT])
    store_art.write_artifact(
        state, REALITY_AUDIT, {"turn": 2}, "brain", allow_stale_dependency=True
    )

    versions = store_art.list_artifact_versions(REALITY_AUDIT)
    assert len(versions) == 2
    assert versions[0]["version"] == 1
    assert versions[0]["data"]["turn"] == 1
    assert versions[1]["version"] == 2
    assert versions[1]["data"]["turn"] == 2


def test_legacy_goal_compatibility(temp_goal_dir):
    paths = GoalPaths(temp_goal_dir)
    store = GoalStore(paths)
    store.init_dirs()

    # Legacy V1 state without 'artifacts' or V2 fields
    legacy_state = {
        "version": 1,
        "goal_id": "test-legacy-123",
        "phase": "THINKING",
        "contract_ref": "file:/dummy#sha256=123",
        "plan_ref": None,
    }
    state_file = paths.state
    state_file.write_text(json.dumps(legacy_state), encoding="utf-8")

    # Load with migration enabled (default)
    loaded = store.load()
    assert loaded["artifacts"] == {}
    assert loaded["macro_phase"] == "THINKING"
    assert loaded["execution_rounds"] == 0
    assert loaded["proof_path_locked"] is False
    assert loaded["path_integrity_status"] == "ORIGINAL"
    assert loaded["reasoning_history"] == []

    # Verify that NO fabricated artifacts were created
    assert len(loaded["artifacts"]) == 0
    assert not (paths.artifacts / "reality_audit.v1.json").exists()


def test_labeeb_controller_artifact_store_integration(temp_goal_dir):
    raw_cfg = {
        "controller": {"state_root": str(temp_goal_dir)},
        "roles": {
            "brain": {"transport": "orchestrator", "runtime": "codex"},
            "critic": {"transport": "direct", "command": ["false"]},
            "implementer": {"transport": "jules", "command": "cjules"},
        },
    }
    config = Config(temp_goal_dir / "config.toml", raw_cfg)
    ctl = LabeebController.create_goal(
        config,
        intent="Test integration",
        workspace=str(temp_goal_dir),
        repo="owner/repo",
        branch="main",
        risk_tags=[],
        allowed_paths=[],
        validation_commands=[],
        preauthorize_plan=True,
    )

    # Artifacts dir is created
    assert ctl.paths.artifacts.exists()
    assert hasattr(ctl, "artifact_store")
    assert isinstance(ctl.artifact_store, GoalArtifactStore)

    state = ctl.store.load()
    assert "artifacts" in state
    assert state["artifacts"] == {}
    assert state["macro_phase"] == "THINKING"
    assert state["current_activity"] == "authority_context"

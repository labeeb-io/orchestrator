"""Unit and integration tests for Group 4: Repair & Jules Event Determinism (Tasks 15-20)."""
import datetime as dt
import pathlib
import pytest
from unittest.mock import MagicMock, patch

from labeeb.config import Config
from labeeb.core.effects import EffectManager
from labeeb.core.events import (
    activity_key,
    has_repair_causality,
    jules_snapshot,
    meaningful_event,
    ordered_activities,
)
from labeeb.core.repair import maybe_handle_repair_activation_timeout
from labeeb.errors import AmbiguousEffect, CommandError
from labeeb.providers.jules import JulesProvider
from labeeb.storage.goal_store import GoalPaths, GoalStore


def make_config(raw: dict | None = None) -> Config:
    base = {
        "roles": {
            "implementer": {"transport": "jules", "command": "cjules"},
            "brain": {"transport": "orchestrator", "runtime": "codex"},
        },
        "timeouts": {"launch_seconds": 60, "repair_activation_seconds": 10},
        "workflow": {"repair_fallback": "blocked"},
        "controller": {"reconcile_attempts": 3, "reconcile_delay_seconds": 0.01},
    }
    if raw:
        for k, v in raw.items():
            if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                base[k].update(v)
            else:
                base[k] = v
    return Config(pathlib.Path("/tmp/dummy_config.toml"), base)


class TestTask15CanonicalActivityKey:
    def test_activity_key_deterministic_fallback(self):
        # Activity with explicit id
        act1 = {"id": "act-123", "createTime": "2026-09-22T10:00:00Z", "planGenerated": {}}
        assert activity_key(act1) == "id:act-123"

        # Activity without id fallback to fp:<sha256>
        act2 = {
            "createTime": "2026-09-22T10:05:00Z",
            "agentMessaged": {"message": "Hello world this is a test of fallback key"},
        }
        key2 = activity_key(act2)
        assert key2.startswith("fp:")

    def test_repair_activation_timeout_uses_canonical_keys(self, tmp_path):
        from labeeb.models import utc_now
        paths = GoalPaths(tmp_path / "g1")
        store = GoalStore(paths)
        store.init_dirs()
        config = make_config({"timeouts": {"repair_activation_seconds": 10}})

        ctl = MagicMock()
        ctl.config = config
        ctl.paths = paths
        ctl.store = store
        def fake_block(s, reason, evidence=None):
            s["phase"] = "BLOCKED"
            s["error"] = reason
        ctl.block = fake_block

        marker = "[LABEEB-REPAIR:g1:op1]"
        anchor = {
            "activity_keys": ["id:act-1", "fp:someoldkey"],
            "patch_hashes": [],
            "repair_marker": marker,
            "reserved_at": utc_now(),  # Freshly reserved, within 10s window
        }
        anchor_ref = store.write_json(paths.evidence / "anchor.json", anchor)

        state = {
            "phase": "WAITING_JULES",
            "repair_reserved": True,
            "round_anchor_ref": anchor_ref,
            "jules_session_id": "sess-1",
        }
        session = {"id": "sess-1", "state": "IN_PROGRESS"}

        # Case 1: no new activities -> no timeout if within window
        logs = {
            "activities": [
                {"id": "act-1", "createTime": "2026-09-22T09:59:00Z"},
            ]
        }
        assert not maybe_handle_repair_activation_timeout(ctl, state, session, logs)

        # Case 2: no new activities and reserved_at in the past -> times out after 10s
        anchor["reserved_at"] = "2020-01-01T00:00:00Z"
        anchor_ref_expired = store.write_json(paths.evidence / "anchor_expired.json", anchor)
        state["round_anchor_ref"] = anchor_ref_expired
        assert maybe_handle_repair_activation_timeout(ctl, state, session, logs)
        assert state["phase"] == "BLOCKED"
        assert "Repair message did not produce provable new Jules work" in state["error"]

        # Reset state
        state["phase"] = "WAITING_JULES"
        del state["error"]

        # Case 3: new activity present (even without 'id') -> does NOT timeout even if expired
        logs = {
            "activities": [
                {"id": "act-1", "createTime": "2026-09-22T09:59:00Z"},
                {
                    "createTime": "2026-09-22T10:02:00Z",
                    "agentMessaged": {"agentMessage": f"Working on repair {marker}"},
                },
            ]
        }
        assert not maybe_handle_repair_activation_timeout(ctl, state, session, logs)
        assert state["phase"] == "WAITING_JULES"


class TestTask16DeterministicSnapshotAndOrdering:
    def test_jules_snapshot_set_permutation_stability(self):
        # Shuffled activities and patches should produce identical sorted lists in snapshot
        patch_art_1 = {"changeSet": {"gitPatch": {"unidiffPatch": "diff --git a/file1.txt"}}}
        patch_art_2 = {"changeSet": {"gitPatch": {"unidiffPatch": "diff --git b/file2.txt"}}}

        logs_1 = {
            "activities": [
                {"id": "act-c", "createTime": "2026-09-22T10:02:00Z", "artifacts": [patch_art_2]},
                {"id": "act-a", "createTime": "2026-09-22T10:00:00Z", "artifacts": [patch_art_1]},
                {"id": "act-b", "createTime": "2026-09-22T10:01:00Z"},
            ],
        }
        logs_2 = {
            "activities": [
                {"id": "act-a", "createTime": "2026-09-22T10:00:00Z", "artifacts": [patch_art_1]},
                {"id": "act-c", "createTime": "2026-09-22T10:02:00Z", "artifacts": [patch_art_2]},
                {"id": "act-b", "createTime": "2026-09-22T10:01:00Z"},
            ],
        }

        snap1 = jules_snapshot(logs_1)
        snap2 = jules_snapshot(logs_2)

        assert snap1["activity_keys"] == ["id:act-a", "id:act-b", "id:act-c"]
        assert snap1["activity_keys"] == snap2["activity_keys"]
        assert snap1["patch_hashes"] == snap2["patch_hashes"]

    def test_meaningful_event_orders_activities_before_inspecting_latest(self):
        # Out-of-order activities in logs
        session = {"state": "AWAITING_USER_FEEDBACK"}
        logs = {
            "activities": [
                {
                    "id": "act-latest",
                    "createTime": "2026-09-22T10:05:00Z",
                    "agentMessaged": {"agentMessage": "Final question?"},
                },
                {
                    "id": "act-earlier",
                    "createTime": "2026-09-22T10:01:00Z",
                    "agentMessaged": {"agentMessage": "First question?"},
                },
            ]
        }
        state = {}
        config = make_config({"jules_state_actions": {"AWAITING_USER_FEEDBACK": "wake"}})
        res = meaningful_event(state, session, logs, config)
        assert res is not None
        key, payload = res
        assert payload["type"] == "AWAITING_USER_FEEDBACK"
        # The latest activity chosen should be act-latest because of sorting
        assert payload["latest_activity"]["id"] == "act-latest"


class TestTask17StrengthenRepairCausality:
    def test_repair_causality_requires_user_message_before_agent_message(self):
        marker = "[LABEEB-REPAIR:r1:deadbeef]"
        anchor = {"activity_keys": [], "patch_hashes": []}

        # Case 1: userMessaged followed by agentMessaged -> valid causality
        acts_valid = [
            {
                "id": "act-1",
                "createTime": "2026-09-22T10:00:00Z",
                "userMessaged": {"userMessage": f"Fix this {marker}"},
            },
            {
                "id": "act-2",
                "createTime": "2026-09-22T10:01:00Z",
                "agentMessaged": {"agentMessage": f"I have fixed it. {marker}"},
            },
        ]
        assert has_repair_causality(acts_valid, marker, anchor)

        # Case 2: agentMessaged appears BEFORE userMessaged -> reject causality
        acts_reversed = [
            {
                "id": "act-2",
                "createTime": "2026-09-22T10:01:00Z",
                "agentMessaged": {"agentMessage": f"I have fixed it. {marker}"},
            },
            {
                "id": "act-1",
                "createTime": "2026-09-22T10:02:00Z",
                "userMessaged": {"userMessage": f"Fix this {marker}"},
            },
        ]
        assert not has_repair_causality(acts_reversed, marker, anchor)

    def test_repair_causality_new_patch_hash(self):
        marker = "[LABEEB-REPAIR:r1:deadbeef]"
        anchor = {"activity_keys": [], "patch_hashes": ["old_hash_1"]}

        # userMessaged present, but agentMessaged not yet posted; however, new patch was produced after userMessaged
        acts_with_new_patch = [
            {
                "id": "act-1",
                "createTime": "2026-09-22T10:00:00Z",
                "userMessaged": {"userMessage": f"Fix this {marker}"},
            },
            {
                "id": "p2",
                "createTime": "2026-09-22T10:02:00Z",
                "artifacts": [
                    {"changeSet": {"gitPatch": {"unidiffPatch": "new repair patch content"}}}
                ],
            },
        ]
        assert has_repair_causality(acts_with_new_patch, marker, anchor)


class TestTask18StrictJulesCreateReconciliation:
    def test_find_sessions_rejects_empty_metadata_and_requires_exact_match(self):
        import json
        from labeeb.models import CmdResult
        jules = JulesProvider(make_config())
        marker = "[LABEEB-GOAL:g1:test]"

        sess1 = {
            "id": "sess-1",
            "title": marker,
            "sourceContext": {
                "source": "sources/github/owner/repo",
                "githubRepoContext": {"startingBranch": "feature/branch"},
            },
        }
        sess2 = {
            "id": "sess-2",
            "title": marker,
            "sourceContext": {
                "source": "sources/github/other/repo",
                "githubRepoContext": {"startingBranch": "feature/branch"},
            },
        }

        with patch("labeeb.providers.jules.run_cmd") as mock_cmd:
            mock_cmd.return_value = CmdResult(
                cmd=[],
                rc=0,
                stdout=json.dumps([sess1, sess2]),
                stderr="",
            )

            # Empty repo/branch argument -> rejected
            assert jules.find_sessions(marker, "", "feature/branch") == []
            assert jules.find_sessions(marker, "owner/repo", "") == []

            # Exact match succeeds and filters out sess-2
            matches = jules.find_sessions(marker, "owner/repo", "feature/branch")
            assert len(matches) == 1
            assert matches[0]["id"] == "sess-1"

    def test_reconcile_jules_create_blocks_on_missing_payload_metadata(self, tmp_path):
        paths = GoalPaths(tmp_path / "g1")
        store = GoalStore(paths)
        store.init_dirs()
        config = make_config()
        orchestrator = MagicMock()
        jules = MagicMock()
        em = EffectManager(config, paths, store, orchestrator, jules)

        state = {"goal_id": "g1", "phase": "EFFECT"}
        # Prepare action missing repo or branch
        action = em.prepare_effect(state, "jules_create", {"marker": "[TEST]"}, "WAITING_JULES")
        em.mark_in_flight(state)

        blocked_calls = []
        em.reconcile_effect(
            state,
            action,
            on_block=lambda r, ev: blocked_calls.append((r, ev)),
        )

        assert len(blocked_calls) == 1
        assert "lacks required repo, branch, or marker metadata" in blocked_calls[0][0]


class TestTask19ProvenJulesApproval:
    def test_reconcile_jules_approve_requires_plan_approved_activity(self, tmp_path):
        paths = GoalPaths(tmp_path / "g1")
        store = GoalStore(paths)
        store.init_dirs()
        config = make_config({"controller": {"reconcile_attempts": 1, "reconcile_delay_seconds": 0.01}})
        orchestrator = MagicMock()
        jules = MagicMock()
        em = EffectManager(config, paths, store, orchestrator, jules)

        state = {"goal_id": "g1", "phase": "EFFECT"}
        action = em.prepare_effect(state, "jules_approve", {"session_id": "sess-1"}, "WAITING_JULES")
        em.mark_in_flight(state)

        # Mock jules session state changed but no planApproved activity
        em.jules_get_fn = MagicMock(return_value={"id": "sess-1", "state": "IN_PROGRESS"})
        em.jules_logs_fn = MagicMock(return_value={"activities": [{"id": "act-1", "planGenerated": {}}]})

        blocked_calls = []
        em.reconcile_effect(
            state,
            action,
            on_block=lambda r, ev: blocked_calls.append((r, ev)),
        )

        # Blocked because planApproved activity is missing
        assert len(blocked_calls) == 1
        assert "refusing duplicate approval without proven planApproved activity" in blocked_calls[0][0]

    def test_reconcile_jules_approve_terminal_failure_blocks_immediately(self, tmp_path):
        paths = GoalPaths(tmp_path / "g1")
        store = GoalStore(paths)
        store.init_dirs()
        config = make_config({"controller": {"reconcile_attempts": 3, "reconcile_delay_seconds": 0.01}})
        orchestrator = MagicMock()
        jules = MagicMock()
        em = EffectManager(config, paths, store, orchestrator, jules)

        state = {"goal_id": "g1", "phase": "EFFECT"}
        action = em.prepare_effect(state, "jules_approve", {"session_id": "sess-1"}, "WAITING_JULES")
        em.mark_in_flight(state)

        em.jules_get_fn = MagicMock(return_value={"id": "sess-1", "state": "FAILED"})
        em.jules_logs_fn = MagicMock(return_value={"activities": []})

        blocked_calls = []
        em.reconcile_effect(
            state,
            action,
            on_block=lambda r, ev: blocked_calls.append((r, ev)),
        )

        assert len(blocked_calls) == 1
        assert "terminal state without proven plan approval" in blocked_calls[0][0]

    def test_reconcile_jules_approve_succeeds_with_plan_approved(self, tmp_path):
        paths = GoalPaths(tmp_path / "g1")
        store = GoalStore(paths)
        store.init_dirs()
        config = make_config({"controller": {"reconcile_attempts": 1, "reconcile_delay_seconds": 0.01}})
        orchestrator = MagicMock()
        jules = MagicMock()
        em = EffectManager(config, paths, store, orchestrator, jules)

        state = {"goal_id": "g1", "phase": "EFFECT"}
        action = em.prepare_effect(state, "jules_approve", {"session_id": "sess-1"}, "WAITING_JULES")
        em.mark_in_flight(state)

        em.jules_get_fn = MagicMock(return_value={"id": "sess-1", "state": "IN_PROGRESS"})
        em.jules_logs_fn = MagicMock(return_value={
            "activities": [
                {"id": "act-app", "planApproved": {}},
            ]
        })

        em.reconcile_effect(state, action)
        assert state["phase"] == "WAITING_JULES"
        assert state["pending_action"] is None


class TestTask20BoundedBrainTaskReconciliation:
    def test_reconcile_brain_launch_retries_and_adopts(self, tmp_path):
        paths = GoalPaths(tmp_path / "g1")
        store = GoalStore(paths)
        store.init_dirs()
        config = make_config({"controller": {"reconcile_attempts": 2, "reconcile_delay_seconds": 0.01}})
        orchestrator = MagicMock()
        jules = MagicMock()
        em = EffectManager(config, paths, store, orchestrator, jules)

        # Returns None on attempt 1, returns task on attempt 2
        orchestrator.find_by_name.side_effect = [None, {"taskId": "task-brain-99", "name": "goal-1-brain"}]

        state = {"goal_id": "g1", "phase": "EFFECT"}
        action = em.prepare_effect(
            state,
            "brain_launch",
            {"task_name": "goal-1-brain", "role": "brain"},
            "WAITING_BRAIN",
        )
        em.mark_in_flight(state)

        em.reconcile_effect(state, action)
        assert state["phase"] == "WAITING_BRAIN"
        assert state["latest_codex_task_id"] == "task-brain-99"

    def test_reconcile_brain_launch_blocks_on_ambiguous_effect(self, tmp_path):
        paths = GoalPaths(tmp_path / "g1")
        store = GoalStore(paths)
        store.init_dirs()
        config = make_config({"controller": {"reconcile_attempts": 3, "reconcile_delay_seconds": 0.01}})
        orchestrator = MagicMock()
        jules = MagicMock()
        em = EffectManager(config, paths, store, orchestrator, jules)

        orchestrator.find_by_name.side_effect = AmbiguousEffect("Multiple Orchestrator tasks found with name goal-1-brain")

        state = {"goal_id": "g1", "phase": "EFFECT"}
        action = em.prepare_effect(
            state,
            "brain_launch",
            {"task_name": "goal-1-brain", "role": "brain"},
            "WAITING_BRAIN",
        )
        em.mark_in_flight(state)

        blocked_calls = []
        em.reconcile_effect(
            state,
            action,
            on_block=lambda r, ev: blocked_calls.append((r, ev)),
        )

        assert len(blocked_calls) == 1
        assert "multiple tasks found with name goal-1-brain" in blocked_calls[0][0]

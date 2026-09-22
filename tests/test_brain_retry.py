"""Tests for brain transient retry and unblock-and-retry features."""
import json
import pathlib
import tempfile
import unittest

from labeeb.config import Config, load_config
from labeeb.core.controller import LabeebController
from labeeb.errors import ControllerError
from labeeb.models import DECISION_START, DECISION_END
from labeeb.providers.fakes import FakeCriticProvider, FakeJulesProvider, FakeOrchestratorProvider


BASE_CONFIG = r"""
[controller]
state_root = "{state_root}"
poll_seconds = 0.01
agent_poll_seconds = 0.01
goal_deadline_hours = 1
reconcile_attempts = 1
reconcile_delay_seconds = 0
brain_transient_retry_limit = 3
brain_retry_backoff_seconds = 0.01

[executables]
orchestrator = "orchestrator"
cjules = "cjules"
git = "git"
shell = "/bin/bash"

[orchestrator]
task_store = "{task_store}"

[timeouts]
launch_seconds = 5
orchestrator_read_wait_ms = 100
jules_read_seconds = 5
jules_write_seconds = 5
critic_seconds = 1
validation_command_seconds = 5
repair_activation_seconds = 1

[workflow]
brain_role = "brain"
critic_role = "critic"
implementer_role = "implementer"
pre_critic = "never"
post_critic = "never"
high_risk_tags = ["architecture"]
jules_require_plan_approval = false
repair_fallback = "blocked"
max_brain_resumes_per_thread = 0

[safety]
max_repair_rounds = 1
require_patch_for_implementation = true
require_validation_for_pass = true
critic_failure_blocks_high_risk = true
allow_push = false
allow_pr = false
allow_merge = false
allow_production_mutation = false

[jules_state_actions]
QUEUED = "wait"
PLANNING = "wait"
IN_PROGRESS = "wait"
AWAITING_PLAN_APPROVAL = "wake"
AWAITING_USER_FEEDBACK = "wake"
PAUSED = "wake"
COMPLETED = "review"
FAILED = "wake"
CANCELLED = "wake"
UNKNOWN = "block"

[roles.brain]
transport = "orchestrator"
runtime = "codex"
model = ""

[roles.critic]
transport = "direct"
read_only = true
command = ["false"]

[roles.implementer]
transport = "jules"
command = "cjules"
"""


class FailThenSucceedOrchestrator(FakeOrchestratorProvider):
    """Orchestrator that fails N times with retryable error, then succeeds."""

    def __init__(self, fail_count: int = 2, **kwargs):
        super().__init__(**kwargs)
        self.fail_count = fail_count
        self.calls = 0

    def wait_task(self, task_id, stop_checker=None, deadline_checker=None):
        self.calls += 1
        if self.calls <= self.fail_count:
            return {
                "taskId": task_id,
                "status": "failed",
                "active": False,
                "exitCode": 1,
                "error": "thread-store conflict: thread abc123 already has an active writer",
                "lastMessage": "Error: thread/resume: thread/resume failed",
            }
        return super().wait_task(task_id, stop_checker, deadline_checker)


class AlwaysFailOrchestrator(FakeOrchestratorProvider):
    """Orchestrator that always returns non-retryable failures."""

    def wait_task(self, task_id, stop_checker=None, deadline_checker=None):
        return {
            "taskId": task_id,
            "status": "failed",
            "active": False,
            "exitCode": 1,
            "error": "Model returned invalid JSON decision structure",
        }


class AlwaysRetryableFailOrchestrator(FakeOrchestratorProvider):
    """Orchestrator that always returns retryable failures (exhausts retries)."""

    def wait_task(self, task_id, stop_checker=None, deadline_checker=None):
        return {
            "taskId": task_id,
            "status": "failed",
            "active": False,
            "exitCode": 1,
            "error": "thread-store conflict: thread xyz already has an active writer",
        }


class BrainRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg_path = self.root / "config.toml"
        cfg_path.write_text(BASE_CONFIG.format(
            state_root=self.root / "state",
            task_store=self.root / "tasks",
        ))
        self.config = load_config(str(cfg_path))
        self.workspace = self.root / "repo"
        self.workspace.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_goal(self, brain_provider, **kwargs):
        return LabeebController.create_goal(
            self.config,
            intent="Test brain retry",
            workspace=str(self.workspace),
            repo="test-owner/test-repo",
            branch="main",
            risk_tags=[],
            allowed_paths=["src", "tests"],
            validation_commands=["true"],
            preauthorize_plan=True,
            deadline_hours=1,
            orchestrator_provider=brain_provider,
            jules_provider=kwargs.get("jules_provider", FakeJulesProvider()),
            critic_provider=kwargs.get("critic_provider", FakeCriticProvider()),
        )

    def test_retryable_error_triggers_retry_and_recovers(self):
        """Brain fails 2 times with thread-store conflict, then succeeds on 3rd."""
        brain = FailThenSucceedOrchestrator(fail_count=2)
        ctl = self._create_goal(brain)

        # Run through steps until we reach a non-EFFECT/non-THINKING state or terminal
        # The flow is: CREATED→EFFECT→THINKING→(fail→EFFECT→THINKING→fail→EFFECT→THINKING→succeed)
        for _ in range(30):  # safety limit
            state = ctl.store.load()
            if state["phase"] in {"BLOCKED", "PASS", "FAIL", "PLAN_GATE", "WAITING_JULES"}:
                break
            ctl.step(state)

        state = ctl.store.load()
        # After retries, brain should have succeeded (not BLOCKED)
        self.assertNotEqual(state["phase"], "BLOCKED",
                            f"Goal should not be BLOCKED after transient retry recovery, got phase={state['phase']}")
        # The retry counter should have been reset on success
        self.assertEqual(state.get("brain_transient_retries", 0), 0)
        # We should have seen 3 wait_task calls (2 failures + 1 success)
        self.assertEqual(brain.calls, 3)

    def test_non_retryable_error_blocks_immediately(self):
        """Non-retryable error should block without any retry attempts."""
        brain = AlwaysFailOrchestrator()
        ctl = self._create_goal(brain)

        state = ctl.store.load()
        # CREATED -> EFFECT -> THINKING
        ctl.step(state)
        ctl.step(state)
        # THINKING -> handle_brain_completion -> BLOCKED (no retry)
        ctl.step(state)

        state = ctl.store.load()
        self.assertEqual(state["phase"], "BLOCKED")
        self.assertEqual(state.get("brain_transient_retries", 0), 0)

    def test_retries_exhausted_then_blocks_with_real_error(self):
        """Retryable error exhausts limit and blocks with actual error message."""
        brain = AlwaysRetryableFailOrchestrator()
        ctl = self._create_goal(brain)

        state = ctl.store.load()
        # CREATED -> EFFECT -> THINKING
        ctl.step(state)
        ctl.step(state)

        # THINKING -> handle_brain_completion -> retry 3 times -> BLOCKED
        # The step function drives through the retries within handle_brain_completion
        # But retries launch new effects, so we need to step through them
        # Let's run until terminal
        for _ in range(30):  # safety limit
            state = ctl.store.load()
            if state["phase"] in {"BLOCKED", "PASS", "FAIL"}:
                break
            ctl.step(state)

        state = ctl.store.load()
        self.assertEqual(state["phase"], "BLOCKED")
        self.assertEqual(state.get("brain_transient_retries"), 3)

    def test_is_retryable_brain_failure(self):
        """Pattern matching correctly identifies retryable vs non-retryable errors."""
        brain = FakeOrchestratorProvider()
        ctl = self._create_goal(brain)

        # Retryable patterns
        self.assertTrue(ctl._is_retryable_brain_failure(
            {"error": "thread-store conflict: thread abc already has an active writer"}
        ))
        self.assertTrue(ctl._is_retryable_brain_failure(
            {"lastMessage": "ECONNRESET during codex invocation"}
        ))
        self.assertTrue(ctl._is_retryable_brain_failure(
            {"error": "connection refused by remote host"}
        ))

        # Non-retryable patterns
        self.assertFalse(ctl._is_retryable_brain_failure(
            {"error": "Model returned invalid JSON"}
        ))
        self.assertFalse(ctl._is_retryable_brain_failure(
            {"error": "Rate limit exceeded"}
        ))
        self.assertFalse(ctl._is_retryable_brain_failure({}))

    def test_format_brain_failure_reason_extracts_error_line(self):
        """Error formatting extracts useful Error: lines."""
        # Multi-line Codex error
        reason = LabeebController._format_brain_failure_reason({
            "error": (
                "2026-09-22T03:25:44Z ERROR codex: failed\n"
                "Error: thread/resume: thread abc already has an active writer"
            ),
        })
        self.assertIn("Error: thread/resume", reason)
        self.assertIn("Brain task failed", reason)

        # No error field
        reason2 = LabeebController._format_brain_failure_reason({"status": "cancelled"})
        self.assertIn("status: cancelled", reason2)


class UnblockAndRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg_path = self.root / "config.toml"
        cfg_path.write_text(BASE_CONFIG.format(
            state_root=self.root / "state",
            task_store=self.root / "tasks",
        ))
        self.config = load_config(str(cfg_path))
        self.workspace = self.root / "repo"
        self.workspace.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_blocked_goal(self, *, with_jules=False, with_plan=True):
        brain = FakeOrchestratorProvider()
        jules = FakeJulesProvider()
        ctl = LabeebController.create_goal(
            self.config,
            intent="Test unblock",
            workspace=str(self.workspace),
            repo="test/repo",
            branch="main",
            risk_tags=[],
            allowed_paths=[],
            validation_commands=["true"],
            preauthorize_plan=True,
            deadline_hours=1,
            orchestrator_provider=brain,
            jules_provider=jules,
            critic_provider=FakeCriticProvider(),
        )
        state = ctl.store.load()
        state["phase"] = "BLOCKED"
        if with_jules:
            state["jules_session_id"] = "fake-sess-123"
        if with_plan:
            state["plan_ref"] = "file:fake-plan"
        state["brain_transient_retries"] = 3
        ctl.store.save(state)
        return ctl

    def test_unblock_resets_to_reviewing_when_jules_done(self):
        ctl = self._create_blocked_goal(with_jules=True, with_plan=True)
        result = ctl.unblock_and_retry()
        self.assertEqual(result["phase"], "REVIEWING")
        state = ctl.store.load()
        self.assertEqual(state["phase"], "REVIEWING")
        self.assertEqual(state["brain_transient_retries"], 0)
        self.assertIsNone(state["pending_action"])
        self.assertIsNone(state["active_task"])

    def test_unblock_resets_to_thinking_when_plan_only(self):
        ctl = self._create_blocked_goal(with_jules=False, with_plan=True)
        result = ctl.unblock_and_retry()
        self.assertEqual(result["phase"], "THINKING")

    def test_unblock_resets_to_created_when_no_plan(self):
        ctl = self._create_blocked_goal(with_jules=False, with_plan=False)
        result = ctl.unblock_and_retry()
        self.assertEqual(result["phase"], "CREATED")

    def test_unblock_raises_if_not_blocked(self):
        brain = FakeOrchestratorProvider()
        ctl = LabeebController.create_goal(
            self.config,
            intent="Test unblock error",
            workspace=str(self.workspace),
            repo="test/repo",
            branch="main",
            risk_tags=[],
            allowed_paths=[],
            validation_commands=["true"],
            preauthorize_plan=True,
            deadline_hours=1,
            orchestrator_provider=brain,
            jules_provider=FakeJulesProvider(),
            critic_provider=FakeCriticProvider(),
        )
        with self.assertRaises(ControllerError) as ctx:
            ctl.unblock_and_retry()
        self.assertIn("not BLOCKED", str(ctx.exception))


class UnblockAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg_path = self.root / "config.toml"
        cfg_path.write_text(BASE_CONFIG.format(
            state_root=self.root / "state",
            task_store=self.root / "tasks",
        ))
        self.config = load_config(str(cfg_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_unblock_api_endpoint(self):
        from labeeb.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(self.config)
        client = TestClient(app)

        workspace = self.root / "repo"
        workspace.mkdir()

        # Create a goal
        resp = client.post("/api/goals", json={
            "intent": "Test unblock API",
            "workspace": str(workspace),
            "repo": "test/repo",
            "branch": "main",
            "background": False,
        })
        self.assertEqual(resp.status_code, 200)
        goal_id = resp.json()["goal_id"]

        # Manually set it to BLOCKED
        ctl = LabeebController(self.config, goal_id)
        state = ctl.store.load()
        state["phase"] = "BLOCKED"
        state["brain_transient_retries"] = 3
        ctl.store.save(state)

        # Call unblock API
        resp = client.post(f"/api/goals/{goal_id}/unblock", json={"background": False})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["goal_id"], goal_id)
        self.assertEqual(data["phase"], "CREATED")

        # Verify state was updated
        state = ctl.store.load()
        self.assertEqual(state["phase"], "CREATED")
        self.assertEqual(state["brain_transient_retries"], 0)

    def test_unblock_api_rejects_non_blocked(self):
        from labeeb.api.app import create_app
        from fastapi.testclient import TestClient

        app = create_app(self.config)
        client = TestClient(app)

        workspace = self.root / "repo"
        workspace.mkdir()

        resp = client.post("/api/goals", json={
            "intent": "Test unblock reject",
            "workspace": str(workspace),
            "repo": "test/repo",
            "branch": "main",
            "background": False,
        })
        goal_id = resp.json()["goal_id"]

        resp = client.post(f"/api/goals/{goal_id}/unblock", json={"background": False})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not BLOCKED", resp.json()["detail"])


class RecoveryModuleDirectTests(unittest.TestCase):
    """Direct tests for labeeb.core.recovery module."""

    def test_direct_recovery_exports(self):
        from labeeb.core import (
            RETRYABLE_BRAIN_PATTERNS,
            format_brain_failure_reason,
            is_retryable_brain_failure,
            retry_brain_with_fresh_thread,
            unblock_goal,
        )
        self.assertTrue(len(RETRYABLE_BRAIN_PATTERNS) > 0)
        self.assertTrue(callable(is_retryable_brain_failure))
        self.assertTrue(callable(format_brain_failure_reason))
        self.assertTrue(callable(retry_brain_with_fresh_thread))
        self.assertTrue(callable(unblock_goal))

    def test_is_retryable_helper(self):
        from labeeb.core.recovery import is_retryable_brain_failure

        self.assertTrue(is_retryable_brain_failure({"error": "thread-store conflict"}))
        self.assertTrue(is_retryable_brain_failure({"lastMessage": "socket hang up"}))
        self.assertFalse(is_retryable_brain_failure({"error": "syntax error"}))
        self.assertFalse(is_retryable_brain_failure({}))

    def test_format_failure_reason_helper(self):
        from labeeb.core.recovery import format_brain_failure_reason

        res = format_brain_failure_reason({"error": "Something\nError: specific issue"})
        self.assertEqual(res, "Brain task failed: Error: specific issue")
        res2 = format_brain_failure_reason({"status": "failed"})
        self.assertEqual(res2, "Brain task did not succeed (status: failed)")


if __name__ == "__main__":
    unittest.main()


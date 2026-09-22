"""Regression test suite for Group 1 (Core Safety), Task 9 (Asset Sanitization), and Group 3 (Web & API Bugs)."""
import os
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from labeeb.api.app import create_app
from labeeb.config import load_config
from labeeb.core.controller import LabeebController
from labeeb.core.events import append_domain_event, read_domain_events
from labeeb.core.recovery import unblock_goal
from labeeb.errors import ControllerError
from labeeb.models import DomainEvent, utc_now

BASE_CONFIG = r"""
[controller]
state_root = "{state_root}"
poll_seconds = 0.01
agent_poll_seconds = 0.01
goal_deadline_hours = 1
reconcile_attempts = 1
reconcile_delay_seconds = 0
max_handled_event_keys = 20
brain_transient_retry_limit = 3

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
UNKNOWN = "block"
IN_PROGRESS = "wait"
AWAITING_PLAN_APPROVAL = "wake"
AWAITING_USER_FEEDBACK = "wake"
COMPLETED = "review"
PAUSED = "wake"
FAILED = "wake"
CANCELLED = "wake"

[roles.brain]
runtime = "codex"
transport = "orchestrator"

[roles.critic]
runtime = "claude"
model = "claude-3-opus-20240229"

[roles.implementer]
runtime = "jules"
provider = "cjules"
"""


class StabilizationPassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.state_root = self.root / "state"
        self.task_store = self.root / "tasks"
        self.cfg_file = self.root / "config.toml"
        self.cfg_file.write_text(
            BASE_CONFIG.format(state_root=str(self.state_root), task_store=str(self.task_store)),
            encoding="utf-8",
        )
        self.config = load_config(self.cfg_file)
        self.app = create_app(self.config)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def _create_test_goal(self, preauth: bool = True) -> LabeebController:
        return LabeebController.create_goal(
            self.config,
            intent="Stabilization test intent",
            workspace=str(self.root / "workspace"),
            repo="owner/repo",
            branch="main",
            risk_tags=["architecture"],
            allowed_paths=["src"],
            validation_commands=["true"],
            preauthorize_plan=preauth,
        )

    def test_task3_ambiguous_writes_protection(self):
        """Task 3: block() marks in-flight effect as AMBIGUOUS and unblock_goal refuses to unblock."""
        ctl = self._create_test_goal()
        state = ctl.store.load()

        # Simulate prepared in-flight action
        ctl.prepare_effect(state, "jules_create", {"marker": "test-marker"}, "WAITING_JULES")
        ctl.effects.mark_in_flight(state)
        self.assertEqual(state["pending_action"]["stage"], "IN_FLIGHT")

        # Now block() is called
        ctl.block(state, "Jules creation timed out or unacknowledged")
        reloaded = ctl.store.load()
        self.assertEqual(reloaded["phase"], "BLOCKED")
        self.assertEqual(reloaded["pending_action"]["stage"], "AMBIGUOUS")
        self.assertIsNotNone(reloaded.get("blocked_action"))
        self.assertEqual(reloaded["blocked_action"]["stage"], "AMBIGUOUS")

        # unblock_goal must refuse!
        with self.assertRaises(ControllerError) as ctx:
            ctl.unblock_and_retry()
        self.assertIn("unresolved ambiguous write", str(ctx.exception))

    def test_task4_clear_stop_request(self):
        """Task 4: stop request is cleared upon unblock or resume."""
        ctl = self._create_test_goal()
        state = ctl.store.load()

        # Request stop
        ctl.request_stop()
        self.assertTrue(ctl.stop_requested())

        # Calling clear_stop_request removes it
        ctl.clear_stop_request()
        self.assertFalse(ctl.stop_requested())

        # Also test via unblock_goal
        ctl.request_stop()
        state["phase"] = "BLOCKED"
        ctl.store.save(state)
        ctl.unblock_and_retry()
        self.assertFalse(ctl.stop_requested())

    def test_task5_durable_event_journal(self):
        """Task 5: DomainEvent journaling and reading from events.jsonl."""
        ctl = self._create_test_goal()
        events_file = ctl.paths.events

        # Initial creation emitted goal.created
        events, count = read_domain_events(events_file, after_line=0)
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "goal.created")
        self.assertTrue(bool(events[0].get("event_id")))

        # Record custom event
        ctl.record_event("test.custom_event", {"foo": "bar"})
        events, new_count = read_domain_events(events_file, after_line=count)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "test.custom_event")
        self.assertEqual(events[0]["data"]["foo"], "bar")

    def test_task6_prepared_vs_dispatched_event_types(self):
        """Task 6: Differentiate jules.dispatch_prepared and jules.dispatched."""
        ctl = self._create_test_goal()
        state = ctl.store.load()

        plan_payload = {
            "plan_summary": "Plan for task 6",
            "execution": {
                "jules_prompt": "Do work",
                "allowed_paths": ["src"],
                "validation_commands": ["true"],
            },
        }
        state["plan_ref"] = ctl.store.write_json(ctl.paths.plan, plan_payload)
        ctl.store.save(state)

        # dispatch_jules prepares effect and records jules.dispatch_prepared
        ctl.dispatch_jules(state)
        events, _ = read_domain_events(ctl.paths.events, after_line=0)
        event_types = [e["event_type"] for e in events]
        self.assertIn("jules.dispatch_prepared", event_types)
        self.assertNotIn("jules.dispatched", event_types)

        # Mock JulesProvider create_session and execute effect
        ctl.effects.jules.create_session = MagicMock(return_value={"id": "sessions/s123"})
        ctl.effects.execute_pending_effect(state, on_block=lambda r, ev: ctl.block(state, r, evidence=ev))

        # Now jules.dispatched should be acknowledged and recorded
        events, _ = read_domain_events(ctl.paths.events, after_line=0)
        event_types = [e["event_type"] for e in events]
        self.assertIn("jules.dispatched", event_types)

    def test_task9_asset_sanitization(self):
        """Task 9: Check base.html and router.py have no unpkg CDN fallback or hardcoded machine path."""
        base_html_path = pathlib.Path(__file__).parent.parent / "labeeb" / "web" / "templates" / "base.html"
        base_html_content = base_html_path.read_text(encoding="utf-8")
        self.assertNotIn("unpkg.com", base_html_content)

        router_path = pathlib.Path(__file__).parent.parent / "labeeb" / "web" / "router.py"
        router_content = router_path.read_text(encoding="utf-8")
        self.assertNotIn("/home/hany", router_content)

    def test_task10_and_task12_routes_goals_contextlib_and_async_resume(self):
        """Task 10 & 12: Test resume endpoint without blocking event loop and with contextlib imported."""
        ctl = self._create_test_goal(preauth=False)
        state = ctl.store.load()
        state["phase"] = "PLAN_GATE"
        ctl.store.save(state)

        resp = self.client.post(f"/api/goals/{ctl.goal_id}/resume", json={"once": True})
        self.assertEqual(resp.status_code, 200)

    def test_task11_config_form_update(self):
        """Task 11: update_config supports both form submissions and JSON."""
        # Test JSON
        resp = self.client.get("/api/config")
        toml_text = resp.json()["toml_text"]
        new_toml = toml_text.replace("goal_deadline_hours = 1", "goal_deadline_hours = 2")
        put_json = self.client.put("/api/config", json={"raw_toml": new_toml})
        self.assertEqual(put_json.status_code, 200)

        # Test Form submission (application/x-www-form-urlencoded)
        newer_toml = new_toml.replace("goal_deadline_hours = 2", "goal_deadline_hours = 3")
        put_form = self.client.put(
            "/api/config",
            data={"raw_toml": newer_toml},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(put_form.status_code, 200)
        self.assertEqual(put_form.json()["status"], "updated")

    def test_task13_and_task14_web_templates_and_detail_route(self):
        """Task 13 & 14: Test goal_detail_page context and HTML rendering."""
        ctl = self._create_test_goal()
        # Record events for timeline
        ctl.record_event("custom.phase_shift", {"phase": "TESTING"})

        # Write mock critic review
        critique = {
            "action": "CONVERGE",
            "findings": [{"finding": "Mock finding", "impact": "High", "location": "src/main.py"}],
        }
        ctl.store.write_json(ctl.paths.reviews / "critic-pre-test.json", critique)

        resp = self.client.get(f"/goals/{ctl.goal_id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        # Repair budget shows 0/1 Used
        self.assertIn("0/1 Used", html)
        # Custom timeline event rendered
        self.assertIn("custom.phase_shift", html)
        # Critic finding rendered
        self.assertIn("Mock finding", html)


if __name__ == "__main__":
    unittest.main()

"""Web UI route rendering and form submission tests."""
import pathlib
import tempfile
import unittest
import unittest.mock

from fastapi.testclient import TestClient

from labeeb.api.app import create_app
from labeeb.config import load_config

BASE_CONFIG = r"""
[controller]
state_root = "{state_root}"
poll_seconds = 0.01
agent_poll_seconds = 0.01
goal_deadline_hours = 1
reconcile_attempts = 1
reconcile_delay_seconds = 0
max_handled_event_keys = 20

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


class WebUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.cfg_file = self.root / "config.toml"
        self.cfg_file.write_text(BASE_CONFIG.format(state_root=self.root / "state", task_store=self.root / "tasks"))
        self.config = load_config(str(self.cfg_file))
        self.app = create_app(self.config)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_static_css_served(self):
        resp = self.client.get("/static/app.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Labeeb Orchestrator Developer UI Theme", resp.text)

    def test_index_and_dashboard(self):
        resp = self.client.get("/", follow_redirects=False)
        self.assertIn(resp.status_code, {302, 303, 307})
        self.assertEqual(resp.headers["location"], "/dashboard")

        resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Orchestration Goals", resp.text)
        self.assertIn("TOTAL GOALS", resp.text)

    def test_doctor_page(self):
        resp = self.client.get("/doctor")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Preflight Diagnostic Report", resp.text)

    def test_config_page(self):
        resp = self.client.get("/config")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("System Configuration", resp.text)
        self.assertIn("Raw TOML Editor", resp.text)

    def test_goal_creation_and_detail(self):
        resp = self.client.get("/goals/new")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Create Engineering Goal", resp.text)

        # Form submission creating a goal
        form_payload = {
            "intent": "Test UI form submission intent",
            "workspace": str(self.root / "repo"),
            "repo": "owner/repo",
            "branch": "main",
            "deadline_hours": "12",
            "preauthorize_plan": "true",
            "risk_tags": ["architecture", "security"],
            "allowed_paths": ["src"],
            "validation_commands": ["true"],
        }
        with unittest.mock.patch("labeeb.core.controller.LabeebController.start_background"):
            resp = self.client.post("/goals/new", data=form_payload, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        goal_redirect_url = resp.headers["location"]
        self.assertTrue(goal_redirect_url.startswith("/goals/"))

        # Follow to goal detail page
        resp = self.client.get(goal_redirect_url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Test UI form submission intent", resp.text)
        self.assertIn("Timeline &amp; Events", resp.text)
        self.assertIn("Contract &amp; Plan", resp.text)


if __name__ == "__main__":
    unittest.main()

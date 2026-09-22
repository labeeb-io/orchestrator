"""Phase 6 Web UI pipeline visualization and artifacts/report API tests."""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from fastapi.testclient import TestClient

from labeeb.api.app import create_app
from labeeb.config import load_config
from labeeb.core.artifacts import AUTHORITY_CONTEXT, GOAL_CONTRACT, PROOF_CONTRACT
from labeeb.core.controller import LabeebController

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

[workflow]
brain_role = "brain"
critic_role = "critic"
implementer_role = "implementer"
pre_critic = "never"
post_critic = "never"
high_risk_tags = []
jules_require_plan_approval = false

[safety]
max_repair_rounds = 1
require_patch_for_implementation = true
require_validation_for_pass = true
allow_push = false
allow_pr = false
allow_merge = false
allow_production_mutation = false

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


class WebPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.cfg_file = self.root / "config.toml"
        self.cfg_file.write_text(BASE_CONFIG.format(state_root=self.root / "state", task_store=self.root / "tasks"))
        self.config = load_config(str(self.cfg_file))
        self.app = create_app(self.config)
        self.client = TestClient(self.app)

        # Create a test goal
        self.ctl = LabeebController.create_goal(
            self.config,
            intent="Validate Phase 6 Web Pipeline UI",
            workspace=str(self.root / "workspace"),
            repo="owner/repo",
            branch="main",
            risk_tags=[],
            allowed_paths=["src"],
            validation_commands=["true"],
            preauthorize_plan=True,
        )
        self.goal_id = self.ctl.goal_id

    def tearDown(self):
        self.tmp.cleanup()

    def test_goal_detail_renders_macro_pipeline(self):
        resp = self.client.get(f"/goals/{self.goal_id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        # Verify 4-Stage Macro Loop rendered
        self.assertIn("Macro Loop Control Plane", html)
        self.assertIn("1. THINK / CONVERGE", html)
        self.assertIn("2. EXECUTE", html)
        self.assertIn("3. PROVE", html)
        self.assertIn("4. REPORTING", html)

    def test_goal_detail_renders_reasoning_drawer(self):
        resp = self.client.get(f"/goals/{self.goal_id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        # Verify Reasoning Activity Drawer rendered
        self.assertIn("Reasoning Graph &amp; Activity Trajectory", html)
        self.assertIn("Authority Context", html)
        self.assertIn("Goal Contract", html)
        self.assertIn("Reality Audit", html)
        self.assertIn("Defect Diagnosis", html)
        self.assertIn("Claude Critic", html)
        self.assertIn("Readiness Gate", html)

    def test_goal_detail_renders_artifacts_and_report_tabs(self):
        resp = self.client.get(f"/goals/{self.goal_id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        # Verify new tabs exist in tabs-nav
        self.assertIn('id="tab-btn-artifacts"', html)
        self.assertIn("Artifacts Vault", html)
        self.assertIn('id="tab-btn-report"', html)
        self.assertIn("Final Report", html)

        # Verify tab content panes exist
        self.assertIn('id="tab-artifacts"', html)
        self.assertIn('id="tab-report"', html)

    def test_artifacts_api_endpoints(self):
        # Write an artifact
        state = self.ctl.store.load()
        self.ctl.artifact_store.write_artifact(
            state=state,
            artifact_type=GOAL_CONTRACT,
            data={"observable_outcome": "Tests pass cleanly"},
            producer="brain",
        )

        # 1. Test listing artifacts
        resp = self.client.get(f"/api/goals/{self.goal_id}/artifacts")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("artifacts", data)
        self.assertIn(GOAL_CONTRACT, data["artifacts"])

        # 2. Test fetching specific artifact
        resp_art = self.client.get(f"/api/goals/{self.goal_id}/artifacts/{GOAL_CONTRACT}")
        self.assertEqual(resp_art.status_code, 200)
        art_data = resp_art.json()
        self.assertEqual(art_data.get("artifact_type"), GOAL_CONTRACT)
        self.assertEqual(art_data.get("data", {}).get("observable_outcome"), "Tests pass cleanly")

        # 3. Test 404 for non-existent artifact
        resp_404 = self.client.get(f"/api/goals/{self.goal_id}/artifacts/non_existent_type")
        self.assertEqual(resp_404.status_code, 404)

    def test_report_api_and_rendered_report_in_ui(self):
        state = self.ctl.store.load()
        self.ctl.pass_goal(
            state,
            decision={"reason": "Deterministic verification green"},
            evidence={"validation": {"status": "PASS", "exit_code": 0}},
        )

        # 1. Check API endpoint
        resp = self.client.get(f"/api/goals/{self.goal_id}/report")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "PASS")
        self.assertIn("markdown", data)
        self.assertIn("Goal Report", data["markdown"])
        self.assertIn("> [!SUCCESS] Goal PASSED", data["markdown"])

        # 2. Check UI detail page renders the report
        resp_ui = self.client.get(f"/goals/{self.goal_id}")
        self.assertEqual(resp_ui.status_code, 200)
        self.assertIn("final-report-raw-text", resp_ui.text)
        self.assertIn("Goal Report", resp_ui.text)

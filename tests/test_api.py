"""FastAPI route integration tests."""
import pathlib
import tempfile
import unittest

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


class APITests(unittest.TestCase):
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

    def test_root_and_doctor(self):
        resp = self.client.get("/api")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "running")

        resp = self.client.get("/api/system/doctor")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("version", data)
        self.assertIn("executables", data)

    def test_config_endpoints(self):
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("raw", data)
        self.assertIn("toml_text", data)

        # Invalid invariant update rejected
        bad_toml = resp.json()["toml_text"].replace("max_repair_rounds = 1", "max_repair_rounds = 5")
        bad_resp = self.client.put("/api/config", json={"raw_toml": bad_toml})
        self.assertEqual(bad_resp.status_code, 422)

    def test_goal_crud_and_stop(self):
        # List goals (empty)
        resp = self.client.get("/api/goals")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

        # Create goal (non-background for direct inspection)
        create_payload = {
            "intent": "Test API goal creation",
            "workspace": str(self.root / "workspace"),
            "repo": "owner/repo",
            "branch": "main",
            "risk_tags": ["architecture"],
            "allowed_paths": ["src"],
            "validation_commands": ["true"],
            "preauthorize_plan": True,
            "background": False,
        }
        resp = self.client.post("/api/goals", json=create_payload)
        self.assertEqual(resp.status_code, 200)
        goal_id = resp.json()["goal_id"]

        # Get goal
        resp = self.client.get(f"/api/goals/{goal_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["goal_id"], goal_id)
        self.assertEqual(data["phase"], "CREATED")
        self.assertEqual(data["contract"]["intent"], "Test API goal creation")

        # Stop goal
        resp = self.client.post(f"/api/goals/{goal_id}/stop")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["stop_requested"])

    def test_workspace_endpoints(self):
        # 1. Presets endpoint
        resp = self.client.get("/api/workspaces/presets")
        self.assertEqual(resp.status_code, 200)
        presets = resp.json()
        self.assertIsInstance(presets, list)
        self.assertTrue(any(p["id"] == "labeeb2025" for p in presets))

        # 2. Inspect endpoint with non-existent path
        resp = self.client.get("/api/workspaces/inspect?path=/non/existent/path")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["valid"])
        self.assertIn("not found", data["error"].lower())

        # 3. Inspect endpoint with self-contained git repo
        test_repo_dir = self.root / "test_repo"
        test_repo_dir.mkdir(parents=True, exist_ok=True)
        import subprocess
        subprocess.run(["git", "init", "-b", "master"], cwd=str(test_repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(test_repo_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(test_repo_dir), check=True)
        subprocess.run(["git", "config", "remote.origin.url", "git@github.com:labeeb-io/labeeb.git"], cwd=str(test_repo_dir), check=True)
        (test_repo_dir / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=str(test_repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(test_repo_dir), check=True, capture_output=True)

        resp = self.client.get(f"/api/workspaces/inspect?path={test_repo_dir}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["valid"])
        self.assertEqual(data["repo"], "labeeb-io/labeeb")
        self.assertEqual(data["default_branch"], "master")
        self.assertIn("master", data["branches"])

    def test_web_routes(self):
        # 1. New goal page
        resp = self.client.get("/goals/new")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Target Project &amp; Workspace", resp.text)
        self.assertIn("labeeb2025", resp.text)
        self.assertIn("Risk Classification &amp; Safety Pipeline", resp.text)

        # 2. Goal detail page
        create_payload = {
            "intent": "Test web detail page",
            "workspace": str(self.root / "workspace"),
            "repo": "owner/repo",
            "branch": "main",
            "risk_tags": ["architecture"],
            "allowed_paths": ["src"],
            "validation_commands": ["true"],
            "preauthorize_plan": True,
            "background": False,
        }
        resp = self.client.post("/api/goals", json=create_payload)
        goal_id = resp.json()["goal_id"]

        detail_resp = self.client.get(f"/goals/{goal_id}")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertIn("Visual Summary &amp; Logs", detail_resp.text)
        self.assertIn("tab-summary", detail_resp.text)

    def test_artifact_endpoint_integrity_and_legacy_fallback(self):
        import json
        from labeeb.core.controller import LabeebController

        create_payload = {
            "intent": "Test artifact integrity endpoint",
            "workspace": str(self.root / "workspace"),
            "repo": "owner/repo",
            "branch": "main",
            "risk_tags": [],
            "allowed_paths": [],
            "validation_commands": [],
            "preauthorize_plan": True,
            "background": False,
        }
        resp = self.client.post("/api/goals", json=create_payload)
        self.assertEqual(resp.status_code, 200)
        goal_id = resp.json()["goal_id"]

        ctl = LabeebController(self.config, goal_id)
        state = ctl.store.load()

        # 1. Write a valid indexed artifact with hash
        ctl.artifact_store.write_artifact(
            state=state,
            artifact_type="goal_contract",
            data={"outcome": "original valid contract"},
            producer="brain",
            activity_status="SATISFIED",
        )
        ctl.store.save(state)

        # Retrieve valid artifact -> 200 OK
        resp = self.client.get(f"/api/goals/{goal_id}/artifacts/goal_contract")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["outcome"], "original valid contract")

        # 2. Tamper with the artifact file on disk
        art_ref = state["artifacts"]["goal_contract"]["ref"]
        art_path = pathlib.Path(art_ref.split("#sha256=")[0].replace("file:", ""))
        self.assertTrue(art_path.exists())
        # Overwrite file content behind the back of the store to create hash mismatch
        art_path.write_text(json.dumps({"outcome": "tampered malicious content"}), encoding="utf-8")

        # Retrieval of tampered artifact MUST return HTTP 409 Conflict
        tampered_resp = self.client.get(f"/api/goals/{goal_id}/artifacts/goal_contract")
        self.assertEqual(tampered_resp.status_code, 409)
        self.assertIn("Artifact integrity verification failed", tampered_resp.json()["detail"])

        # 3. Legacy session fallback: artifact file exists on disk, but has no index entry in state['artifacts']
        ctl.paths.artifacts.mkdir(parents=True, exist_ok=True)
        legacy_path = ctl.paths.artifacts / "legacy_plan.v1.json"
        legacy_path.write_text(json.dumps({"plan": "legacy unindexed artifact"}), encoding="utf-8")

        legacy_resp = self.client.get(f"/api/goals/{goal_id}/artifacts/legacy_plan")
        self.assertEqual(legacy_resp.status_code, 200)
        self.assertEqual(legacy_resp.json()["plan"], "legacy unindexed artifact")

        # 4. Non-existent artifact -> 404
        missing_resp = self.client.get(f"/api/goals/{goal_id}/artifacts/does_not_exist")
        self.assertEqual(missing_resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()

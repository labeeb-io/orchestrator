"""Unit and integration tests for modular Labeeb backend core."""
import pathlib
import tempfile
import unittest

from labeeb.config import Config, load_config, validate_config
from labeeb.core.controller import LabeebController
from labeeb.errors import ConfigError
from labeeb.providers.fakes import FakeCriticProvider, FakeJulesProvider, FakeOrchestratorProvider

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


class BackendCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg = self.root / "config.toml"
        cfg.write_text(BASE_CONFIG.format(state_root=self.root / "state", task_store=self.root / "tasks"))
        self.config = load_config(str(cfg))
        self.workspace = self.root / "repo"
        self.workspace.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_safety_invariants_enforced(self):
        # max_repair_rounds != 1 must fail
        bad_raw = dict(self.config.raw)
        bad_raw["safety"] = dict(bad_raw.get("safety", {}))
        bad_raw["safety"]["max_repair_rounds"] = 2
        bad_cfg = Config(self.root / "bad.toml", bad_raw)
        with self.assertRaises(ConfigError):
            validate_config(bad_cfg)

        # allow_push = true must fail
        bad_raw["safety"]["max_repair_rounds"] = 1
        bad_raw["safety"]["allow_push"] = True
        with self.assertRaises(ConfigError):
            validate_config(bad_cfg)

    def test_fake_providers_and_step_lifecycle(self):
        fake_brain = FakeOrchestratorProvider()
        fake_jules = FakeJulesProvider(initial_state="IN_PROGRESS")
        fake_critic = FakeCriticProvider()

        ctl = LabeebController.create_goal(
            self.config,
            intent="Test automated backend execution",
            workspace=str(self.workspace),
            repo="test-owner/test-repo",
            branch="main",
            risk_tags=["architecture"],
            allowed_paths=["src", "tests"],
            validation_commands=["true"],
            preauthorize_plan=True,
            deadline_hours=1,
            orchestrator_provider=fake_brain,
            jules_provider=fake_jules,
            critic_provider=fake_critic,
        )

        state = ctl.store.load()
        self.assertEqual(state["phase"], "CREATED")

        # Step 1: CREATED -> EFFECT (brain_launch)
        progressed = ctl.step(state)
        self.assertTrue(progressed)
        self.assertEqual(state["phase"], "EFFECT")
        self.assertEqual(state["pending_action"]["kind"], "brain_launch")

        # Step 2: EFFECT -> THINKING
        progressed = ctl.step(state)
        self.assertTrue(progressed)
        self.assertEqual(state["phase"], "THINKING")

        # Step 3: THINKING -> handle_brain_completion -> PLAN_GATE (preauthorized) -> EFFECT (jules_create)
        progressed = ctl.step(state)
        self.assertTrue(progressed)
        # Because preauthorize_plan is True, it transitions through PLAN_GATE to EFFECT (jules_create)
        self.assertIn(state["phase"], {"PLAN_GATE", "EFFECT", "WAITING_JULES"})

        if state["phase"] == "PLAN_GATE":
            ctl.step(state)

        if state["phase"] == "EFFECT":
            self.assertEqual(state["pending_action"]["kind"], "jules_create")
            ctl.step(state)
            self.assertEqual(state["phase"], "WAITING_JULES")

        # Verify listing goals returns this goal
        all_goals = LabeebController.list_goals(self.config)
        self.assertEqual(len(all_goals), 1)
        self.assertEqual(all_goals[0]["goal_id"], ctl.goal_id)
        self.assertEqual(all_goals[0]["intent"], "Test automated backend execution")


if __name__ == "__main__":
    unittest.main()

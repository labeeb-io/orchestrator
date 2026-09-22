import json
import pathlib
import tempfile
import unittest
from unittest import mock

import labeeb_controller as lc


BASE_CONFIG = r'''
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
'''


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        cfg = root / "config.toml"
        cfg.write_text(BASE_CONFIG.format(state_root=root / "state", task_store=root / "tasks"))
        self.config = lc.load_config(str(cfg))
        self.workspace = root / "repo"
        self.workspace.mkdir()
        self.ctl = lc.LabeebController.create_goal(
            self.config,
            intent="test intent",
            workspace=str(self.workspace),
            repo="owner/repo",
            branch="main",
            risk_tags=[],
            allowed_paths=["src", "tests"],
            validation_commands=["true"],
            preauthorize_plan=True,
            deadline_hours=1,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_ref_detects_mutation(self):
        p = self.ctl.paths.evidence / "x.json"
        ref = self.ctl.store.write_json(p, {"a": 1})
        self.assertEqual(lc.read_ref_json(ref), {"a": 1})
        p.write_text('{"a":2}')
        with self.assertRaises(lc.ControllerError):
            lc.read_ref_json(ref)

    def test_changed_paths(self):
        patch = """diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@\n+x\ndiff --git a/tests/t.py b/tests/t.py\n--- a/tests/t.py\n+++ b/tests/t.py\n"""
        self.assertEqual(lc.changed_paths_from_patch(patch), ["src/a.py", "tests/t.py"])
        self.assertTrue(lc.path_allowed("src/a.py", ["src"]))
        self.assertFalse(lc.path_allowed("docs/a.md", ["src"]))

    def test_completed_initial_is_review_event(self):
        state = self.ctl.store.load()
        session = {"state": "COMPLETED", "updateTime": "2026-01-01T00:00:00Z"}
        logs = {"activities": [{"id": "a1", "createTime": "2026-01-01T00:00:00Z", "agentMessaged": {"agentMessage": "done"}}]}
        event = self.ctl.meaningful_event(state, session, logs)
        self.assertIsNotNone(event)
        self.assertEqual(event[1]["round"], "initial")

    def test_repair_rejects_stale_completed_and_accepts_new_round(self):
        state = self.ctl.store.load()
        old = [{"id": "a1", "createTime": "2026-01-01T00:00:00Z", "agentMessaged": {"agentMessage": "old"}}]
        marker = "[LABEEB-REPAIR:test:1]"
        anchor = {
            "activity_keys": [lc.activity_key(a) for a in old],
            "repair_marker": marker,
            "session_id": "s1",
            "reserved_at": lc.utc_now(),
        }
        state["repair_reserved"] = True
        state["round_anchor_ref"] = self.ctl.store.write_json(self.ctl.paths.evidence / "anchor.json", anchor)
        self.ctl.store.save(state)
        session = {"state": "COMPLETED", "updateTime": "2026-01-01T00:01:00Z"}
        self.assertIsNone(self.ctl.meaningful_event(state, session, {"activities": old}))
        new = old + [
            {"id": "a2", "createTime": "2026-01-01T00:02:00Z", "userMessaged": {"userMessage": marker}},
            {"id": "a3", "createTime": "2026-01-01T00:03:00Z", "agentMessaged": {"agentMessage": "fixed " + marker}},
        ]
        event = self.ctl.meaningful_event(state, session, {"activities": new})
        self.assertIsNotNone(event)
        self.assertEqual(event[1]["round"], "repair")

    def test_reconcile_message_adopts_matching_marker(self):
        state = self.ctl.store.load()
        state["phase"] = "EFFECT"
        marker = "[M]"
        ref = self.ctl.store.write_json(self.ctl.paths.requests / "p.json", {"session_id": "s1", "marker": marker, "message": marker})
        state["pending_action"] = {
            "id": "op1",
            "kind": "jules_message",
            "stage": "IN_FLIGHT",
            "payload_ref": ref,
            "next_phase": "WAITING_JULES",
        }
        self.ctl.store.save(state)
        with mock.patch.object(self.ctl, "jules_logs", return_value={"activities": [{"userMessaged": {"userMessage": marker}}]}):
            self.ctl.reconcile_effect(state, state["pending_action"])
        now = self.ctl.store.load()
        self.assertEqual(now["phase"], "WAITING_JULES")
        self.assertIsNone(now["pending_action"])

    def test_precritic_runs_only_once(self):
        state = self.ctl.store.load()
        state["phase"] = "THINKING"
        state["latest_codex_task_id"] = "task-1"
        self.ctl.store.save(state)
        decision = {
            "action": "PLAN_READY",
            "goal_contract": {},
            "execution": {
                "jules_prompt": "do it",
                "validation_commands": ["true"],
                "allowed_paths": ["src"],
                "risk_tags": ["architecture"],
                "needs_pre_critic": True,
                "needs_post_critic": False,
            },
            "plan_summary": "plan",
        }
        with mock.patch.object(self.ctl, "perform_critic", return_value={"material_findings": []}) as critic:
            with mock.patch.object(self.ctl, "prepare_effect", side_effect=lambda st, *a, **kw: self.ctl.store.save(st)) as prepare:
                self.ctl.handle_plan_decision(state, decision)
                self.assertTrue(state["pre_critic_done"])
                critic.assert_called_once()
                prepare.assert_called_once()
        # Simulate convergence PLAN_READY: critic must not repeat.
        state = self.ctl.store.load()
        state["phase"] = "THINKING"
        self.ctl.store.save(state)
        with mock.patch.object(self.ctl, "perform_critic") as critic:
            with mock.patch.object(self.ctl, "prepare_effect", side_effect=lambda st, *a, **kw: self.ctl.store.save(st)):
                self.ctl.handle_plan_decision(state, decision)
                critic.assert_not_called()
        self.assertEqual(self.ctl.store.load()["phase"], "PLAN_GATE")

    def test_wake_event_is_reserved_with_pending_effect(self):
        state = self.ctl.store.load()
        state["phase"] = "WAITING_JULES"
        state["latest_codex_task_id"] = "brain-task-1"
        state["jules_session_id"] = "s1"
        self.ctl.store.save(state)
        session = {"state": "AWAITING_USER_FEEDBACK", "updateTime": "2026-01-01T00:00:00Z"}
        logs = {"activities": [{"id": "q1", "createTime": "2026-01-01T00:00:00Z", "agentMessaged": {"agentMessage": "need input"}}]}
        with mock.patch.object(self.ctl, "jules_get", return_value=session), mock.patch.object(self.ctl, "jules_logs", return_value=logs):
            progressed = self.ctl.step_waiting_jules(state)
        self.assertTrue(progressed)
        now = self.ctl.store.load()
        self.assertEqual(now["phase"], "EFFECT")
        self.assertIsNotNone(now["pending_action"])
        self.assertEqual(now["pending_action"]["kind"], "brain_resume")
        self.assertEqual(len(now["handled_event_keys"]), 1)

    def test_completed_event_is_reserved_with_validation_transition(self):
        state = self.ctl.store.load()
        state["phase"] = "WAITING_JULES"
        state["latest_codex_task_id"] = "brain-task-1"
        state["jules_session_id"] = "s1"
        self.ctl.store.save(state)
        session = {"state": "COMPLETED", "updateTime": "2026-01-01T00:00:00Z"}
        logs = {"activities": [{"id": "a1", "createTime": "2026-01-01T00:00:00Z", "agentMessaged": {"agentMessage": "done"}}]}
        with mock.patch.object(self.ctl, "jules_get", return_value=session), mock.patch.object(self.ctl, "jules_logs", return_value=logs):
            progressed = self.ctl.step_waiting_jules(state)
        self.assertTrue(progressed)
        now = self.ctl.store.load()
        self.assertEqual(now["phase"], "VALIDATING")
        self.assertIsNotNone(now["review_ref"])
        self.assertEqual(len(now["handled_event_keys"]), 1)

    def test_config_rejects_more_than_one_repair(self):
        cfg = pathlib.Path(self.tmp.name) / "bad.toml"
        text = BASE_CONFIG.format(state_root=pathlib.Path(self.tmp.name) / "s2", task_store=pathlib.Path(self.tmp.name) / "t2")
        cfg.write_text(text.replace("max_repair_rounds = 1", "max_repair_rounds = 2"))
        with self.assertRaises(lc.ControllerError):
            lc.load_config(str(cfg))


class HappyPathIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        import subprocess, os
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "base.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.base = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        patch = """diff --git a/src/target.txt b/src/target.txt\nnew file mode 100644\nindex 0000000..ce01362\n--- /dev/null\n+++ b/src/target.txt\n@@ -0,0 +1 @@\n+hello\n"""
        self.patch_file = self.root / "patch.txt"
        self.patch_file.write_text(patch)
        self.patch2_file = self.root / "patch2.txt"
        self.patch2_file.write_text(patch.replace("+hello", "+fixed"))
        self.orch_store = self.root / "orch"
        self.jules_store = self.root / "jules"
        self.orch_store.mkdir(); self.jules_store.mkdir()
        self.fake_orch = self.root / "orchestrator"
        self.fake_jules = self.root / "cjules"
        self._write_fake_orchestrator()
        self._write_fake_jules()
        cfg = self.root / "config.toml"
        text = BASE_CONFIG.format(state_root=self.root / "state", task_store=self.orch_store)
        text = text.replace('orchestrator = "orchestrator"', f'orchestrator = "{self.fake_orch}"')
        text = text.replace('command = "cjules"', f'command = "{self.fake_jules}"')
        cfg.write_text(text)
        self.config = lc.load_config(str(cfg))
        os.environ["FAKE_ORCH_STORE"] = str(self.orch_store)
        os.environ["FAKE_JULES_STORE"] = str(self.jules_store)
        os.environ["FAKE_BASE"] = self.base
        os.environ["FAKE_PATCH_FILE"] = str(self.patch_file)
        os.environ["FAKE_PATCH2_FILE"] = str(self.patch2_file)

    def tearDown(self):
        import os
        for k in ["FAKE_ORCH_STORE", "FAKE_JULES_STORE", "FAKE_BASE", "FAKE_PATCH_FILE", "FAKE_PATCH2_FILE", "FAKE_REPAIR"]:
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def _write_fake_orchestrator(self):
        self.fake_orch.write_text(r'''#!/usr/bin/env python3
import json, os, pathlib, sys, uuid
args=sys.argv[1:]
store=pathlib.Path(os.environ['FAKE_ORCH_STORE']); store.mkdir(exist_ok=True)
if args and args[0]=='--version': print('0.1.0'); raise SystemExit(0)
def emit_task(name, runtime, output):
    tid=str(uuid.uuid4()); d=store/tid; d.mkdir();
    task={'id':tid[:8],'taskId':tid,'name':name,'runtime':runtime,'status':'succeeded','active':False,'output':output,'lastMessage':output}
    (d/'task.json').write_text(json.dumps(task)); (d/'result.json').write_text(json.dumps(task)); print(json.dumps({'id':tid[:8],'taskId':tid,'name':name,'runtime':runtime,'status':'running','active':True})); return tid
if args[0]=='launch':
    runtime=args[1]; name=args[args.index('--name')+1]; prompt=args[-1]
    output=''' + repr(lc.DECISION_START) + r'''+'\n'+json.dumps({'action':'PLAN_READY','reason':'ok','goal_contract':{'observable_outcome':'target exists','current_behavior':'missing','expected_behavior':'present','constraints':[],'acceptance_criteria':['target exists'],'non_goals':[],'must_not_change':[],'required_evidence':['test'],'material_unknowns':[]},'execution':{'jules_prompt':'create target','validation_commands':['test -f src/target.txt'],'allowed_paths':['src'],'risk_tags':[],'needs_pre_critic':False,'needs_post_critic':False},'plan_summary':'small'})+'\n'+''' + repr(lc.DECISION_END) + r'''
    emit_task(name,runtime,output)
elif args[0]=='resume':
    name=args[args.index('--name')+1]
    counter=store/'resume-count'; n=int(counter.read_text())+1 if counter.exists() else 1; counter.write_text(str(n))
    if os.environ.get('FAKE_REPAIR')=='1' and n==1:
        decision={'action':'REPAIR','reason':'validation failed','repair_message':'change target content to fixed','evidence_assessment':'bad content'}
    else:
        decision={'action':'PASS','reason':'validated','evidence_assessment':'ok'}
    output=''' + repr(lc.DECISION_START) + r'''+'\n'+json.dumps(decision)+'\n'+''' + repr(lc.DECISION_END) + r'''
    emit_task(name,'codex',output)
elif args[0]=='read':
    tid=args[1]; task=json.loads((store/tid/'task.json').read_text()); print(json.dumps(task))
else:
    print('{}')
''')
        self.fake_orch.chmod(0o755)

    def _write_fake_jules(self):
        self.fake_jules.write_text(r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]; store=pathlib.Path(os.environ['FAKE_JULES_STORE']); store.mkdir(exist_ok=True)
if args and args[0]=='--version': print('0.2.3'); raise SystemExit(0)
sid='s1'; sf=store/'session.json'
if args[0]=='new':
    prompt=sys.stdin.read(); title=args[args.index('--title')+1]
    sess={'id':sid,'state':'COMPLETED','title':title,'prompt':prompt,'updateTime':'2026-01-01T00:00:01Z','sourceContext':{'source':'sources/github/owner/repo','githubRepoContext':{'startingBranch':'main'}}}
    sf.write_text(json.dumps(sess)); print(json.dumps(sess))
elif args[0]=='get': print(sf.read_text())
elif args[0]=='logs':
    sess=json.loads(sf.read_text()); patch=pathlib.Path(os.environ['FAKE_PATCH_FILE']).read_text(); base=os.environ['FAKE_BASE']
    acts=[{'id':'a1','createTime':'2026-01-01T00:00:01Z','agentMessaged':{'agentMessage':'done'},'artifacts':[{'changeSet':{'gitPatch':{'baseCommitId':base,'unidiffPatch':patch}}}]}]
    mf=store/'message.txt'
    if mf.exists():
        msg=mf.read_text(); marker=msg.splitlines()[0]; patch2=pathlib.Path(os.environ['FAKE_PATCH2_FILE']).read_text()
        acts += [
          {'id':'a2','createTime':'2026-01-01T00:00:02Z','userMessaged':{'userMessage':msg}},
          {'id':'a3','createTime':'2026-01-01T00:00:03Z','agentMessaged':{'agentMessage':'fixed '+marker},'artifacts':[{'changeSet':{'gitPatch':{'baseCommitId':base,'unidiffPatch':patch2}}}]},
        ]
    payload={'session':sess,'activities':acts}; print(json.dumps(payload))
elif args[0]=='ls': print(json.dumps([json.loads(sf.read_text())] if sf.exists() else []))
elif args[0]=='msg': store.joinpath('message.txt').write_text(sys.stdin.read()); print('message sent')
elif args[0]=='approve': print('plan approved')
else: print('{}')
''')
        self.fake_jules.chmod(0o755)

    def test_full_repair_path_ignores_stale_completed_and_passes(self):
        import os
        os.environ["FAKE_REPAIR"] = "1"
        ctl = lc.LabeebController.create_goal(
            self.config,
            intent="make target fixed",
            workspace=str(self.repo),
            repo="owner/repo",
            branch="main",
            risk_tags=[],
            allowed_paths=["src"],
            validation_commands=["grep -q fixed src/target.txt"],
            preauthorize_plan=True,
            deadline_hours=1,
        )
        state = None
        for _ in range(30):
            state = ctl.run(once=True)
            if state["phase"] in lc.TERMINAL_PHASES:
                break
        self.assertEqual(state["phase"], "PASS")
        self.assertTrue(state["repair_reserved"])
        result = lc.read_ref_json(state["result_ref"])
        self.assertEqual(result["evidence"]["validation"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()

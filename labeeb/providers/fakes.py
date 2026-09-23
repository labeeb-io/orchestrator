"""Mock and fake provider adapters for tests and UI offline development."""
from __future__ import annotations

import json
from typing import Any, Callable

from labeeb.models import DECISION_START, DECISION_END, CRITIC_START, CRITIC_END, utc_now


class FakeOrchestratorProvider:
    def __init__(self, default_decision: dict[str, Any] | None = None):
        self.tasks: dict[str, dict[str, Any]] = {}
        self.launch_history: list[dict[str, Any]] = []
        self.resume_history: list[dict[str, Any]] = []
        self.default_decision = default_decision or {
            "action": "PLAN_READY",
            "reason": "Simulated plan ready",
            "goal_contract": {
                "observable_outcome": "Outcome achieved in test",
                "current_behavior": "Baseline",
                "expected_behavior": "Target",
                "constraints": ["Keep clean"],
                "acceptance_criteria": ["Tests pass"],
                "non_goals": ["No regressions"],
                "must_not_change": ["Core"],
                "required_evidence": ["Test output"],
                "material_unknowns": [],
            },
            "execution": {
                "jules_prompt": "Simulated jules execution contract",
                "validation_commands": ["true"],
                "allowed_paths": ["src", "tests"],
                "risk_tags": ["architecture"],
                "needs_pre_critic": False,
                "needs_post_critic": False,
            },
            "plan_summary": "Simulated plan summary for UI",
        }

    def launch(self, role_name: str, prompt: str, name: str, workspace: str) -> dict[str, Any]:
        task_id = f"fake-task-{len(self.tasks) + 1}"
        output = f"{DECISION_START}\n{json.dumps(self.default_decision)}\n{DECISION_END}"
        task = {
            "taskId": task_id,
            "id": task_id,
            "name": name,
            "status": "succeeded",
            "active": False,
            "output": output,
            "lastMessage": output,
        }
        self.tasks[task_id] = task
        self.launch_history.append({"role": role_name, "prompt": prompt, "name": name, "workspace": workspace})
        return task

    def resume(self, role_name: str, source_task: str, prompt: str, name: str) -> dict[str, Any]:
        task_id = f"fake-task-{len(self.tasks) + 1}"
        output = f"{DECISION_START}\n{json.dumps(self.default_decision)}\n{DECISION_END}"
        task = {
            "taskId": task_id,
            "id": task_id,
            "name": name,
            "source_task": source_task,
            "status": "succeeded",
            "active": False,
            "output": output,
            "lastMessage": output,
        }
        self.tasks[task_id] = task
        self.resume_history.append({"role": role_name, "source_task": source_task, "prompt": prompt, "name": name})
        return task

    def wait_task(
        self,
        task_id: str,
        stop_checker: Callable[[], bool] | None = None,
        deadline_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        return self.tasks.get(task_id, {"status": "succeeded", "active": False, "output": ""})

    def find_by_name(self, name: str) -> dict[str, Any] | None:
        for t in self.tasks.values():
            if t.get("name") == name:
                return t
        return None


class FakeJulesProvider:
    def __init__(self, initial_state: str = "IN_PROGRESS"):
        self.sessions: dict[str, dict[str, Any]] = {}
        self.logs_data: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.initial_state = initial_state

    def is_repo_available(self, repo: str) -> tuple[bool, list[str]]:
        return True, [repo]

    def create_session(
        self,
        repo: str,
        branch: str,
        marker: str,
        prompt: str,
        require_approval: bool = False,
    ) -> dict[str, Any]:
        sid = f"fake-sess-{len(self.sessions) + 1}"
        session = {
            "id": sid,
            "name": f"sessions/{sid}",
            "title": marker,
            "prompt": prompt,
            "state": self.initial_state,
            "updateTime": utc_now(),
            "sourceContext": {
                "source": f"sources/github/{repo}",
                "githubRepoContext": {"startingBranch": branch},
            },
        }
        self.sessions[sid] = session
        self.logs_data[sid] = {
            "activities": [
                {
                    "id": f"act-{sid}-1",
                    "createTime": utc_now(),
                    "progressUpdated": {"description": "Working on implementation"},
                }
            ]
        }
        return session

    def send_message(self, session_id_val: str, message: str) -> dict[str, Any]:
        self.messages.append({"session_id": session_id_val, "message": message})
        activities = self.logs_data.setdefault(session_id_val, {}).setdefault("activities", [])
        activities.append(
            {
                "id": f"act-{session_id_val}-{len(activities) + 1}",
                "createTime": utc_now(),
                "userMessaged": {"userMessage": message},
            }
        )
        return {"stdout": "OK"}

    def approve_plan(self, session_id_val: str) -> dict[str, Any]:
        if session_id_val in self.sessions:
            self.sessions[session_id_val]["state"] = "IN_PROGRESS"
        return {"stdout": "APPROVED"}

    def get_session(self, session_id_val: str, check: bool = True) -> dict[str, Any] | None:
        return self.sessions.get(session_id_val)

    def get_logs(self, session_id_val: str, check: bool = True) -> dict[str, Any] | None:
        return self.logs_data.get(session_id_val)

    def find_sessions(self, marker: str, repo: str | None = None, branch: str | None = None) -> list[dict[str, Any]]:
        return [s for s in self.sessions.values() if s.get("title") == marker]

    def complete_session(self, session_id_val: str, patch: str = "") -> None:
        if session_id_val in self.sessions:
            self.sessions[session_id_val]["state"] = "COMPLETED"
            self.sessions[session_id_val]["updateTime"] = utc_now()
        activities = self.logs_data.setdefault(session_id_val, {}).setdefault("activities", [])
        if patch:
            activities.append(
                {
                    "id": f"act-{session_id_val}-patch",
                    "createTime": utc_now(),
                    "gitPatch": {"patch": patch},
                }
            )
        activities.append(
            {
                "id": f"act-{session_id_val}-done",
                "createTime": utc_now(),
                "agentMessaged": {"agentMessage": "Implementation completed"},
            }
        )


class FakeCriticProvider:
    def __init__(self, findings: list[dict[str, Any]] | None = None):
        self.findings = findings or []

    def review(self, prompt: str, workspace: str, goal_id: str, op_id: str) -> dict[str, Any]:
        critique = {
            "material_findings": self.findings,
            "notes": "Fake critic completed review",
        }
        output = f"{CRITIC_START}\n{json.dumps(critique)}\n{CRITIC_END}"
        return {"output": output, "command": ["fake-critic"]}

"""Provider adapter for Claude read-only critic."""
from __future__ import annotations

import contextlib
import json
import shutil
from typing import Any

from labeeb.config import Config, expand, role_config
from labeeb.errors import ControllerError
from labeeb.models import task_name
from labeeb.providers.base import run_cmd
from labeeb.providers.orchestrator import OrchestratorProvider


class ClaudeCriticProvider:
    def __init__(self, config: Config, orchestrator_provider: OrchestratorProvider | None = None):
        self.config = config
        self.orchestrator = orchestrator_provider or OrchestratorProvider(config)

    def review(self, prompt: str, workspace: str, goal_id: str, op_id: str) -> dict[str, Any]:
        role_name = str(self.config.get("workflow.critic_role", "critic"))
        role = role_config(self.config, role_name)
        transport = role.get("transport")
        if transport != "direct":
            if transport == "orchestrator":
                name = task_name(goal_id, role_name, op_id)
                task = self.orchestrator.launch(role_name, prompt, name, workspace)
                task_id = str(task.get("taskId") or task.get("id"))
                done = self.orchestrator.wait_task(task_id)
                output = str(done.get("output") or done.get("lastMessage") or "")
                return {"task": done, "output": output}
            raise ControllerError("Critic role transport must be direct or orchestrator")
        command = role.get("command")
        if not isinstance(command, list) or not command:
            raise ControllerError("Direct critic role requires command = [..]")
        cmd = [expand(str(x)) for x in command] + [prompt]
        timeout = float(role.get("timeout_seconds", self.config.get("timeouts.critic_seconds", 900)))
        result = run_cmd(cmd, cwd=workspace, timeout=timeout)
        output = result.stdout.strip()
        parsed: Any = None
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(output)
        if isinstance(parsed, dict):
            output = str(parsed.get("result") or parsed.get("output") or parsed.get("message") or output)
        return {"output": output, "stderr": result.stderr, "command": cmd[:-1] + ["<PROMPT>"]}

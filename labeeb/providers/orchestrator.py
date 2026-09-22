"""Provider adapter for backnotprop/orchestrator CLI."""
from __future__ import annotations

import contextlib
import json
import pathlib
import time
from typing import Any, Callable

from labeeb.config import Config, executable, expand, role_config
from labeeb.errors import AmbiguousEffect, ControllerError
from labeeb.providers.base import parse_json_stdout, run_cmd


class OrchestratorProvider:
    def __init__(self, config: Config):
        self.config = config
        self.orchestrator = executable(config, "orchestrator", "orchestrator")

    def launch(self, role_name: str, prompt: str, name: str, workspace: str) -> dict[str, Any]:
        role = role_config(self.config, role_name)
        if role.get("transport", "orchestrator") != "orchestrator":
            raise ControllerError(f"Role {role_name} is not an orchestrator role")
        runtime = str(role.get("runtime", "codex"))
        cmd = [self.orchestrator, "launch", runtime, "--name", name, "--cwd", workspace]
        model = str(role.get("model", "")).strip()
        if model:
            cmd += ["--model", model]
        cmd += ["--json", "--compact", "--brief", prompt]
        result = run_cmd(cmd, timeout=float(self.config.get("timeouts.launch_seconds", 120)))
        return parse_json_stdout(result)

    def resume(self, role_name: str, source_task: str, prompt: str, name: str) -> dict[str, Any]:
        role = role_config(self.config, role_name)
        runtime = str(role.get("runtime", "codex"))
        cmd = [self.orchestrator, "resume", source_task, "--name", name]
        model = str(role.get("model", "")).strip()
        if model:
            cmd += ["--model", model]
        cmd += ["--json", "--compact", "--brief", prompt]
        result = run_cmd(cmd, timeout=float(self.config.get("timeouts.launch_seconds", 120)))
        payload = parse_json_stdout(result)
        if payload.get("runtime") and payload.get("runtime") != runtime:
            raise ControllerError(f"Resume runtime mismatch: expected {runtime}, got {payload.get('runtime')}")
        return payload

    def wait_task(
        self,
        task_id: str,
        stop_checker: Callable[[], bool] | None = None,
        deadline_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        wait_ms = min(int(self.config.get("timeouts.orchestrator_read_wait_ms", 300000)), 600000)
        poll = float(self.config.get("controller.agent_poll_seconds", 5))
        while True:
            if stop_checker and stop_checker():
                raise ControllerError("Stop requested")
            cmd = [self.orchestrator, "read", task_id, "--wait", "--timeout-ms", str(wait_ms), "--json", "--compact"]
            result = run_cmd(cmd, timeout=(wait_ms / 1000) + 30, check=False)
            if result.stdout.strip():
                try:
                    payload = parse_json_stdout(result)
                except ControllerError:
                    payload = {}
                status = str(payload.get("status") or "")
                active = bool(payload.get("active", False))
                if status in {"succeeded", "failed", "cancelled", "timed_out"} or (status and not active):
                    return payload
            if deadline_checker and deadline_checker():
                raise ControllerError("Goal deadline expired while waiting for agent")
            time.sleep(poll)

    def find_by_name(self, name: str) -> dict[str, Any] | None:
        store = pathlib.Path(expand(str(self.config.get("orchestrator.task_store", "~/.orchestrator/tasks"))))
        if not store.exists():
            return None
        matches: list[dict[str, Any]] = []
        for task_json in store.glob("*/task.json"):
            with contextlib.suppress(Exception):
                payload = json.loads(task_json.read_text())
                if payload.get("name") == name:
                    matches.append(payload)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousEffect(f"Multiple Orchestrator tasks found with name {name}")
        return None

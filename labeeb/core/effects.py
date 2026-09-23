"""Orchestration side-effect lifecycle (PREPARED -> IN_FLIGHT -> ACKNOWLEDGED) and reconciliation."""
from __future__ import annotations

import time
from typing import Any, Callable

from labeeb.config import Config
from labeeb.core.events import append_domain_event, global_event_bus
from labeeb.errors import AmbiguousEffect, CommandError, ControllerError
from labeeb.models import DomainEvent, new_operation_id, utc_now
from labeeb.providers.jules import JulesProvider, has_user_message_marker, session_id
from labeeb.providers.orchestrator import OrchestratorProvider
from labeeb.storage.goal_store import GoalPaths, GoalStore, read_ref_json


class EffectManager:
    def __init__(
        self,
        config: Config,
        paths: GoalPaths,
        store: GoalStore,
        orchestrator: OrchestratorProvider,
        jules: JulesProvider,
    ):
        self.config = config
        self.paths = paths
        self.store = store
        self.orchestrator = orchestrator
        self.jules = jules
        self.jules_get_fn: Callable[..., Any] | None = None
        self.jules_logs_fn: Callable[..., Any] | None = None

    def prepare_effect(self, state: dict[str, Any], kind: str, payload: dict[str, Any], next_phase: str) -> dict[str, Any]:
        op_id = new_operation_id(kind)
        ref = self.store.write_json(self.paths.requests / f"{op_id}.json", payload)
        action = {
            "id": op_id,
            "kind": kind,
            "stage": "PREPARED",
            "payload_ref": ref,
            "next_phase": next_phase,
            "created_at": utc_now(),
        }
        state["pending_action"] = action
        state["phase"] = "EFFECT"
        self.store.save(state)
        return action

    def mark_in_flight(self, state: dict[str, Any]) -> dict[str, Any]:
        action = state.get("pending_action")
        if not isinstance(action, dict):
            raise ControllerError("No pending action")
        action["stage"] = "IN_FLIGHT"
        action["started_at"] = utc_now()
        state["pending_action"] = action
        self.store.save(state)
        return action

    def complete_effect(self, state: dict[str, Any], result: dict[str, Any], *, next_phase: str | None = None) -> None:
        action = state.get("pending_action")
        if not isinstance(action, dict):
            raise ControllerError("No pending action to complete")
        action["stage"] = "ACKNOWLEDGED"
        action["completed_at"] = utc_now()
        result_ref = self.store.write_json(self.paths.requests / f"{action['id']}.result.json", result)
        action["result_ref"] = result_ref
        target_phase = next_phase or action["next_phase"]
        state["pending_action"] = None
        state.pop("blocked_action", None)
        state["phase"] = target_phase
        self.store.save(state)

        kind = action.get("kind")
        if kind == "jules_create":
            sid = state.get("jules_session_id")
            ev = DomainEvent(
                event_type="jules.dispatched",
                goal_id=state.get("goal_id", ""),
                timestamp=utc_now(),
                data={"session_id": sid, "result": result},
            )
            append_domain_event(self.paths.events, ev)
            global_event_bus.publish_sync(ev)
        elif kind == "jules_message":
            payload = read_ref_json(action["payload_ref"]) if action.get("payload_ref") else {}
            marker = str(payload.get("marker") or "")
            if marker.startswith("[LABEEB-REPAIR:"):
                ev = DomainEvent(
                    event_type="repair.dispatched",
                    goal_id=state.get("goal_id", ""),
                    timestamp=utc_now(),
                    data={"session_id": payload.get("session_id"), "marker": marker},
                )
                append_domain_event(self.paths.events, ev)
                global_event_bus.publish_sync(ev)

    def execute_pending_effect(
        self,
        state: dict[str, Any],
        on_block: Callable[[str, Any], None],
    ) -> None:
        action = state.get("pending_action")
        if not isinstance(action, dict):
            raise ControllerError("EFFECT phase without pending_action")
        if action.get("stage") == "IN_FLIGHT":
            self.reconcile_effect(state, action, on_block=on_block)
            return
        if action.get("stage") != "PREPARED":
            raise ControllerError(f"Unknown effect stage: {action.get('stage')}")
        kind = action["kind"]
        payload = read_ref_json(action["payload_ref"])
        self.mark_in_flight(state)
        try:
            if kind == "brain_launch":
                result = self._effect_brain_launch(state, action, payload)
            elif kind == "brain_resume":
                result = self._effect_brain_resume(state, action, payload)
            elif kind == "jules_create":
                result = self._effect_jules_create(state, action, payload)
            elif kind == "jules_message":
                result = self._effect_jules_message(state, action, payload)
            elif kind == "jules_approve":
                result = self._effect_jules_approve(state, action, payload)
            else:
                raise ControllerError(f"Unsupported effect kind: {kind}")
        except CommandError as exc:
            if kind in {"jules_create", "jules_message", "jules_approve", "brain_launch", "brain_resume"}:
                err_detail = f"{exc}"
                if getattr(exc, "stderr", None) and str(exc.stderr).strip():
                    err_detail += f" | stderr: {str(exc.stderr).strip()}"
                if getattr(exc, "stdout", None) and str(exc.stdout).strip():
                    err_detail += f" | stdout: {str(exc.stdout).strip()}"
                self.store.append_log(f"effect command failed; reconciling {kind}: {err_detail}")
                self.reconcile_effect(state, state["pending_action"], command_error=exc, on_block=on_block)
                return
            raise
        self.complete_effect(state, result)

    def reconcile_effect(
        self,
        state: dict[str, Any],
        action: dict[str, Any],
        command_error: Exception | None = None,
        on_block: Callable[[str, Any], None] | None = None,
    ) -> None:
        def block(reason: str, evidence: Any = None):
            if on_block:
                on_block(reason, evidence)
            else:
                state["phase"] = "BLOCKED"
                self.store.save(state)

        kind = action["kind"]
        payload = read_ref_json(action["payload_ref"])
        if kind in {"brain_launch", "brain_resume"}:
            attempts = int(self.config.get("controller.reconcile_attempts", 3))
            delay = float(self.config.get("controller.reconcile_delay_seconds", 5))
            task = None
            for i in range(attempts):
                try:
                    task = self.orchestrator.find_by_name(payload["task_name"])
                    if task is not None:
                        break
                except AmbiguousEffect as exc:
                    block(f"Ambiguous {kind}: multiple tasks found with name {payload['task_name']}", evidence=str(exc))
                    return
                if i + 1 < attempts:
                    time.sleep(delay)
            if task is None:
                block(f"Ambiguous {kind}: task creation cannot be proven after {attempts} attempts", evidence=str(command_error or ""))
                return
            task_id = str(task.get("taskId") or task.get("id"))
            state["latest_codex_task_id"] = task_id
            state["active_task"] = {"role": payload.get("role", "brain"), "task_id": task_id, "operation_id": action["id"]}
            self.complete_effect(state, {"adopted": True, "task_id": task_id})
            return
        if kind == "jules_create":
            if not payload.get("repo") or not payload.get("branch") or not payload.get("marker"):
                block(
                    "Ambiguous Jules create: effect payload lacks required repo, branch, or marker metadata",
                    evidence=payload,
                )
                return
            matches = self.jules.find_sessions(payload["marker"], payload.get("repo"), payload.get("branch"))
            if len(matches) == 1:
                sid = session_id(matches[0])
                state["jules_session_id"] = sid
                if sid not in state.setdefault("jules_session_history", []):
                    state["jules_session_history"].append(sid)
                self.complete_effect(state, {"adopted": True, "session_id": sid})
                return
            if len(matches) > 1:
                block(
                    f"Ambiguous Jules create: expected at most one matching session, found {len(matches)}",
                    evidence={"marker": payload["marker"], "matches": [session_id(x) for x in matches]},
                )
                return
            # len(matches) == 0:
            is_avail, sources = self.jules.is_repo_available(payload.get("repo", ""))
            if not is_avail:
                sources_str = ", ".join(sources) if sources else "None discovered"
                block(
                    f"Jules create failed: repository '{payload.get('repo')}' is not authorized in Google Jules",
                    evidence={
                        "marker": payload["marker"],
                        "repo": payload.get("repo"),
                        "authorized_sources": sources,
                        "instruction": f"Please authorize '{payload.get('repo')}' at https://jules.google.com/ or select an authorized source ({sources_str}).",
                    },
                )
                return
            if command_error:
                err_msg = str(command_error)
                stderr_str = str(getattr(command_error, "stderr", "") or "").strip()
                stdout_str = str(getattr(command_error, "stdout", "") or "").strip()
                block(
                    f"Jules create failed: {err_msg}",
                    evidence={
                        "marker": payload["marker"],
                        "command_error": err_msg,
                        "stderr": stderr_str,
                        "stdout": stdout_str,
                    },
                )
                return
            block(
                "Ambiguous Jules create: expected exactly one matching session, found 0",
                evidence={"marker": payload["marker"], "matches": []},
            )
            return
        if kind == "jules_message":
            attempts = int(self.config.get("controller.reconcile_attempts", 3))
            delay = float(self.config.get("controller.reconcile_delay_seconds", 5))
            for i in range(attempts):
                logs = self.jules_logs_fn(payload["session_id"], check=False) if self.jules_logs_fn else self.jules.get_logs(payload["session_id"], check=False)
                if logs and has_user_message_marker(logs.get("activities") or [], payload["marker"]):
                    self.complete_effect(state, {"adopted": True, "marker": payload["marker"]})
                    return
                if i + 1 < attempts:
                    time.sleep(delay)
            if command_error:
                err_msg = str(command_error)
                block(
                    f"Jules message failed: {err_msg}",
                    evidence={
                        "marker": payload["marker"],
                        "command_error": err_msg,
                        "stderr": str(getattr(command_error, "stderr", "") or "").strip(),
                        "stdout": str(getattr(command_error, "stdout", "") or "").strip(),
                    },
                )
                return
            block("Ambiguous Jules message delivery; refusing duplicate send", evidence={"marker": payload["marker"]})
            return
        if kind == "jules_approve":
            attempts = int(self.config.get("controller.reconcile_attempts", 3))
            delay = float(self.config.get("controller.reconcile_delay_seconds", 5))
            for i in range(attempts):
                session = self.jules_get_fn(payload["session_id"], check=False) if self.jules_get_fn else self.jules.get_session(payload["session_id"], check=False)
                logs = self.jules_logs_fn(payload["session_id"], check=False) if self.jules_logs_fn else self.jules.get_logs(payload["session_id"], check=False)
                activities = (logs.get("activities") or []) if logs else []
                has_plan_approved = any(isinstance(a.get("planApproved"), dict) for a in activities)
                if has_plan_approved:
                    self.complete_effect(state, {"adopted": True, "state": session.get("state") if session else None})
                    return
                session_state = str(session.get("state") or "") if session else ""
                if session_state in {"FAILED", "CANCELLED"} and not has_plan_approved:
                    block("Jules session reached terminal state without proven plan approval", evidence={"session_state": session_state})
                    return
                if i + 1 < attempts:
                    time.sleep(delay)
            if command_error:
                err_msg = str(command_error)
                block(
                    f"Jules approve failed: {err_msg}",
                    evidence={
                        "session_id": payload["session_id"],
                        "command_error": err_msg,
                        "stderr": str(getattr(command_error, "stderr", "") or "").strip(),
                        "stdout": str(getattr(command_error, "stdout", "") or "").strip(),
                    },
                )
                return
            block("Ambiguous Jules plan approval; refusing duplicate approval without proven planApproved activity")
            return
        block(f"Cannot reconcile effect kind: {kind}")

    def _effect_brain_launch(self, state: dict[str, Any], action: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        task = self.orchestrator.launch(payload["role"], payload["prompt"], payload["task_name"], payload["workspace"])
        task_id = str(task.get("taskId") or task.get("id"))
        if not task_id:
            raise ControllerError("Orchestrator launch returned no task id")
        state["latest_codex_task_id"] = task_id
        state["brain_resume_count"] = 0
        state["active_task"] = {"role": payload["role"], "task_id": task_id, "operation_id": action["id"]}
        self.store.save(state)
        return {"task_id": task_id, "launch": task}

    def _effect_brain_resume(self, state: dict[str, Any], action: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        source_task = payload["source_task_id"]
        task = self.orchestrator.resume(payload["role"], source_task, payload["prompt"], payload["task_name"])
        task_id = str(task.get("taskId") or task.get("id"))
        if not task_id:
            raise ControllerError("Orchestrator resume returned no task id")
        state["latest_codex_task_id"] = task_id
        state["brain_resume_count"] = int(state.get("brain_resume_count", 0)) + 1
        state["active_task"] = {"role": payload["role"], "task_id": task_id, "operation_id": action["id"]}
        self.store.save(state)
        return {"task_id": task_id, "resume": task}

    def _effect_jules_create(self, state: dict[str, Any], action: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        session = self.jules.create_session(
            payload["repo"],
            payload["branch"],
            payload["marker"],
            payload["prompt"],
            payload.get("require_approval", False),
        )
        sid = session_id(session)
        state["jules_session_id"] = sid
        if sid not in state.setdefault("jules_session_history", []):
            state["jules_session_history"].append(sid)
        self.store.save(state)
        return {"session_id": sid, "session": session}

    def _effect_jules_message(self, state: dict[str, Any], action: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        res = self.jules.send_message(payload["session_id"], payload["message"])
        return {"stdout": res.get("stdout", ""), "marker": payload["marker"]}

    def _effect_jules_approve(self, state: dict[str, Any], action: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        res = self.jules.approve_plan(payload["session_id"])
        return {"stdout": res.get("stdout", "")}

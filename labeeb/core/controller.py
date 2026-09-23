"""Main LabeebController application service."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid
from typing import Any

from labeeb.config import Config, critic_needed, expand, risk_matches, role_config
from labeeb.core import decisions, repair
from labeeb.core.artifacts import GoalArtifactStore
from labeeb.core.effects import EffectManager
from labeeb.core.events import (
    append_domain_event,
    global_event_bus,
    jules_snapshot,
    meaningful_event,
    reserve_event_key,
)
from labeeb.core.state_machine import initial_brain_prompt
from labeeb.core.recovery import (
    RETRYABLE_BRAIN_PATTERNS,
    format_brain_failure_reason,
    is_retryable_brain_failure,
    retry_brain_with_fresh_thread,
    unblock_goal,
)
from labeeb.core.validation import prepare_review_evidence, validate_evidence
from labeeb.errors import ControllerError, JulesSourceUnauthorizedError
from labeeb.models import (
    DECISION_END,
    DECISION_START,
    TERMINAL_PHASES,
    VERSION,
    ArtifactStatus,
    DomainEvent,
    deadline_after,
    new_operation_id,
    parse_utc,
    task_name,
    utc_now,
)
from labeeb.providers.base import extract_enveloped_json
from labeeb.providers.claude import ClaudeCriticProvider
from labeeb.providers.git import GitProvider
from labeeb.providers.jules import JulesProvider, ordered_activities
from labeeb.providers.orchestrator import OrchestratorProvider
from labeeb.storage.goal_store import (
    GoalPaths,
    GoalStore,
    atomic_text_write,
    read_ref_json,
)


class LabeebController:
    def __init__(
        self,
        config: Config,
        goal_id: str,
        orchestrator_provider: OrchestratorProvider | None = None,
        jules_provider: JulesProvider | None = None,
        critic_provider: ClaudeCriticProvider | None = None,
        git_provider: GitProvider | None = None,
    ):
        self.config = config
        self.goal_id = goal_id
        root = pathlib.Path(expand(str(config.get("controller.state_root", "~/.local/state/labeeb-controller"))))
        self.paths = GoalPaths(root / "goals" / goal_id)
        self.store = GoalStore(self.paths)

        self.orchestrator = orchestrator_provider or OrchestratorProvider(config)
        self.jules = jules_provider or JulesProvider(config)
        self.critic = critic_provider or ClaudeCriticProvider(config, self.orchestrator)
        self.git = git_provider or GitProvider(config)
        self.effects = EffectManager(config, self.paths, self.store, self.orchestrator, self.jules)
        self.effects.jules_get_fn = lambda sid, check=False: self.jules_get(sid, check=check)
        self.effects.jules_logs_fn = lambda sid, check=False: self.jules_logs(sid, check=check)
        self.artifact_store = GoalArtifactStore(self.paths, self.store)
        from labeeb.core.reporting import FinalReportGenerator
        self.report_generator = FinalReportGenerator(self)

    # ----------------------------- facade & backward compatibility -----------------------------
    def jules_get(self, session_id: str, check: bool = True) -> dict[str, Any] | None:
        return self.jules.get_session(session_id, check=check)

    def jules_logs(self, session_id: str, check: bool = True) -> dict[str, Any] | None:
        return self.jules.get_logs(session_id, check=check)

    def prepare_effect(self, state: dict[str, Any], kind: str, payload: dict[str, Any], next_phase: str) -> dict[str, Any]:
        return self.effects.prepare_effect(state, kind, payload, next_phase)

    def reconcile_effect(
        self,
        state: dict[str, Any],
        action: dict[str, Any],
        command_error: Exception | None = None,
    ) -> None:
        self.effects.reconcile_effect(
            state,
            action,
            command_error=command_error,
            on_block=lambda r, ev: self.block(state, r, evidence=ev),
        )

    def reconcile_pending_or_blocked(self) -> dict[str, Any]:
        with self.store.locked():
            state = self.store.load()
            action = state.get("blocked_action") or state.get("pending_action")
            if not isinstance(action, dict):
                raise ControllerError("No pending or ambiguous action to reconcile")
            self.reconcile_effect(state, action)
            return self.store.load()

    def meaningful_event(
        self,
        state: dict[str, Any],
        session: dict[str, Any],
        logs: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        return meaningful_event(state, session, logs, self.config)

    def jules_snapshot(self, logs: dict[str, Any]) -> dict[str, Any]:
        from labeeb.providers.jules import jules_snapshot
        return jules_snapshot(logs)

    # ----------------------------- factory/lifecycle -----------------------------
    @classmethod
    def create_goal(
        cls,
        config: Config,
        *,
        intent: str,
        workspace: str,
        repo: str,
        branch: str,
        risk_tags: list[str],
        allowed_paths: list[str],
        validation_commands: list[str],
        preauthorize_plan: bool,
        deadline_hours: float | None = None,
        force: bool = False,
        orchestrator_provider: OrchestratorProvider | None = None,
        jules_provider: JulesProvider | None = None,
        critic_provider: ClaudeCriticProvider | None = None,
        git_provider: GitProvider | None = None,
    ) -> "LabeebController":
        implementer_role = str(config.get("workflow.implementer_role", "implementer"))
        implementer_transport = role_config(config, implementer_role).get("transport", "jules")
        check_sources = bool(config.get("safety.check_jules_sources", True))
        if repo in {"owner/repo", "test/repo", "dummy/repo"}:
            check_sources = False
        if implementer_transport == "jules" and check_sources and not force:
            jp = jules_provider or JulesProvider(config)
            if hasattr(jp, "is_repo_available"):
                is_avail, sources = jp.is_repo_available(repo)
                if not is_avail:
                    raise JulesSourceUnauthorizedError(repo, sources)

        goal_id = str(uuid.uuid4())
        ctl = cls(
            config,
            goal_id,
            orchestrator_provider=orchestrator_provider,
            jules_provider=jules_provider,
            critic_provider=critic_provider,
            git_provider=git_provider,
        )
        ctl.store.init_dirs()
        contract_seed = {
            "schema_version": 1,
            "goal_id": goal_id,
            "intent": intent,
            "workspace": str(pathlib.Path(expand(workspace)).resolve()),
            "repo": repo,
            "branch": branch,
            "risk_tags": risk_tags,
            "allowed_paths": allowed_paths,
            "validation_commands": validation_commands,
            "authority": {
                "preauthorize_bounded_plan": preauthorize_plan,
                "allow_workspace_edits": True,
                "allow_push": False,
                "allow_pr": False,
                "allow_merge": False,
                "allow_production_mutation": False,
            },
            "created_at": utc_now(),
        }
        contract_ref = ctl.store.write_json(ctl.paths.contract, contract_seed)
        hours = deadline_hours if deadline_hours is not None else float(config.get("controller.goal_deadline_hours", 12))
        state = {
            "version": 1,
            "goal_id": goal_id,
            "contract_ref": contract_ref,
            "plan_ref": None,
            "phase": "CREATED",
            "macro_phase": "THINKING",
            "current_activity": "authority_context",
            "artifacts": {},
            "execution_rounds": 0,
            "materialization_corrections": 0,
            "materialization_verified": False,
            "proof_path_locked": False,
            "path_integrity_status": "ORIGINAL",
            "reasoning_history": [],
            "deadline": deadline_after(hours),
            "latest_codex_task_id": None,
            "active_task": None,
            "jules_session_id": None,
            "jules_session_history": [],
            "repair_reserved": False,
            "repair_request_ref": None,
            "round_anchor_ref": None,
            "pre_critic_done": False,
            "brain_resume_count": 0,
            "brain_thread_generation": 1,
            "brain_transient_retries": 0,
            "handled_event_keys": [],
            "jules_read_failures": 0,
            "pending_action": None,
            "review_ref": None,
            "result_ref": None,
            "controller_pid": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        ctl.store.save(state)
        ctl.store.append_log("goal created")
        ctl.record_event("goal.created", {"phase": "CREATED"})
        return ctl

    @staticmethod
    def list_goals(config: Config) -> list[dict[str, Any]]:
        root = pathlib.Path(expand(str(config.get("controller.state_root", "~/.local/state/labeeb-controller"))))
        goals_dir = root / "goals"
        if not goals_dir.exists():
            return []
        items = []
        for g_dir in sorted(goals_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not g_dir.is_dir():
                continue
            state_file = g_dir / "state.json"
            if not state_file.exists():
                continue
            try:
                st = json.loads(state_file.read_text())
                contract = {}
                contract_file = g_dir / "contract.json"
                if contract_file.exists():
                    with contextlib.suppress(Exception):
                        contract = json.loads(contract_file.read_text())
                result = None
                result_file = g_dir / "result.json"
                if result_file.exists():
                    with contextlib.suppress(Exception):
                        result = json.loads(result_file.read_text())
                items.append(
                    {
                        "goal_id": st.get("goal_id", g_dir.name),
                        "phase": st.get("phase"),
                        "intent": contract.get("intent", ""),
                        "repo": contract.get("repo", ""),
                        "branch": contract.get("branch", ""),
                        "risk_tags": contract.get("risk_tags", []),
                        "created_at": st.get("created_at"),
                        "updated_at": st.get("updated_at"),
                        "deadline": st.get("deadline"),
                        "jules_session_id": st.get("jules_session_id"),
                        "latest_codex_task_id": st.get("latest_codex_task_id"),
                        "repair_reserved": st.get("repair_reserved", False),
                        "result": result,
                    }
                )
            except Exception:
                continue
        return items

    def deadline_expired(self, state: dict[str, Any]) -> bool:
        return dt.datetime.now(dt.timezone.utc) >= parse_utc(state["deadline"])

    def record_event(self, event_type: str, data: dict[str, Any] | None = None) -> DomainEvent:
        event = DomainEvent(
            event_type=event_type,
            goal_id=self.goal_id,
            timestamp=utc_now(),
            data=data or {},
        )
        append_domain_event(self.paths.events, event)
        global_event_bus.publish_sync(event)
        return event

    def clear_stop_request(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.paths.stop_request.unlink()

    def read_events(self) -> list[dict[str, Any]]:
        return self.store.read_events()

    def _write_emergency_report(
        self,
        state: dict[str, Any],
        status: str,
        *,
        reason: str = "",
        evidence: Any = None,
        error: str = "",
    ) -> None:
        """Write minimal emergency fallback report to guarantee terminal persistence."""
        minimal_report = {
            "goal_id": self.goal_id,
            "status": status,
            "reason": reason,
            "evidence": evidence,
            "generated_at": utc_now(),
            "emergency_fallback": True,
            "generator_error": error,
        }
        state["final_report_ref"] = self.store.write_json(self.paths.final_report_json, minimal_report)
        md_text = f"# Final Goal Report: {self.goal_id}\n\n**Status**: {status}\n\n**Reason**: {reason}\n\n> Note: Generated via emergency fallback due to report generator error: {error}\n"
        state["final_report_md_ref"] = self.store.write_text(self.paths.final_report_md, md_text)
        self.store.append_log(f"EMERGENCY REPORT: Generated minimal fallback report for {status}")

    def _require_durable_report(
        self,
        state: dict[str, Any],
        status: str,
        *,
        reason: str = "",
        evidence: Any = None,
    ) -> None:
        """Enforce that both final_report.json and final_report.md are durably saved before terminal persistence.

        Safe failure path: If report writing fails, records an error log and domain event, but does NOT
        commit result.json or transition to a terminal phase, and avoids recursive block/fail calls.
        """
        state["macro_phase"] = "REPORTING"
        try:
            self.report_generator.generate_and_save(state, status, reason=reason, evidence=evidence)
            if not self.paths.final_report_json.exists() or not self.paths.final_report_md.exists():
                raise ControllerError("Final report files missing from disk after generation")
            if not state.get("final_report_ref") or not state.get("final_report_md_ref"):
                raise ControllerError("Final report references missing in state")
        except Exception as exc:
            retry_cnt = int((state.get("pending_terminal") or {}).get("report_retry_count", 0))
            if retry_cnt >= 1:
                self._write_emergency_report(state, status, reason=reason, evidence=evidence, error=str(exc))
                return
            if isinstance(state.get("pending_terminal"), dict):
                state["pending_terminal"]["report_retry_count"] = retry_cnt + 1
            self.store.append_log(f"CRITICAL: Final report generation failed ({status}): {exc}")
            self.record_event(
                "reporting.failed",
                {
                    "target_status": status,
                    "reason": reason,
                    "error": str(exc),
                },
            )
            self.store.save(state)
            raise ControllerError(f"Durable report generation failed for terminal status '{status}': {exc}") from exc

    def block(self, state: dict[str, Any], reason: str, *, evidence: Any = None) -> None:
        if not state.get("pending_terminal"):
            state["pending_terminal"] = {
                "status": "BLOCKED",
                "reason": reason,
                "evidence": evidence,
                "at": utc_now(),
            }
            self.store.save(state)
        self._require_durable_report(state, "BLOCKED", reason=reason, evidence=evidence)
        result = {
            "status": "BLOCKED",
            "reason": reason,
            "evidence": evidence,
            "at": utc_now(),
            "final_report_ref": state.get("final_report_ref"),
            "final_report_md_ref": state.get("final_report_md_ref"),
        }
        state["result_ref"] = self.store.write_json(self.paths.results, result)
        state["phase"] = "BLOCKED"
        state["blocked_reason"] = reason
        state["active_task"] = None
        state.pop("pending_terminal", None)
        pending = state.get("pending_action")
        if isinstance(pending, dict) and pending.get("stage") in {"IN_FLIGHT", "PREPARED", "AMBIGUOUS"}:
            pending["stage"] = "AMBIGUOUS"
            state["blocked_action"] = dict(pending)
            state["pending_action"] = pending
        else:
            state["pending_action"] = None
        self.store.save(state)
        self.store.append_log(f"BLOCKED: {reason}")
        self.record_event("goal.blocked", {"reason": reason})

    def fail(self, state: dict[str, Any], reason: str, *, evidence: Any = None) -> None:
        if not state.get("pending_terminal"):
            state["pending_terminal"] = {
                "status": "FAIL",
                "reason": reason,
                "evidence": evidence,
                "at": utc_now(),
            }
            self.store.save(state)
        self._require_durable_report(state, "FAIL", reason=reason, evidence=evidence)
        result = {
            "status": "FAIL",
            "reason": reason,
            "evidence": evidence,
            "at": utc_now(),
            "final_report_ref": state.get("final_report_ref"),
            "final_report_md_ref": state.get("final_report_md_ref"),
        }
        state["result_ref"] = self.store.write_json(self.paths.results, result)
        state["phase"] = "FAIL"
        state["active_task"] = None
        state["pending_action"] = None
        state.pop("pending_terminal", None)
        self.store.save(state)
        self.store.append_log(f"FAIL: {reason}")
        self.record_event("goal.failed", {"reason": reason})

    def pass_goal(self, state: dict[str, Any], decision: dict[str, Any], evidence: dict[str, Any]) -> None:
        reason = str(decision.get("reason") or "Goal acceptance criteria verified")
        if not state.get("pending_terminal"):
            state["pending_terminal"] = {
                "status": "PASS",
                "reason": reason,
                "decision": decision,
                "evidence": evidence,
                "at": utc_now(),
            }
            self.store.save(state)
        self._require_durable_report(state, "PASS", reason=reason, evidence=evidence)
        result = {
            "status": "PASS",
            "reason": reason,
            "decision": decision,
            "evidence": evidence,
            "at": utc_now(),
            "final_report_ref": state.get("final_report_ref"),
            "final_report_md_ref": state.get("final_report_md_ref"),
        }
        state["result_ref"] = self.store.write_json(self.paths.results, result)
        state["phase"] = "PASS"
        state["active_task"] = None
        state["pending_action"] = None
        state.pop("pending_terminal", None)
        self.store.save(state)
        self.store.append_log("PASS")
        self.record_event("goal.passed", {"decision": decision})

    # ----------------------------- brain/critic operations -----------------------------
    def handle_brain_completion(self, state: dict[str, Any]) -> None:
        if state.get("pending_terminal"):
            pt = state["pending_terminal"]
            st = pt.get("status")
            if st == "PASS":
                self.pass_goal(state, pt.get("decision") or {"reason": pt.get("reason")}, pt.get("evidence") or {})
                return
            elif st == "FAIL":
                self.fail(state, str(pt.get("reason") or "Terminal failure"), evidence=pt.get("evidence"))
                return
            elif st == "BLOCKED":
                self.block(state, str(pt.get("reason") or "Terminal block"), evidence=pt.get("evidence"))
                return
        active = state.get("active_task")
        if not isinstance(active, dict) or not active.get("task_id"):
            self.block(state, "THINKING/REVIEWING has no active brain task")
            return
        done = self.orchestrator.wait_task(
            active["task_id"],
            stop_checker=self.stop_requested,
            deadline_checker=lambda: self.deadline_expired(self.store.load()),
        )
        state["active_task"] = None
        self.store.save(state)
        if done.get("status") != "succeeded":
            if self._is_retryable_brain_failure(done):
                retried = self._retry_brain_with_fresh_thread(state, done)
                if retried:
                    return  # New brain task launched; step loop will pick it up
            self.block(state, self._format_brain_failure_reason(done), evidence=done)
            return
        state["brain_transient_retries"] = 0
        output = str(done.get("output") or done.get("lastMessage") or "").strip()
        try:
            decision = extract_enveloped_json(output, DECISION_START, DECISION_END)
        except ControllerError as exc:
            self.block(state, f"Brain returned invalid structured decision: {exc}", evidence={"output": output[:4000]})
            return
        phase = state["phase"]
        if phase == "THINKING":
            self.handle_plan_decision(state, decision)
        elif phase == "REVIEWING":
            self.handle_review_decision(state, decision)
        else:
            self.block(state, f"Unexpected brain completion in phase {phase}")

    def handle_plan_decision(self, state: dict[str, Any], decision: dict[str, Any]) -> None:
        decisions.handle_plan_decision(self, state, decision)

    def perform_critic(
        self,
        when: str,
        contract: dict[str, Any],
        plan: dict[str, Any],
        evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return decisions.perform_critic(self, when, contract, plan, evidence)

    def approve_plan_if_authorized(self, state: dict[str, Any], *, manual_approval: bool = False) -> None:
        decisions.approve_plan_if_authorized(self, state, manual_approval=manual_approval)

    def dispatch_jules(self, state: dict[str, Any]) -> None:
        decisions.dispatch_jules(self, state)

    def complete_baseline_shortcut(self, state: dict[str, Any], decision: dict[str, Any]) -> None:
        """Finish a goal already proven by its original baseline without dispatching Jules."""
        evidence = {
            "status": "PASS",
            "terminal_path": "BASELINE_SHORTCUT",
            "baseline_artifact": (state.get("artifacts") or {}).get("baseline_result"),
            "reason": decision.get("reason"),
        }
        state["macro_phase"] = "REPORTING"
        state["baseline_shortcut"] = True
        self.record_event("baseline.shortcut_pass", evidence)
        if state.get("reasoning_graph_active"):
            from labeeb.models import PathIntegrityStatus
            with contextlib.suppress(Exception):
                self.artifact_store.write_artifact(
                    state=state,
                    artifact_type="goal_proof",
                    data={
                        "proof_entrypoint": (state.get("artifacts") or {}).get("proof_contract"),
                        "path_integrity_status": PathIntegrityStatus.ORIGINAL,
                        "proof_passed": True,
                        "validation_status": "PASS",
                        "terminal_path": "BASELINE_SHORTCUT",
                        "execution_rounds": 0,
                    },
                    producer="controller",
                    allow_stale_dependency=True,
                )
        self.pass_goal(state, decision, evidence)

    def handle_review_decision(self, state: dict[str, Any], decision: dict[str, Any]) -> None:
        decisions.handle_review_decision(self, state, decision)

    def wake_brain_for_event(self, state: dict[str, Any], event: dict[str, Any], evidence: dict[str, Any]) -> None:
        decisions.wake_brain_for_event(self, state, event, evidence)

    # ----------------------------- transient brain failure retry & recovery -----------------------------
    _RETRYABLE_PATTERNS = RETRYABLE_BRAIN_PATTERNS

    def _is_retryable_brain_failure(self, done: dict[str, Any]) -> bool:
        """Return True if the brain failure looks transient and worth retrying."""
        return is_retryable_brain_failure(done)

    def _retry_brain_with_fresh_thread(
        self, state: dict[str, Any], done: dict[str, Any]
    ) -> bool:
        """Attempt to launch a fresh brain thread after a transient failure."""
        return retry_brain_with_fresh_thread(self, state, done)

    @staticmethod
    def _format_brain_failure_reason(done: dict[str, Any]) -> str:
        """Extract a human-readable failure reason from the brain task result."""
        return format_brain_failure_reason(done)

    def unblock_and_retry(self, *, background: bool = False) -> dict[str, Any]:
        """Reset a BLOCKED goal back to its pre-block phase and re-launch brain."""
        return unblock_goal(self, background=background)

    def reserve_and_send_repair(self, state: dict[str, Any], message: str) -> None:
        repair.reserve_and_send_repair(self, state, message)

    def maybe_handle_repair_activation_timeout(
        self, state: dict[str, Any], session: dict[str, Any], logs: dict[str, Any]
    ) -> bool:
        return repair.maybe_handle_repair_activation_timeout(self, state, session, logs)

    def dispatch_repair_fallback_session(self, state: dict[str, Any]) -> None:
        repair.dispatch_repair_fallback_session(self, state)

    # ----------------------------- main state machine step -----------------------------
    def step(self, state: dict[str, Any]) -> bool:
        phase = state["phase"]
        if phase in TERMINAL_PHASES:
            return False
        if state.get("pending_terminal"):
            pt = state["pending_terminal"]
            st = pt.get("status")
            if st == "PASS":
                self.pass_goal(state, pt.get("decision") or {"reason": pt.get("reason")}, pt.get("evidence") or {})
                return True
            elif st == "FAIL":
                self.fail(state, str(pt.get("reason") or "Terminal failure"), evidence=pt.get("evidence"))
                return True
            elif st == "BLOCKED":
                self.block(state, str(pt.get("reason") or "Terminal block"), evidence=pt.get("evidence"))
                return True
        if self.stop_requested():
            self.block(state, "Manual stop requested")
            return True
        if self.deadline_expired(state):
            self.block(state, "Goal deadline expired")
            return True
        if phase == "EFFECT":
            self.effects.execute_pending_effect(state, on_block=lambda r, ev: self.block(state, r, evidence=ev))
            return True
        if phase == "CREATED":
            contract = read_ref_json(state["contract_ref"])
            prompt = initial_brain_prompt(contract, self.config)
            op_id = new_operation_id("brain-plan")
            self.prepare_effect(
                state,
                "brain_launch",
                {
                    "role": str(self.config.get("workflow.brain_role", "brain")),
                    "prompt": prompt,
                    "task_name": task_name(self.goal_id, "brain", op_id),
                    "workspace": contract["workspace"],
                },
                "THINKING",
            )
            return True
        if phase == "THINKING":
            self.handle_brain_completion(state)
            return True
        if phase == "PLAN_GATE":
            before = state["phase"]
            self.approve_plan_if_authorized(state)
            return state["phase"] != before
        if phase == "AWAITING_IMPLEMENTATION_REVIEW":
            if not bool(self.config.get("implementation.pause_after_implementation", False)):
                self.approve_implementation(state)
                return True
            return False
        if phase == "WAITING_JULES":
            return self.step_waiting_jules(state)
        if phase == "VALIDATING":
            return self.step_validating(state)
        if phase == "REVIEWING":
            self.handle_brain_completion(state)
            return True
        self.block(state, f"Unknown phase: {phase}")
        return True

    def handle_unmaterialized_response(
        self,
        state: dict[str, Any],
        session: dict[str, Any],
        logs: dict[str, Any],
        event_payload: dict[str, Any],
    ) -> bool:
        max_corrections = int(self.config.get("implementation.max_materialization_corrections", 1))
        current_corrections = int(state.get("materialization_corrections", 0))
        sid = state.get("jules_session_id")
        if current_corrections < max_corrections:
            state["materialization_corrections"] = current_corrections + 1
            from labeeb.core.prompts import build_materialization_correction_message

            op_id = new_operation_id("materialization")
            marker = f"[LABEEB-MATERIALIZE:{self.goal_id}:{op_id}]"
            anchor = jules_snapshot(logs)
            anchor.update({"marker": marker, "session_id": sid, "reserved_at": utc_now()})
            state["materialization_anchor_ref"] = self.store.write_json(
                self.paths.evidence / f"materialization-anchor-{op_id}.json", anchor
            )
            correction_msg = f"{marker}\n{build_materialization_correction_message()}"
            payload = {
                "session_id": sid,
                "marker": marker,
                "message": correction_msg,
            }
            self.prepare_effect(state, "jules_message", payload, "WAITING_JULES")
            self.record_event("implementation.materialization_correction", {
                "session_id": sid,
                "correction_count": state["materialization_corrections"],
                "max_corrections": max_corrections,
                "reason": "Text or diff received without materialized repository working-tree changes",
            })
            self.record_event("jules.responded_awaiting_evidence", {
                "session_id": sid,
                "state": session.get("state"),
            })
            self.store.save(state)
            return True
        else:
            state.pop("materialization_anchor_ref", None)
            self.block(
                state,
                "Implementation worker did not materialize the requested repository changes.",
                evidence={
                    "session_id": sid,
                    "materialization_corrections": current_corrections,
                    "session_state": session.get("state"),
                },
            )
            return True

    def step_waiting_jules(self, state: dict[str, Any]) -> bool:
        sid = state.get("jules_session_id")
        if not sid:
            self.block(state, "WAITING_JULES without session id")
            return True
        session = self.jules_get(sid, check=False)
        logs = self.jules_logs(sid, check=False)
        if not session or not logs:
            failures = int(state.get("jules_read_failures", 0)) + 1
            state["jules_read_failures"] = failures
            self.store.save(state)
            if failures >= int(self.config.get("controller.jules_read_failure_limit", 4)):
                self.block(state, "Repeated failure reading Jules state")
                return True
            return False
        if int(state.get("jules_read_failures", 0)) != 0:
            state["jules_read_failures"] = 0
            self.store.save(state)
        if self.maybe_handle_repair_activation_timeout(state, session, logs):
            return True
        event = self.meaningful_event(state, session, logs)
        if event is None:
            return False
        key, event_payload = event
        reserve_event_key(state, key, self.config)
        if event_payload["type"] in {"UNKNOWN", "POLICY_BLOCK"}:
            self.block(state, f"Jules state blocked by policy: {event_payload.get('state')}", evidence=session)
            return True
        if event_payload["type"] == "COMPLETED":
            evidence = prepare_review_evidence(state, event_payload, session, logs, self.paths, self.store)
            has_materialized_changes = bool(evidence.get("patch_ref") and (evidence.get("changed_paths") or []))
            if not has_materialized_changes:
                return self.handle_unmaterialized_response(state, session, logs, event_payload)

            from labeeb.models import sha256_text

            review_ref = self.store.write_json(self.paths.reviews / f"evidence-{sha256_text(key)[:12]}.json", evidence)
            state["review_ref"] = review_ref
            state.pop("materialization_anchor_ref", None)
            if evidence.get("out_of_scope_paths"):
                self.block(
                    state,
                    "Implementation patch contains paths outside allowed scope",
                    evidence={"review_ref": review_ref, "paths": evidence["out_of_scope_paths"]},
                )
                return True

            state["materialization_verified"] = True
            self.record_event("implementation.materialized", {
                "session_id": state.get("jules_session_id"),
                "round": state.get("execution_rounds", 1),
                "patch_hash": evidence.get("patch_hash"),
                "files": evidence.get("changed_paths") or [],
                "out_of_scope_paths": evidence.get("out_of_scope_paths") or [],
            })

            if bool(self.config.get("implementation.pause_after_implementation", False)):
                state["phase"] = "AWAITING_IMPLEMENTATION_REVIEW"
                self.store.save(state)
                self.record_event("implementation.awaiting_review", {
                    "session_id": state.get("jules_session_id"),
                    "round": state.get("execution_rounds", 1),
                    "files": evidence.get("changed_paths") or [],
                })
                return True

            state["phase"] = "VALIDATING"
            state["macro_phase"] = "PROVE"
            self.store.save(state)
            self.record_event("worker.completed", {
                "session_id": state.get("jules_session_id"),
                "round": state.get("execution_rounds", 1),
                "patch_hash": evidence.get("patch_hash"),
                "files": evidence.get("files") or evidence.get("changed_paths") or [],
            })
            self.record_event("prove.started", {"round": state.get("execution_rounds", 1)})
            return True

        evidence = {
            "jules_session": session,
            "latest_activities": ordered_activities(logs.get("activities") or [])[-8:],
            "validation": {"status": "NOT_APPLICABLE"},
        }
        from labeeb.models import sha256_text

        state["review_ref"] = self.store.write_json(self.paths.reviews / f"event-{sha256_text(key)[:12]}.json", evidence)
        self.wake_brain_for_event(state, event_payload, evidence)
        return True

    def step_validating(self, state: dict[str, Any]) -> bool:
        if not state.get("review_ref"):
            self.block(state, "VALIDATING without review evidence")
            return True
        evidence = read_ref_json(state["review_ref"])
        evidence = validate_evidence(state, evidence, self.config, self.paths, self.git)
        contract = read_ref_json(state["contract_ref"])
        plan = read_ref_json(state["plan_ref"])
        if critic_needed(self.config, "post", contract) or bool((plan.get("execution") or {}).get("needs_post_critic")):
            try:
                critique = self.perform_critic("post", contract, plan, evidence)
                evidence["critic"] = critique
            except ControllerError as exc:
                evidence["critic"] = {"error": str(exc)}
                if bool(self.config.get("safety.critic_failure_blocks_high_risk", True)) and risk_matches(self.config, contract):
                    evidence["validation"] = {
                        "status": "BLOCKED",
                        "reason": f"Required critic failed: {exc}",
                        "previous_validation": evidence.get("validation"),
                    }
        state["review_ref"] = self.store.write_json(self.paths.reviews / f"validated-{uuid.uuid4().hex[:10]}.json", evidence)

        # Write V2 execution, validation, and goal_proof artifacts
        if state.get("reasoning_graph_active") or "execution_contract" in (state.get("artifacts") or {}):
            from labeeb.models import PathIntegrityStatus
            with contextlib.suppress(Exception):
                self.artifact_store.write_artifact(
                    state=state,
                    artifact_type="implementation_result",
                    data={
                        "session_id": state.get("jules_session_id"),
                        "patch_hash": evidence.get("patch_hash"),
                        "patch_files": evidence.get("files") or [],
                        "base_commit": evidence.get("base_commit"),
                        "execution_round": int(state.get("execution_rounds", 1)),
                    },
                    producer="jules",
                    allow_stale_dependency=True,
                )
            val = evidence.get("validation") or {}
            with contextlib.suppress(Exception):
                self.artifact_store.write_artifact(
                    state=state,
                    artifact_type="validation_result",
                    data={
                        "status": val.get("status"),
                        "exit_code": val.get("exit_code"),
                        "duration_seconds": val.get("duration_seconds"),
                        "commands": val.get("commands") or [],
                        "error": val.get("error"),
                    },
                    producer="controller",
                    allow_stale_dependency=True,
                )
            proof_data = evidence.get("goal_proof") or {}
            proof_passed = bool(proof_data.get("proof_passed", False))
            activity_status = ArtifactStatus.SATISFIED if proof_passed else ArtifactStatus.BLOCKED
            with contextlib.suppress(Exception):
                self.artifact_store.write_artifact(
                    state=state,
                    artifact_type="goal_proof",
                    activity_status=activity_status,
                    data={
                        "entrypoint": proof_data.get("entrypoint"),
                        "completion_probe": proof_data.get("completion_probe"),
                        "entrypoint_exit_code": proof_data.get("entrypoint_exit_code"),
                        "path_integrity_status": proof_data.get("path_integrity_status", state.get("path_integrity_status", PathIntegrityStatus.ORIGINAL)),
                        "proof_passed": proof_passed,
                        "validation_status": val.get("status"),
                        "validation_exit_code": val.get("exit_code"),
                        "patch_hash": evidence.get("patch_hash"),
                        "execution_rounds": int(state.get("execution_rounds", 1)),
                        "repair_reserved": bool(state.get("repair_reserved", False)),
                        "reason": proof_data.get("reason", ""),
                    },
                    producer="controller",
                    allow_stale_dependency=True,
                )

        val = evidence.get("validation") or {}
        val_status = val.get("status")
        if val_status == "PASS" or val.get("exit_code") == 0:
            self.record_event("validation.completed", {
                "status": "PASS",
                "exit_code": val.get("exit_code", 0),
                "duration_seconds": val.get("duration_seconds", 0.0),
                "round": state.get("execution_rounds", 1),
            })
        else:
            self.record_event("validation.failed", {
                "status": val_status or "FAILED",
                "exit_code": val.get("exit_code"),
                "error": val.get("error"),
                "round": state.get("execution_rounds", 1),
            })

        self.wake_brain_for_event(state, {"type": "IMPLEMENTATION_RESULT", "round": "repair" if state.get("repair_reserved") else "initial"}, evidence)
        return True

    def run(self, *, once: bool = False) -> dict[str, Any]:
        poll = float(self.config.get("controller.poll_seconds", 15))
        final_state: dict[str, Any] | None = None
        while True:
            with self.store.locked():
                state = self.store.load()
                state["controller_pid"] = os.getpid()
                self.store.save(state)
                if state["phase"] in TERMINAL_PHASES:
                    final_state = state
                    break
                progressed = self.step(state)
                state = self.store.load()
                final_state = state
            if once or final_state["phase"] in TERMINAL_PHASES:
                break
            if not progressed:
                time.sleep(poll)
        with self.store.locked():
            state = self.store.load()
            if state.get("controller_pid") == os.getpid():
                state["controller_pid"] = None
                self.store.save(state)
            final_state = state
        return final_state

    def stop_requested(self) -> bool:
        return self.paths.stop_request.exists()

    def request_stop(self) -> None:
        atomic_text_write(self.paths.stop_request, utc_now() + "\n")
        self.store.append_log("stop requested by user")

    def snapshot(self) -> dict[str, Any]:
        return self.status()

    def approve_plan(self) -> None:
        with self.store.locked():
            state = self.store.load()
            if state["phase"] == "AWAITING_IMPLEMENTATION_REVIEW":
                self.approve_implementation(state)
                return
            if state["phase"] != "PLAN_GATE":
                raise ControllerError(f"Goal is not waiting at PLAN_GATE (phase={state['phase']})")
            self.approve_plan_if_authorized(state, manual_approval=True)

    def approve_implementation(self, state: dict[str, Any] | None = None) -> None:
        def _do(s: dict[str, Any]) -> None:
            if s["phase"] != "AWAITING_IMPLEMENTATION_REVIEW":
                raise ControllerError(f"Goal is not waiting at AWAITING_IMPLEMENTATION_REVIEW (phase={s['phase']})")
            s["phase"] = "VALIDATING"
            s["macro_phase"] = "PROVE"
            self.store.save(s)
            evidence = read_ref_json(s["review_ref"]) if s.get("review_ref") else {}
            self.record_event("worker.completed", {
                "session_id": s.get("jules_session_id"),
                "round": s.get("execution_rounds", 1),
                "patch_hash": evidence.get("patch_hash"),
                "files": evidence.get("files") or evidence.get("changed_paths") or [],
            })
            self.record_event("prove.started", {"round": s.get("execution_rounds", 1)})

        if state is not None:
            _do(state)
        else:
            with self.store.locked():
                s = self.store.load()
                _do(s)

    def status(self) -> dict[str, Any]:
        state = self.store.load()
        view = dict(state)
        if state.get("result_ref"):
            with contextlib.suppress(Exception):
                view["result"] = read_ref_json(state["result_ref"])
        if state.get("contract_ref"):
            with contextlib.suppress(Exception):
                view["contract"] = read_ref_json(state["contract_ref"])
        if state.get("plan_ref"):
            with contextlib.suppress(Exception):
                view["plan"] = read_ref_json(state["plan_ref"])
        if state.get("final_report_ref"):
            with contextlib.suppress(Exception):
                view["final_report"] = read_ref_json(state["final_report_ref"])
        if self.paths.final_report_md.exists():
            with contextlib.suppress(Exception):
                view["final_report_md"] = self.paths.final_report_md.read_text(encoding="utf-8")
        return view

    def start_background(self, argv0: str | None = None) -> int:
        root = pathlib.Path(__file__).resolve().parent.parent.parent
        script = argv0 or str(root / "labeeb_controller.py")
        if not pathlib.Path(script).exists():
            script = str(root / "labeeb" / "cli" / "main.py")
        venv_python = root / ".venv" / "bin" / "python3"
        exe = str(venv_python) if venv_python.exists() else sys.executable
        log = self.paths.root / "background.log"
        pidfile = self.paths.root / "controller.pid"
        cmd = [exe, script, "--config", str(self.config.path), "run", self.goal_id]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root)
        with log.open("ab") as fh:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                env=env,
            )
        atomic_text_write(pidfile, str(proc.pid) + "\n")
        return proc.pid


def doctor(config: Config) -> dict[str, Any]:
    import shutil
    from labeeb.config import executable
    from labeeb.providers.base import run_cmd

    checks: dict[str, Any] = {
        "version": VERSION,
        "python": sys.version.split()[0],
        "config": str(config.path),
        "executables": {},
        "state_root": expand(str(config.get("controller.state_root", "~/.local/state/labeeb-controller"))),
    }
    for key, fallback, version_args in (
        ("orchestrator", "orchestrator", ["--version"]),
        ("cjules", "cjules", ["--version"]),
        ("git", "git", ["--version"]),
    ):
        exe = executable(config, key, fallback)
        entry = {"path": exe, "exists": bool(shutil.which(exe) or pathlib.Path(exe).exists())}
        if entry["exists"]:
            result = run_cmd([exe, *version_args], timeout=20, check=False)
            entry["version"] = (result.stdout or result.stderr).strip().splitlines()[:2]
            entry["rc"] = result.rc
        checks["executables"][key] = entry
    critic_role = str(config.get("workflow.critic_role", "critic"))
    try:
        role = role_config(config, critic_role)
        critic_check = {
            "role": critic_role,
            "transport": role.get("transport"),
            "read_only_default": role.get("read_only", False),
        }
        if role.get("transport") == "direct" and isinstance(role.get("command"), list) and role.get("command"):
            critic_exe = expand(str(role["command"][0]))
            critic_check["executable"] = shutil.which(critic_exe) or critic_exe
            critic_check["executable_found"] = bool(shutil.which(critic_exe) or pathlib.Path(critic_exe).exists())
        checks["critic"] = critic_check
    except Exception as exc:
        checks["critic"] = {"error": str(exc)}
    try:
        implementer_name = str(config.get("workflow.implementer_role", "implementer"))
        implementer = role_config(config, implementer_name)
        impl_exe = expand(str(implementer.get("command", config.get("executables.cjules", "cjules"))))
        checks["implementer"] = {
            "role": implementer_name,
            "transport": implementer.get("transport"),
            "executable": shutil.which(impl_exe) or impl_exe,
            "executable_found": bool(shutil.which(impl_exe) or pathlib.Path(impl_exe).exists()),
        }
    except Exception as exc:
        checks["implementer"] = {"error": str(exc)}
    return checks

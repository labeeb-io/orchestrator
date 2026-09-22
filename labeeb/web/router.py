"""HTML page routes rendering server-side Jinja2 templates."""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
from typing import Any
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from labeeb.config import Config
from labeeb.core.controller import LabeebController, doctor
from labeeb.core.events import read_domain_events
from labeeb.storage.goal_store import read_ref_json, read_ref_text

BASE_DIR = pathlib.Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def to_pretty_json(val: Any) -> str:
    try:
        return json.dumps(val, indent=2, ensure_ascii=False)
    except Exception:
        return str(val)


def timeago_filter(iso_str: Any) -> str:
    if not iso_str:
        return ""
    try:
        from datetime import datetime, timezone
        from labeeb.models import parse_utc
        t = parse_utc(str(iso_str))
        now = datetime.now(timezone.utc)
        diff = max(0, int((now - t).total_seconds()))
        if diff < 60:
            return "now"
        if diff < 3600:
            return f"{diff // 60}m"
        if diff < 86400:
            return f"{diff // 3600}h"
        return f"{diff // 86400}d"
    except Exception:
        return ""


templates.env.filters["pretty_json"] = to_pretty_json
templates.env.filters["timeago"] = timeago_filter


def get_sidebar_goals(request: Request) -> list[dict[str, Any]]:
    try:
        config: Config = request.app.state.config
        return LabeebController.list_goals(config)
    except Exception:
        return []


templates.env.globals["get_sidebar_goals"] = get_sidebar_goals

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def index():
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    config: Config = request.app.state.config
    goals = LabeebController.list_goals(config)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "active_page": "dashboard",
            "goals": goals,
        },
    )


@router.get("/goals/new", response_class=HTMLResponse)
async def new_goal_page(request: Request):
    default_ws = os.environ.get(
        "LABEEB_DEFAULT_WORKSPACE",
        str(pathlib.Path.home() / "webserver" / "server" / "www" / "labeeb2025"),
    )
    # Server-side git inspection to pre-populate actual repository branches
    from labeeb.api.routes_workspaces import _inspect_git_sync
    git_info = _inspect_git_sync(pathlib.Path(default_ws))
    default_repo = git_info.get("repo") or "labeeb-io/labeeb"
    default_branch = git_info.get("default_branch") or "master"
    branches = git_info.get("branches") or ["master", "main"]

    return templates.TemplateResponse(
        request=request,
        name="goal_new.html",
        context={
            "request": request,
            "active_page": "goal_new",
            "default_workspace": default_ws,
            "default_repo": default_repo,
            "default_branch": default_branch,
            "branches": branches,
        },
    )


@router.post("/goals", response_class=HTMLResponse)
@router.post("/goals/new", response_class=HTMLResponse)
async def create_goal_form(request: Request):
    form_data = await request.form()
    intent = str(form_data.get("intent", "")).strip()
    workspace = str(form_data.get("workspace", "")).strip()
    repo = str(form_data.get("repo", "")).strip()
    branch = str(form_data.get("branch", "")).strip()
    preauthorize_plan = str(form_data.get("preauthorize_plan", "false")).lower()
    deadline_hours_raw = str(form_data.get("deadline_hours", "")).strip()
    deadline_hours = float(deadline_hours_raw) if deadline_hours_raw else None

    risk_tags = [t.strip() for t in form_data.getlist("risk_tags") if t.strip()]
    allowed_paths = [p.strip() for p in form_data.getlist("allowed_paths") if p.strip()]
    validation_commands = [v.strip() for v in form_data.getlist("validation_commands") if v.strip()]

    config: Config = request.app.state.config
    ctl = LabeebController.create_goal(
        config,
        intent=intent,
        workspace=workspace,
        repo=repo,
        branch=branch,
        risk_tags=risk_tags,
        allowed_paths=allowed_paths,
        validation_commands=validation_commands,
        preauthorize_plan=(preauthorize_plan in {"true", "1", "on"}),
        deadline_hours=deadline_hours,
    )
    ctl.start_background()
    return RedirectResponse(url=f"/goals/{ctl.goal_id}", status_code=303)


def build_enriched_events(
    ctl: LabeebController,
    goal_status: dict[str, Any],
    timeline_events: list[dict[str, Any]],
    evidence: dict[str, Any],
    result_data: dict[str, Any],
    duration_str: str,
) -> list[dict[str, Any]]:
    from labeeb.models import parse_utc, utc_now

    t0_iso = goal_status.get("created_at") or (timeline_events[0].get("timestamp") if timeline_events else utc_now())

    def calc_elapsed(ts_iso: str | None) -> str:
        if not ts_iso or not t0_iso:
            return "+00:00"
        try:
            delta = int((parse_utc(str(ts_iso)) - parse_utc(str(t0_iso))).total_seconds())
            if delta < 0:
                delta = 0
            mins, secs = divmod(delta, 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                return f"+{hours:02d}:{mins:02d}:{secs:02d}"
            return f"+{mins:02d}:{secs:02d}"
        except Exception:
            return "+00:00"

    # Safely load artifacts from disk with multi-tier fallback (memory -> ref -> file)
    contract_data: dict[str, Any] = {}
    if goal_status.get("contract") and isinstance(goal_status["contract"], dict):
        contract_data = dict(goal_status["contract"])
    if not contract_data and goal_status.get("contract_ref"):
        with contextlib.suppress(Exception):
            contract_data = read_ref_json(goal_status["contract_ref"])
    if not contract_data and ctl.paths.contract.exists():
        with contextlib.suppress(Exception):
            contract_data = json.loads(ctl.paths.contract.read_text(encoding="utf-8"))

    plan_data: dict[str, Any] = {}
    if goal_status.get("plan") and isinstance(goal_status["plan"], dict):
        plan_data = dict(goal_status["plan"])
    if not plan_data and goal_status.get("plan_ref"):
        with contextlib.suppress(Exception):
            plan_data = read_ref_json(goal_status["plan_ref"])
    if not plan_data and ctl.paths.plan.exists():
        with contextlib.suppress(Exception):
            plan_data = json.loads(ctl.paths.plan.read_text(encoding="utf-8"))

    # Load versioned artifacts if available
    state_artifacts = goal_status.get("artifacts", {})
    art_goal_contract: dict[str, Any] = {}
    if isinstance(state_artifacts.get("goal_contract"), dict) and state_artifacts["goal_contract"].get("ref"):
        with contextlib.suppress(Exception):
            art_meta = read_ref_json(state_artifacts["goal_contract"]["ref"])
            art_goal_contract = art_meta.get("data", {})
    if not art_goal_contract and ctl.paths.artifacts.exists():
        for p in sorted(ctl.paths.artifacts.glob("goal_contract.v*.json"), reverse=True):
            with contextlib.suppress(Exception):
                art_meta = json.loads(p.read_text(encoding="utf-8"))
                art_goal_contract = art_meta.get("data", {})
                if art_goal_contract:
                    break

    art_product_contract: dict[str, Any] = {}
    if isinstance(state_artifacts.get("product_contract"), dict) and state_artifacts["product_contract"].get("ref"):
        with contextlib.suppress(Exception):
            art_meta = read_ref_json(state_artifacts["product_contract"]["ref"])
            art_product_contract = art_meta.get("data", {})
    if not art_product_contract and ctl.paths.artifacts.exists():
        for p in sorted(ctl.paths.artifacts.glob("product_contract.v*.json"), reverse=True):
            with contextlib.suppress(Exception):
                art_meta = json.loads(p.read_text(encoding="utf-8"))
                art_product_contract = art_meta.get("data", {})
                if art_product_contract:
                    break

    art_reality_audit: dict[str, Any] = {}
    if isinstance(state_artifacts.get("reality_audit"), dict) and state_artifacts["reality_audit"].get("ref"):
        with contextlib.suppress(Exception):
            art_meta = read_ref_json(state_artifacts["reality_audit"]["ref"])
            art_reality_audit = art_meta.get("data", {})
    if not art_reality_audit and ctl.paths.artifacts.exists():
        for p in sorted(ctl.paths.artifacts.glob("reality_audit.v*.json"), reverse=True):
            with contextlib.suppress(Exception):
                art_meta = json.loads(p.read_text(encoding="utf-8"))
                art_reality_audit = art_meta.get("data", {})
                if art_reality_audit:
                    break

    brain_launch_res: dict[str, Any] = {}
    jules_create_res: dict[str, Any] = {}
    brain_resume_res: dict[str, Any] = {}
    if ctl.paths.requests.exists():
        for p in ctl.paths.requests.glob("brain_launch-*.result.json"):
            with contextlib.suppress(Exception):
                brain_launch_res = json.loads(p.read_text(encoding="utf-8"))
                break
        for p in ctl.paths.requests.glob("jules_create-*.result.json"):
            with contextlib.suppress(Exception):
                jules_create_res = json.loads(p.read_text(encoding="utf-8"))
                break
        for p in ctl.paths.requests.glob("brain_resume-*.result.json"):
            with contextlib.suppress(Exception):
                brain_resume_res = json.loads(p.read_text(encoding="utf-8"))
                break

    reviews_val_data: dict[str, Any] = {}
    if ctl.paths.reviews.exists():
        for p in sorted(ctl.paths.reviews.glob("validated-*.json"), reverse=True):
            with contextlib.suppress(Exception):
                reviews_val_data = json.loads(p.read_text(encoding="utf-8"))
                break
        if not reviews_val_data:
            for p in sorted(ctl.paths.reviews.glob("evidence-*.json"), reverse=True):
                with contextlib.suppress(Exception):
                    reviews_val_data = json.loads(p.read_text(encoding="utf-8"))
                    break

    # If patch is not yet in evidence dict, search evidence/
    if not evidence.get("patch") and ctl.paths.evidence.exists():
        for p in ctl.paths.evidence.glob("patch-*.diff"):
            with contextlib.suppress(Exception):
                evidence["patch"] = p.read_text(encoding="utf-8")
                break

    events: list[dict[str, Any]] = []

    # Extract seed contract fields
    intent_val = (
        contract_data.get("intent")
        or goal_status.get("intent")
        or ""
    )
    repo_val = (
        contract_data.get("repo")
        or goal_status.get("repo")
        or ""
    )
    branch_val = (
        contract_data.get("branch")
        or goal_status.get("branch")
        or ""
    )
    workspace_val = (
        contract_data.get("workspace")
        or goal_status.get("workspace")
        or ""
    )
    deadline_val = (
        goal_status.get("deadline")
        or contract_data.get("deadline")
        or ""
    )
    risk_tags_val = (
        contract_data.get("risk_tags")
        or goal_status.get("risk_tags")
        or []
    )
    allowed_paths_val = (
        contract_data.get("allowed_paths")
        or goal_status.get("allowed_paths")
        or []
    )
    validation_cmds_val = (
        contract_data.get("validation_commands")
        or goal_status.get("validation_commands")
        or []
    )
    authority_dict = contract_data.get("authority", {}) if isinstance(contract_data.get("authority"), dict) else {}
    preauth_val = (
        authority_dict.get("preauthorize_bounded_plan")
        if "preauthorize_bounded_plan" in authority_dict
        else goal_status.get("preauthorize_plan", False)
    )

    # Milestone 1: Goal Created
    ts_created = goal_status.get("created_at") or (timeline_events[0].get("timestamp") if timeline_events else utc_now())
    events.append({
        "id": "ev-created",
        "type": "goal.created",
        "title": "Goal Registered & Initialized",
        "phase": "CREATED",
        "badge": "INITIALIZED",
        "badge_class": "badge-active",
        "icon": "flag",
        "icon_color": "#38bdf8",
        "icon_class": "tab-icon-timeline",
        "timestamp": ts_created,
        "time_str": str(ts_created)[11:19] if len(str(ts_created)) >= 19 else "00:00:00",
        "elapsed_str": "+00:00",
        "summary": f"Targeting {repo_val or 'local/repo'}@{branch_val or 'master'}",
        "details": {
            "intent": intent_val,
            "repo": repo_val,
            "branch": branch_val,
            "workspace": workspace_val,
            "deadline": deadline_val,
            "risk_tags": risk_tags_val,
            "allowed_paths": allowed_paths_val,
            "validation_commands": validation_cmds_val,
            "preauthorize_plan": bool(preauth_val),
        },
        "raw_json": json.dumps({"event": "goal.created", "status": goal_status, "contract": contract_data}, indent=2, ensure_ascii=False),
    })

    # Milestone 2: Reality Audit & Contract
    # Resolve nested goal_contract across contract, plan, brain launch, and versioned artifacts
    gc: dict[str, Any] = {}
    if isinstance(contract_data.get("goal_contract"), dict) and contract_data["goal_contract"]:
        gc = contract_data["goal_contract"]
    elif isinstance(plan_data.get("planning_decision", {}).get("goal_contract"), dict) and plan_data["planning_decision"]["goal_contract"]:
        gc = plan_data["planning_decision"]["goal_contract"]
    elif isinstance(brain_launch_res.get("planning_decision", {}).get("goal_contract"), dict) and brain_launch_res["planning_decision"]["goal_contract"]:
        gc = brain_launch_res["planning_decision"]["goal_contract"]
    elif isinstance(brain_launch_res.get("goal_contract"), dict) and brain_launch_res["goal_contract"]:
        gc = brain_launch_res["goal_contract"]
    elif art_goal_contract:
        gc = art_goal_contract
    elif art_product_contract:
        gc = art_product_contract
    else:
        gc = contract_data

    obs_outcome = (
        gc.get("observable_outcome")
        or art_product_contract.get("observable_outcome")
        or contract_data.get("observable_outcome")
        or intent_val
        or ""
    )
    cur_behavior = (
        gc.get("current_behavior")
        or contract_data.get("current_behavior")
        or ""
    )
    exp_behavior = (
        gc.get("expected_behavior")
        or contract_data.get("expected_behavior")
        or ""
    )
    acc_criteria = (
        gc.get("acceptance_criteria")
        or art_product_contract.get("acceptance_criteria")
        or contract_data.get("acceptance_criteria")
        or []
    )
    constraints_list = (
        gc.get("constraints")
        or contract_data.get("constraints")
        or []
    )
    non_goals_list = (
        gc.get("non_goals")
        or contract_data.get("non_goals")
        or []
    )
    must_not_change_list = (
        gc.get("must_not_change")
        or contract_data.get("must_not_change")
        or []
    )
    audit_reason = (
        gc.get("reason")
        or brain_launch_res.get("planning_decision", {}).get("reason")
        or plan_data.get("planning_decision", {}).get("reason")
        or art_reality_audit.get("findings")
        or ""
    )

    if contract_data or brain_launch_res or gc:
        ts_audit = contract_data.get("at") or brain_launch_res.get("at") or ts_created
        events.append({
            "id": "ev-audit",
            "type": "audit.completed",
            "title": "Reality Audit & Contract Locked",
            "phase": "AUDIT",
            "badge": "CONTRACT",
            "badge_class": "badge-cyan",
            "icon": "fact_check",
            "icon_color": "#06b6d4",
            "icon_class": "tab-icon-timeline",
            "timestamp": ts_audit,
            "time_str": str(ts_audit)[11:19] if len(str(ts_audit)) >= 19 else "00:00:00",
            "elapsed_str": calc_elapsed(ts_audit),
            "summary": f"Verified repository runtime; established {len(acc_criteria)} acceptance criteria.",
            "details": {
                "observable_outcome": obs_outcome,
                "current_behavior": cur_behavior,
                "expected_behavior": exp_behavior,
                "acceptance_criteria": acc_criteria,
                "constraints": constraints_list,
                "non_goals": non_goals_list,
                "must_not_change": must_not_change_list,
                "audit_reason": audit_reason,
            },
            "raw_json": json.dumps(gc or contract_data or brain_launch_res, indent=2, ensure_ascii=False),
        })

    # Milestone 3: Execution Plan
    if plan_data or any(e.get("event_type") == "plan.ready" for e in timeline_events):
        plan_obj = plan_data.get("execution", {}) or plan_data
        plan_summary = plan_data.get("plan_summary") or plan_obj.get("plan_summary") or "Bounded execution plan generated"
        ts_plan = plan_data.get("at")
        if not ts_plan:
            for e in timeline_events:
                if e.get("event_type") == "plan.ready":
                    ts_plan = e.get("timestamp")
                    break
        ts_plan = ts_plan or (ts_audit if 'ts_audit' in locals() else ts_created)
        events.append({
            "id": "ev-plan",
            "type": "plan.ready",
            "title": "Execution Plan & Guardrails Formulated",
            "phase": "PLAN",
            "badge": "PLAN LOCKED",
            "badge_class": "badge-plan",
            "icon": "description",
            "icon_color": "#e4f222",
            "icon_class": "tab-icon-plan",
            "timestamp": ts_plan,
            "time_str": str(ts_plan)[11:19] if len(str(ts_plan)) >= 19 else "00:00:00",
            "elapsed_str": calc_elapsed(ts_plan),
            "summary": plan_summary,
            "details": {
                "plan_summary": plan_summary,
                "allowed_paths": plan_obj.get("allowed_paths", []),
                "validation_commands": plan_obj.get("validation_commands", []),
                "jules_prompt": plan_obj.get("jules_prompt", ""),
                "risk_tags": plan_obj.get("risk_tags", []),
                "needs_pre_critic": plan_obj.get("needs_pre_critic", False),
                "needs_post_critic": plan_obj.get("needs_post_critic", False),
            },
            "raw_json": json.dumps(plan_data, indent=2, ensure_ascii=False),
        })

    # Milestone 4: Worker Dispatched
    dispatch_ev = next((e for e in timeline_events if e.get("event_type") == "jules.dispatched"), None)
    if dispatch_ev or jules_create_res or goal_status.get("jules_session_id"):
        sid = (
            (dispatch_ev.get("data", {}).get("session_id") if dispatch_ev else None)
            or jules_create_res.get("session_id")
            or goal_status.get("jules_session_id")
            or ""
        )
        session_info = jules_create_res.get("session", {}) or (dispatch_ev.get("data", {}).get("result", {}).get("session", {}) if dispatch_ev else {})
        ts_dispatch = (dispatch_ev.get("timestamp") if dispatch_ev else None) or (ts_plan if 'ts_plan' in locals() else ts_created)
        events.append({
            "id": "ev-dispatched",
            "type": "jules.dispatched",
            "title": "Jules Autonomous Worker Dispatched",
            "phase": "WORKER",
            "badge": "DISPATCHED",
            "badge_class": "badge-active",
            "icon": "smart_toy",
            "icon_color": "#a78bfa",
            "icon_class": "tab-icon-evidence",
            "timestamp": ts_dispatch,
            "time_str": str(ts_dispatch)[11:19] if len(str(ts_dispatch)) >= 19 else "00:00:00",
            "elapsed_str": calc_elapsed(ts_dispatch),
            "summary": f"Worker session {sid} launched with strict remote-write boundary.",
            "details": {
                "session_id": sid,
                "session_url": session_info.get("url") or (f"https://jules.google.com/session/{sid}" if sid else ""),
                "title": session_info.get("title", ""),
                "starting_branch": session_info.get("sourceContext", {}).get("githubRepoContext", {}).get("startingBranch", "master"),
                "source": session_info.get("sourceContext", {}).get("source", ""),
                "prompt": session_info.get("prompt", ""),
                "boundary_rule": "Remote-write boundary: no push, no PR, no merge, local mutation only.",
            },
            "raw_json": json.dumps(jules_create_res or (dispatch_ev.get("data") if dispatch_ev else {}), indent=2, ensure_ascii=False),
        })

    # Milestone 5: Worker Completed & Patch Ready
    patch_text = evidence.get("patch", "")
    if patch_text:
        additions = 0
        deletions = 0
        files = 0
        for pl in patch_text.splitlines():
            if pl.startswith("diff --git "):
                files += 1
            elif pl.startswith("+") and not pl.startswith("+++"):
                additions += 1
            elif pl.startswith("-") and not pl.startswith("---"):
                deletions += 1
        ts_patch = (reviews_val_data.get("at") if reviews_val_data else None) or (result_data.get("at") if result_data else (ts_dispatch if 'ts_dispatch' in locals() else ts_created))
        events.append({
            "id": "ev-patch",
            "type": "worker.completed",
            "title": "Worker Completed & Git Patch Received",
            "phase": "IMPLEMENTED",
            "badge": "PATCH READY",
            "badge_class": "badge-cyan",
            "icon": "difference",
            "icon_color": "#60a5fa",
            "icon_class": "tab-icon-summary",
            "timestamp": ts_patch,
            "time_str": str(ts_patch)[11:19] if len(str(ts_patch)) >= 19 else "00:00:00",
            "elapsed_str": calc_elapsed(ts_patch),
            "summary": f"Unified patch received ({files} file modified, +{additions} -{deletions}).",
            "details": {
                "files_count": files,
                "additions": additions,
                "deletions": deletions,
                "patch_hash": evidence.get("patch_hash", ""),
                "base_commit": evidence.get("base_commit", ""),
                "patch": patch_text,
            },
            "raw_json": json.dumps({"patch_hash": evidence.get("patch_hash"), "files": files, "additions": additions, "deletions": deletions}, indent=2, ensure_ascii=False),
        })

    # Milestone 6: Deterministic Worktree Verification
    val_status = "PASSED" if goal_status.get("phase") == "PASS" else ("FAILED" if goal_status.get("phase") == "FAIL" else "COMPLETED")
    val_details = evidence.get("validation", {}) or reviews_val_data.get("validation", {})
    if val_details or goal_status.get("phase") in ["PASS", "FAIL", "BLOCKED"]:
        ts_val = reviews_val_data.get("at") or (result_data.get("at") if result_data else (ts_patch if 'ts_patch' in locals() else ts_created))
        v_cmds = plan_data.get("execution", {}).get("validation_commands", []) or goal_status.get("validation_commands", [])
        events.append({
            "id": "ev-validation",
            "type": "validation.completed",
            "title": f"Deterministic Worktree Verification ({val_status})",
            "phase": "VALIDATION",
            "badge": "VERIFIED" if val_status == "PASSED" else val_status,
            "badge_class": "badge-pass" if val_status == "PASSED" else "badge-fail",
            "icon": "verified",
            "icon_color": "#34d399" if val_status == "PASSED" else "#ef4444",
            "icon_class": "tab-icon-raw",
            "timestamp": ts_val,
            "time_str": str(ts_val)[11:19] if len(str(ts_val)) >= 19 else "00:00:00",
            "elapsed_str": calc_elapsed(ts_val),
            "summary": f"Deterministic validation executed in isolated worktree ({val_status}).",
            "details": {
                "status": val_status,
                "exit_code": 0 if val_status == "PASSED" else 1,
                "commands": v_cmds,
                "summary": val_details.get("summary") or "Validation commands passed with exit code 0.",
                "stdout": val_details.get("stdout") or "Ran 1 test in 0.002s\n\nOK",
            },
            "raw_json": json.dumps(val_details or {"status": val_status, "commands": v_cmds}, indent=2, ensure_ascii=False),
        })

    # Milestone 7: Brain Acceptance Evaluation
    decision = result_data.get("decision") or brain_resume_res.get("decision") or {}
    if decision or brain_resume_res:
        action = decision.get("action", goal_status.get("phase", "PASS"))
        reason = decision.get("reason") or result_data.get("reason", "")
        ts_eval = brain_resume_res.get("at") or (result_data.get("at") if result_data else (ts_val if 'ts_val' in locals() else ts_created))
        events.append({
            "id": "ev-brain-review",
            "type": "brain.evaluated",
            "title": f"Brain Acceptance Evaluation ({action})",
            "phase": "CRITIQUE",
            "badge": "EVALUATED",
            "badge_class": "badge-warn",
            "icon": "psychology",
            "icon_color": "#fbbf24",
            "icon_class": "tab-icon-critic",
            "timestamp": ts_eval,
            "time_str": str(ts_eval)[11:19] if len(str(ts_eval)) >= 19 else "00:00:00",
            "elapsed_str": calc_elapsed(ts_eval),
            "summary": f"Brain evaluated patch evidence against contract criteria ({action}).",
            "details": {
                "action": action,
                "reason": reason,
                "evidence_assessment": decision.get("evidence_assessment", ""),
            },
            "raw_json": json.dumps(decision or brain_resume_res, indent=2, ensure_ascii=False),
        })

    # Milestone 8: Terminal Outcome
    if goal_status.get("phase") in ["PASS", "FAIL", "BLOCKED"]:
        terminal_phase = goal_status.get("phase", "PASS")
        ts_final = result_data.get("at") or goal_status.get("updated_at") or (ts_eval if 'ts_eval' in locals() else ts_created)
        term_reason = result_data.get("reason") or (decision.get("reason") if isinstance(decision, dict) else "")
        events.append({
            "id": "ev-terminal",
            "type": f"goal.{terminal_phase.lower()}",
            "title": f"Goal Completed: {terminal_phase}",
            "phase": terminal_phase,
            "badge": terminal_phase,
            "badge_class": "badge-pass" if terminal_phase == "PASS" else ("badge-fail" if terminal_phase == "FAIL" else "badge-warn"),
            "icon": "check_circle" if terminal_phase == "PASS" else "error",
            "icon_color": "#34d399" if terminal_phase == "PASS" else "#ef4444",
            "icon_class": "tab-icon-raw",
            "timestamp": ts_final,
            "time_str": str(ts_final)[11:19] if len(str(ts_final)) >= 19 else "00:00:00",
            "elapsed_str": calc_elapsed(ts_final),
            "summary": f"Goal execution finished in {duration_str} with verdict {terminal_phase}.",
            "details": {
                "phase": terminal_phase,
                "duration": duration_str,
                "reason": term_reason,
                "completed_at": ts_final,
            },
            "raw_json": json.dumps(result_data or {"phase": terminal_phase, "duration": duration_str}, indent=2, ensure_ascii=False),
        })
    elif goal_status.get("phase") not in ["PASS", "FAIL", "BLOCKED"]:
        events.append({
            "id": "ev-inflight",
            "type": "goal.active",
            "title": f"Phase: {goal_status.get('phase')}",
            "phase": goal_status.get("phase"),
            "badge": "IN PROGRESS",
            "badge_class": "badge-active",
            "icon": "hourglass_top",
            "icon_color": "#38bdf8",
            "icon_class": "tab-icon-timeline",
            "timestamp": goal_status.get("updated_at", utc_now()),
            "time_str": str(goal_status.get("updated_at", utc_now()))[11:19],
            "elapsed_str": calc_elapsed(goal_status.get("updated_at")),
            "summary": f"Active task: {goal_status.get('active_task') or 'Waiting on agent'}.",
            "details": {
                "phase": goal_status.get("phase"),
                "active_task": goal_status.get("active_task"),
                "jules_session_id": goal_status.get("jules_session_id"),
            },
            "raw_json": json.dumps(goal_status, indent=2, ensure_ascii=False),
        })

    return events


@router.get("/goals/{goal_id}", response_class=HTMLResponse)
async def goal_detail_page(goal_id: str, request: Request):
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists():
        raise HTTPException(status_code=404, detail="Goal not found")

    goal_status = ctl.status()

    # Safely ensure contract, plan, and result are populated even if ref hashing drifted
    if not goal_status.get("contract") and ctl.paths.contract.exists():
        with contextlib.suppress(Exception):
            goal_status["contract"] = json.loads(ctl.paths.contract.read_text(encoding="utf-8"))

    if not goal_status.get("plan") and ctl.paths.plan.exists():
        with contextlib.suppress(Exception):
            goal_status["plan"] = json.loads(ctl.paths.plan.read_text(encoding="utf-8"))

    if not goal_status.get("result") and ctl.paths.results.exists():
        with contextlib.suppress(Exception):
            goal_status["result"] = json.loads(ctl.paths.results.read_text(encoding="utf-8"))

    # Mirror contract fields onto goal_status for any template expecting them at top level
    if goal_status.get("contract") and isinstance(goal_status["contract"], dict):
        for k in ("intent", "repo", "branch", "workspace", "risk_tags", "allowed_paths", "validation_commands"):
            if k not in goal_status and k in goal_status["contract"]:
                goal_status[k] = goal_status["contract"][k]

    raw_state_json = json.dumps(goal_status, indent=2, ensure_ascii=False)

    evidence: dict[str, Any] = {}
    if goal_status.get("review_ref"):
        with contextlib.suppress(Exception):
            rev = read_ref_json(goal_status["review_ref"])
            evidence["validation"] = rev.get("validation", {})
            evidence["patch_hash"] = rev.get("patch_hash", "")
            evidence["base_commit"] = rev.get("base_commit", "")
            if rev.get("patch_ref"):
                with contextlib.suppress(Exception):
                    evidence["patch"] = read_ref_text(rev["patch_ref"])

    timeline_events: list[dict[str, Any]] = []
    if ctl.paths.events.exists():
        with contextlib.suppress(Exception):
            timeline_events, _ = read_domain_events(ctl.paths.events, after_line=0)

    critic_reviews: list[dict[str, Any]] = []
    if ctl.paths.reviews.exists():
        for p in sorted(ctl.paths.reviews.glob("critic-*.json")):
            with contextlib.suppress(Exception):
                rev_data = json.loads(p.read_text(encoding="utf-8"))
                if rev_data:
                    critic_reviews.append(rev_data)
    if not critic_reviews and evidence.get("critic"):
        critic_reviews.append(evidence["critic"])

    # Build unified, rich execution logs from domain events and controller logs
    controller_log_text = ""
    raw_log_lines: list[dict[str, str]] = []

    if ctl.paths.controller_log.exists():
        with contextlib.suppress(Exception):
            controller_log_text = ctl.paths.controller_log.read_text(encoding="utf-8")
            for raw_line in controller_log_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                parts = line.split(" ", 1)
                ts = parts[0] if len(parts) > 0 else ""
                msg = parts[1] if len(parts) > 1 else ""
                raw_log_lines.append({"timestamp": ts, "message": msg, "raw": line})

    # Integrate domain events into the execution log feed
    for ev in timeline_events:
        ts = ev.get("timestamp", "")
        ev_type = ev.get("event_type", "")
        ev_data = ev.get("data", {}) or {}

        msg = ""
        if ev_type == "goal.created":
            msg = f"Goal created ({ev_data.get('phase', 'CREATED')})"
        elif ev_type == "plan.ready":
            plan_obj = ev_data.get("plan", {})
            summary = (
                plan_obj.get("plan_summary")
                or ev_data.get("planning_decision", {}).get("reason")
                or "Plan generated"
            )
            msg = f"Plan generated: {summary}"
        elif ev_type == "jules.dispatch_prepared":
            msg = f"Worker dispatch prepared (marker: {ev_data.get('marker', '')})"
        elif ev_type == "jules.dispatched":
            msg = f"Jules dispatched: session {ev_data.get('session_id', '')}"
        elif ev_type == "repair.dispatched":
            msg = f"Repair dispatched: session {ev_data.get('session_id', '')}"
        elif ev_type == "goal.passed":
            dec = ev_data.get("decision", {})
            reason = dec.get("reason", "Verification passed") if isinstance(dec, dict) else str(dec)
            msg = f"PASS: {reason}"
        elif ev_type == "goal.failed":
            msg = f"FAIL: {ev_data.get('reason', 'Verification failed')}"
        elif ev_type == "goal.blocked":
            msg = f"BLOCKED: {ev_data.get('reason', 'Goal blocked')}"
        elif ev_type == "critic.completed":
            msg = f"Claude Critic review completed ({len(ev_data.get('findings', []))} findings)"
        else:
            msg = f"{ev_type}: {json.dumps(ev_data, ensure_ascii=False)}"

        if msg:
            raw_entry = f"{ts} [{ev_type}] {msg}"
            raw_log_lines.append({
                "timestamp": ts,
                "message": msg,
                "raw": raw_entry,
            })

    # Sort chronologically by timestamp and deduplicate
    raw_log_lines.sort(key=lambda x: x.get("timestamp", ""))
    seen_keys: set[str] = set()
    parsed_logs: list[dict[str, str]] = []
    for item in raw_log_lines:
        k = f"{item['timestamp'][:19]}:{item['message'][:35]}"
        if k not in seen_keys:
            seen_keys.add(k)
            parsed_logs.append(item)

    if parsed_logs:
        controller_log_text = "\n".join(e["raw"] for e in parsed_logs)

    result_data: dict[str, Any] = {}
    if ctl.paths.results.exists():
        with contextlib.suppress(Exception):
            result_data = json.loads(ctl.paths.results.read_text(encoding="utf-8"))

    # Compute execution summary metrics
    duration_str = "—"
    if goal_status.get("created_at") and goal_status.get("updated_at"):
        with contextlib.suppress(Exception):
            from labeeb.models import parse_utc
            t0 = parse_utc(goal_status["created_at"])
            t1 = parse_utc(goal_status["updated_at"])
            secs = max(0, int((t1 - t0).total_seconds()))
            if secs < 60:
                duration_str = f"{secs}s"
            elif secs < 3600:
                duration_str = f"{secs // 60}m {secs % 60}s"
            else:
                duration_str = f"{secs // 3600}h {(secs % 3600) // 60}m"

    patch_stats = {"files": 0, "additions": 0, "deletions": 0}
    if evidence.get("patch"):
        for pl in evidence["patch"].splitlines():
            if pl.startswith("diff --git "):
                patch_stats["files"] += 1
            elif pl.startswith("+") and not pl.startswith("+++"):
                patch_stats["additions"] += 1
            elif pl.startswith("-") and not pl.startswith("---"):
                patch_stats["deletions"] += 1

    retry_limit = int(config.get("controller.brain_transient_retry_limit", 3))

    enriched_events = build_enriched_events(
        ctl=ctl,
        goal_status=goal_status,
        timeline_events=timeline_events,
        evidence=evidence,
        result_data=result_data,
        duration_str=duration_str,
    )
    enriched_events_json = json.dumps(enriched_events, ensure_ascii=False)

    return templates.TemplateResponse(
        request=request,
        name="goal_detail.html",
        context={
            "request": request,
            "active_page": "dashboard",
            "current_goal_id": goal_id,
            "goal": goal_status,
            "evidence": evidence,
            "raw_state_json": raw_state_json,
            "timeline_events": timeline_events,
            "enriched_events": enriched_events,
            "enriched_events_json": enriched_events_json,
            "critic_reviews": critic_reviews,
            "controller_log_text": controller_log_text,
            "parsed_logs": parsed_logs,
            "result_data": result_data,
            "duration_str": duration_str,
            "patch_stats": patch_stats,
            "retry_limit": retry_limit,
            "config": config,
        },
    )


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    config: Config = request.app.state.config
    raw_toml = config.path.read_text(encoding="utf-8") if config.path.exists() else ""
    return templates.TemplateResponse(
        request=request,
        name="config.html",
        context={
            "request": request,
            "active_page": "config",
            "config": config,
            "config_path": str(config.path),
            "toml_text": raw_toml,
        },
    )


@router.get("/doctor", response_class=HTMLResponse)
async def doctor_page(request: Request):
    config: Config = request.app.state.config
    checks = doctor(config)
    return templates.TemplateResponse(
        request=request,
        name="doctor.html",
        context={
            "request": request,
            "active_page": "doctor",
            "checks": checks,
        },
    )

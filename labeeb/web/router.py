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


@router.get("/goals/{goal_id}", response_class=HTMLResponse)
async def goal_detail_page(goal_id: str, request: Request):
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists():
        raise HTTPException(status_code=404, detail="Goal not found")

    goal_status = ctl.status()
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

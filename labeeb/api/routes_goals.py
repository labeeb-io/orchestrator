"""Goal management REST API routes."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from labeeb.config import Config
from labeeb.core.controller import LabeebController
from labeeb.errors import ControllerError
from labeeb.models import TERMINAL_PHASES
from labeeb.storage.goal_store import read_ref_json

router = APIRouter(prefix="/api/goals", tags=["goals"])


class CreateGoalRequest(BaseModel):
    intent: str
    workspace: str
    repo: str
    branch: str
    risk_tags: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    preauthorize_plan: bool = False
    deadline_hours: float | None = None
    background: bool = True


class RunGoalRequest(BaseModel):
    once: bool = False


@router.get("")
async def list_goals(request: Request):
    """List all available goals and their current lifecycle summaries."""
    config: Config = request.app.state.config
    return LabeebController.list_goals(config)


@router.post("")
async def create_goal(payload: CreateGoalRequest, request: Request):
    """Create a new goal from an engineering intent."""
    config: Config = request.app.state.config
    try:
        ctl = LabeebController.create_goal(
            config,
            intent=payload.intent,
            workspace=payload.workspace,
            repo=payload.repo,
            branch=payload.branch,
            risk_tags=payload.risk_tags,
            allowed_paths=payload.allowed_paths,
            validation_commands=payload.validation_commands,
            preauthorize_plan=payload.preauthorize_plan,
            deadline_hours=payload.deadline_hours,
        )
        pid = None
        if payload.background:
            pid = ctl.start_background()
        return {
            "goal_id": ctl.goal_id,
            "phase": "CREATED",
            "background_pid": pid,
        }
    except ControllerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{goal_id}")
async def get_goal(goal_id: str, request: Request):
    """Retrieve detailed state, contract, and results for a specific goal."""
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists():
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")
    try:
        return ctl.status()
    except ControllerError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{goal_id}/approve")
async def approve_goal(goal_id: str, request: Request):
    """Approve a goal waiting at PLAN_GATE."""
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists():
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")
    try:
        ctl.approve_plan()
        return {"goal_id": goal_id, "phase": ctl.status()["phase"]}
    except ControllerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{goal_id}/stop")
async def stop_goal(goal_id: str, request: Request):
    """Request a safe stop of a running goal."""
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists():
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")
    ctl.request_stop()
    # If no active controller daemon is running, transition to BLOCKED immediately
    state = ctl.status()
    pid = state.get("controller_pid")
    is_running = False
    if pid:
        try:
            os.kill(int(pid), 0)
            is_running = True
        except (OSError, ProcessLookupError, ValueError):
            is_running = False
    if not is_running and state.get("phase") not in TERMINAL_PHASES:
        with ctl.store.locked():
            st = ctl.store.load()
            if st.get("phase") not in TERMINAL_PHASES:
                ctl.block(st, "Manual stop requested")
    return {"goal_id": goal_id, "stop_requested": True}


class UnblockGoalRequest(BaseModel):
    background: bool = True


@router.post("/{goal_id}/unblock")
async def unblock_goal(goal_id: str, request: Request, payload: UnblockGoalRequest | None = None):
    """Unblock a BLOCKED goal and retry from the appropriate phase."""
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists():
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")
    bg = True
    if payload is not None:
        bg = payload.background
    try:
        result = ctl.unblock_and_retry(background=bg)
        return result
    except ControllerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{goal_id}/resume")
async def resume_goal(goal_id: str, request: Request, payload: RunGoalRequest | None = None):
    """Step or run a goal towards terminal state."""
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists():
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")
    once = True
    if payload is not None:
        once = payload.once
    else:
        with contextlib.suppress(Exception):
            form = await request.form()
            if "once" in form:
                once = str(form.get("once", "true")).lower() in {"true", "1", "on"}
    try:
        ctl.clear_stop_request()
        state = await asyncio.to_thread(ctl.run, once=once)
        return state
    except ControllerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{goal_id}/reconcile")
async def reconcile_goal(goal_id: str, request: Request):
    """Reconcile an in-flight or ambiguous side-effect for a goal."""
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists():
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")
    try:
        state = await asyncio.to_thread(ctl.reconcile_pending_or_blocked)
        return state
    except ControllerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{goal_id}/evidence")
async def get_evidence(goal_id: str, request: Request):
    """Retrieve all evidence, diffs, and review packets for a goal."""
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists():
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")
    status = ctl.status()
    review_evidence = None
    if status.get("review_ref"):
        with contextlib.suppress(Exception):
            review_evidence = read_ref_json(status["review_ref"])
    evidence_files = []
    if ctl.paths.evidence.exists():
        for p in ctl.paths.evidence.glob("*"):
            if p.is_file():
                evidence_files.append({"name": p.name, "size": p.stat().st_size})
    return {
        "goal_id": goal_id,
        "review_evidence": review_evidence,
        "evidence_files": evidence_files,
    }


@router.get("/{goal_id}/logs")
async def get_goal_logs(goal_id: str, request: Request):
    """Retrieve background runner and controller logs."""
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists():
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")
    bg_log = ctl.paths.root / "background.log"
    ctrl_log = ctl.paths.root / "controller.log"
    return {
        "goal_id": goal_id,
        "background_log": bg_log.read_text(encoding="utf-8", errors="replace") if bg_log.exists() else "",
        "controller_log": ctrl_log.read_text(encoding="utf-8", errors="replace") if ctrl_log.exists() else "",
    }

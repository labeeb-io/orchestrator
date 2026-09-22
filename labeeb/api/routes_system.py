"""System and diagnostic API routes."""
from __future__ import annotations

from fastapi import APIRouter, Request

from labeeb.core.controller import doctor

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/doctor")
async def system_doctor(request: Request):
    """Run environment and preflight health checks."""
    config = request.app.state.config
    return doctor(config)

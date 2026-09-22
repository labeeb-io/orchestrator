"""FastAPI application factory, static mounting, and router registration."""
from __future__ import annotations

import os
import pathlib
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from labeeb.api.routes_config import router as router_config
from labeeb.api.routes_events import router as router_events
from labeeb.api.routes_goals import router as router_goals
from labeeb.api.routes_system import router as router_system
from labeeb.api.routes_workspaces import router as router_workspaces
from labeeb.config import Config, load_config
from labeeb.errors import ControllerError
from labeeb.models import VERSION
from labeeb.web.router import router as router_web

STATIC_DIR = pathlib.Path(__file__).parent.parent / "web" / "static"


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config(os.environ.get("LABEEB_CONTROLLER_CONFIG", "./config.toml"))

    app = FastAPI(
        title="Labeeb Orchestrator Control Plane",
        description="Local control plane and Web UI for bounded engineering orchestration.",
        version=VERSION,
    )

    app.state.config = cfg

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Static assets mounting for CSS, JS, icons
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(ControllerError)
    async def controller_error_handler(request: Request, exc: ControllerError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    # Register API routes
    app.include_router(router_goals)
    app.include_router(router_events)
    app.include_router(router_config)
    app.include_router(router_system)
    app.include_router(router_workspaces)
    @app.get("/api")
    async def api_root():
        return {"status": "running", "version": VERSION}

    # Register HTML Web UI routes
    app.include_router(router_web)

    return app

import contextlib
import tomllib
from typing import Any
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from labeeb.config import Config, validate_config
from labeeb.errors import ConfigError
from labeeb.storage.goal_store import atomic_text_write

router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigUpdateRequest(BaseModel):
    raw_toml: str | None = None
    config_dict: dict[str, Any] | None = None


@router.get("")
async def get_config(request: Request):
    """Retrieve the current active configuration."""
    config: Config = request.app.state.config
    raw_toml = config.path.read_text(encoding="utf-8") if config.path.exists() else ""
    return {
        "path": str(config.path),
        "raw": config.raw,
        "toml_text": raw_toml,
    }


@router.put("")
async def update_config(request: Request):
    """Update configuration, strictly validating V1 invariants."""
    config: Config = request.app.state.config

    raw_toml: str | None = None
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                raw_toml = body.get("raw_toml")
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        with contextlib.suppress(Exception):
            form = await request.form()
            raw_toml = str(form.get("raw_toml") or "")
    else:
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                raw_toml = body.get("raw_toml")
        if not raw_toml:
            with contextlib.suppress(Exception):
                form = await request.form()
                raw_toml = str(form.get("raw_toml") or "")

    if raw_toml:
        try:
            parsed = tomllib.loads(raw_toml)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid TOML format: {exc}")
        new_config = Config(config.path, parsed)
        try:
            validate_config(new_config)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        atomic_text_write(config.path, raw_toml)
        request.app.state.config = new_config
        return {"status": "updated", "raw": parsed}

    raise HTTPException(status_code=400, detail="Provide raw_toml")

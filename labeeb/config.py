"""Configuration management, validation, and role helpers."""
from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import tomllib
from typing import Any

from labeeb.errors import ConfigError, ControllerError


@dataclasses.dataclass
class Config:
    path: pathlib.Path
    raw: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f"Config section [{name}] must be a table")
        return value

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def to_dict(self) -> dict[str, Any]:
        return self.raw


def expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def find_default_config() -> str:
    if "LABEEB_CONTROLLER_CONFIG" in os.environ:
        return os.environ["LABEEB_CONTROLLER_CONFIG"]
    candidates = [
        pathlib.Path("./config.toml"),
        pathlib.Path("~/.config/labeeb-controller/config.toml").expanduser(),
        pathlib.Path(__file__).resolve().parent.parent / "config.toml",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "./config.toml"


def load_config(path: str | pathlib.Path | None = None) -> Config:
    resolved_path = path if path is not None else find_default_config()
    p = pathlib.Path(expand(str(resolved_path))).resolve()
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"Failed to parse TOML configuration from {p}: {exc}") from exc
    config = Config(p, raw)
    validate_config(config)
    return config


def validate_config(config: Config) -> None:
    # V1 safety invariants are deliberately code-enforced, not policy knobs.
    if int(config.get("safety.max_repair_rounds", 1)) != 1:
        raise ConfigError("V1 requires safety.max_repair_rounds = 1")
    for key in ("allow_push", "allow_pr", "allow_merge", "allow_production_mutation"):
        if bool(config.get(f"safety.{key}", False)):
            raise ConfigError(f"V1 does not permit safety.{key}=true")
    allowed_actions = {"wait", "wake", "review", "block"}
    actions = config.section("jules_state_actions")
    for state, action in actions.items():
        if str(action).lower() not in allowed_actions:
            raise ConfigError(f"Invalid jules_state_actions.{state}={action}")
    if str(actions.get("COMPLETED", "review")).lower() not in {"review", "block"}:
        raise ConfigError("COMPLETED may only be 'review' or 'block' in V1")
    critic_name = str(config.get("workflow.critic_role", "critic"))
    critic = role_config(config, critic_name)
    if critic.get("transport") == "direct" and not bool(critic.get("read_only", False)):
        raise ConfigError("Direct critic role must set read_only = true")


def role_config(config: Config, role_name: str) -> dict[str, Any]:
    roles = config.section("roles")
    role = roles.get(role_name)
    if not isinstance(role, dict):
        raise ConfigError(f"Missing role config [roles.{role_name}]")
    return role


def executable(config: Config, key: str, fallback: str) -> str:
    value = str(config.get(f"executables.{key}", fallback))
    value = expand(value)
    if os.path.sep in value:
        return value
    resolved = shutil.which(value)
    return resolved or value


def risk_matches(config: Config, contract: dict[str, Any]) -> bool:
    tags = set(map(str, contract.get("risk_tags") or []))
    configured = set(map(str, config.get("workflow.high_risk_tags", [])))
    return bool(tags & configured)


def critic_needed(config: Config, when: str, contract: dict[str, Any]) -> bool:
    policy = str(config.get(f"workflow.{when}_critic", "never")).lower()
    if policy == "always":
        return True
    if policy == "never":
        return False
    if policy == "high_risk":
        return risk_matches(config, contract)
    raise ConfigError(f"Unknown critic policy: {policy}")

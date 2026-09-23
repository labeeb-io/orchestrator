"""System and provider diagnostic checks (`doctor`)."""
from __future__ import annotations

import pathlib
import shutil
import sys
from typing import Any

from labeeb.config import Config, executable, role_config
from labeeb.models import VERSION
from labeeb.providers.base import run_cmd


def doctor(config: Config) -> dict[str, Any]:
    """Inspect environment, executables, and provider configuration."""
    checks: dict[str, Any] = {
        "version": VERSION,
        "python": sys.version.split()[0],
        "config": str(config.path),
        "executables": {},
        "state_root": str(pathlib.Path(config.get("controller.state_root", "~/.local/state/labeeb-controller")).expanduser()),
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
            critic_exe = str(pathlib.Path(role["command"][0]).expanduser())
            critic_check["executable"] = shutil.which(critic_exe) or critic_exe
            critic_check["executable_found"] = bool(shutil.which(critic_exe) or pathlib.Path(critic_exe).exists())
        checks["critic"] = critic_check
    except Exception as exc:
        checks["critic"] = {"error": str(exc)}
    try:
        implementer_name = str(config.get("workflow.implementer_role", "implementer"))
        implementer = role_config(config, implementer_name)
        impl_exe = str(pathlib.Path(implementer.get("command", config.get("executables.cjules", "cjules"))).expanduser())
        checks["implementer"] = {
            "role": implementer_name,
            "transport": implementer.get("transport"),
            "executable": shutil.which(impl_exe) or impl_exe,
            "executable_found": bool(shutil.which(impl_exe) or pathlib.Path(impl_exe).exists()),
        }
    except Exception as exc:
        checks["implementer"] = {"error": str(exc)}
    return checks

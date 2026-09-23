"""Workspace inspection and project presets REST API routes."""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import subprocess
from typing import Any
from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

DEFAULT_PRESETS = [
    {
        "id": "labeeb2025",
        "name": "labeeb2025 (Core Platform)",
        "path": str(pathlib.Path.home() / "webserver" / "server" / "www" / "labeeb2025"),
        "repo": "labeeb-io/labeeb",
        "default_branch": "master",
        "allowed_paths": ["app", "routes", "tests", "src"],
        "validation_commands": ["true"],
    },
    {
        "id": "labeeb-orchestrator",
        "name": "labeeb-orchestrator (Control Plane)",
        "path": str(pathlib.Path.home() / "webserver" / "server" / "www" / "labeeb-orchestrator"),
        "repo": "labeeb-io/orchestrator",
        "default_branch": "main",
        "allowed_paths": ["labeeb", "tests"],
        "validation_commands": [".venv/bin/python -m pytest tests/"],
    },
]


def _parse_github_repo(remote_url: str) -> str:
    cleaned = remote_url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    # Match git@github.com:owner/repo or https://github.com/owner/repo
    m = re.search(r"github\.com[:/]([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", cleaned)
    if m:
        return m.group(1)
    # Generic fallback for any host:owner/repo
    m = re.search(r"[:/]([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)$", cleaned)
    if m:
        return m.group(1)
    return ""


def _inspect_git_sync(target_path: pathlib.Path) -> dict[str, Any]:
    git_bin = "git"
    result: dict[str, Any] = {
        "valid": False,
        "path": str(target_path),
        "repo": "",
        "default_branch": "master",
        "branches": [],
        "jules_authorized": None,
        "jules_sources": [],
        "error": None,
    }

    if not target_path.exists():
        result["error"] = f"Directory not found: {target_path}"
        return result

    if not target_path.is_dir():
        result["error"] = f"Path is not a directory: {target_path}"
        return result

    git_dir = target_path / ".git"
    if not git_dir.exists():
        result["error"] = f"Not a git repository: {target_path}"
        return result

    try:
        # 1. Query remote origin URL
        proc = subprocess.run(
            [git_bin, "config", "--get", "remote.origin.url"],
            cwd=str(target_path),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            result["repo"] = _parse_github_repo(proc.stdout.strip())

        # 2. Query current branch
        proc_branch = subprocess.run(
            [git_bin, "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(target_path),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        cur_branch = proc_branch.stdout.strip() if proc_branch.returncode == 0 else ""
        if cur_branch and cur_branch != "HEAD":
            result["default_branch"] = cur_branch

        # 3. Query all local and remote branch names
        proc_list = subprocess.run(
            [git_bin, "branch", "-a", "--format=%(refname:short)"],
            cwd=str(target_path),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        branches_set: set[str] = set()
        if cur_branch and cur_branch != "HEAD":
            branches_set.add(cur_branch)

        if proc_list.returncode == 0:
            for line in proc_list.stdout.splitlines():
                b = line.strip()
                if not b or "->" in b or b == "HEAD":
                    continue
                if b.startswith("origin/"):
                    b = b[7:]
                if b:
                    branches_set.add(b)

        # Ensure sensible defaults if list is empty
        if not branches_set:
            branches_set.add("master")
            branches_set.add("main")

        # Sort branches with default_branch first
        branches_sorted = sorted(branches_set)
        if result["default_branch"] in branches_sorted:
            branches_sorted.remove(result["default_branch"])
            branches_sorted.insert(0, result["default_branch"])

        result["branches"] = branches_sorted
        result["valid"] = True

        if result["repo"]:
            try:
                from labeeb.config import load_config
                from labeeb.providers.jules import JulesProvider
                cfg = load_config()
                jp = JulesProvider(cfg)
                is_avail, sources = jp.is_repo_available(result["repo"])
                result["jules_sources"] = sources
                result["jules_authorized"] = is_avail if sources else None
            except Exception:
                result["jules_sources"] = []
                result["jules_authorized"] = None

        return result

    except Exception as exc:
        result["error"] = f"Git inspection failed: {exc}"
        return result


@router.get("/presets")
async def get_workspace_presets():
    """Return configured/known workspace presets."""
    return DEFAULT_PRESETS


@router.get("/inspect")
async def inspect_workspace(path: str = Query(..., description="Local workspace directory to inspect")):
    """Inspect a local workspace path for git repository information and branches."""
    expanded = pathlib.Path(os.path.expanduser(path.strip())).resolve()
    return await asyncio.to_thread(_inspect_git_sync, expanded)


@router.get("/jules/sources")
async def get_jules_sources():
    """Return all GitHub repository sources authorized in Google Jules."""
    def _fetch():
        try:
            from labeeb.config import load_config
            from labeeb.providers.jules import JulesProvider
            cfg = load_config()
            jp = JulesProvider(cfg)
            sources = jp.list_sources()
            return {"sources": sources, "count": len(sources)}
        except Exception as exc:
            return {"sources": [], "count": 0, "error": str(exc)}

    return await asyncio.to_thread(_fetch)

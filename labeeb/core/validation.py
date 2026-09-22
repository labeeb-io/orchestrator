"""Isolated deterministic validation in temporary git worktrees."""
from __future__ import annotations

import pathlib
import tempfile
import time
from typing import Any

from labeeb.config import Config
from labeeb.errors import CommandError
from labeeb.models import sha256_text, utc_now
from labeeb.providers.base import run_cmd
from labeeb.providers.git import GitProvider
from labeeb.providers.jules import (
    activity_key,
    activity_text,
    changed_paths_from_patch,
    ordered_activities,
    patch_candidates,
    path_allowed,
)
from labeeb.storage.goal_store import GoalPaths, GoalStore, read_ref_json, ref_path


def prepare_review_evidence(
    state: dict[str, Any],
    event: dict[str, Any],
    session: dict[str, Any],
    logs: dict[str, Any],
    paths: GoalPaths,
    store: GoalStore,
) -> dict[str, Any]:
    activities = [x for x in (logs.get("activities") or []) if isinstance(x, dict)]
    relevant = activities
    if state.get("repair_reserved") and state.get("round_anchor_ref"):
        old = set(read_ref_json(state["round_anchor_ref"]).get("activity_keys") or [])
        relevant = [a for a in activities if activity_key(a) not in old]
    patches = patch_candidates(relevant)
    chosen = patches[-1] if patches else None
    patch_ref = None
    patch_hash = None
    changed_paths: list[str] = []
    base_commit = None
    if chosen:
        patch_hash = sha256_text(chosen["patch"])
        base_commit = chosen.get("base_commit")
        patch_path = paths.evidence / f"patch-{patch_hash[:16]}.diff"
        patch_ref = store.write_text(patch_path, chosen["patch"])
        changed_paths = changed_paths_from_patch(chosen["patch"])
    contract = read_ref_json(state["contract_ref"])
    allowed = list(contract.get("allowed_paths") or [])
    out_of_scope = [p for p in changed_paths if not path_allowed(p, allowed)]
    evidence = {
        "event": event,
        "jules_session": session,
        "activity_keys": [activity_key(a) for a in relevant],
        "latest_agent_messages": [activity_text(a) for a in ordered_activities(relevant) if a.get("agentMessaged")][-3:],
        "patch_ref": patch_ref,
        "patch_hash": patch_hash,
        "base_commit": base_commit,
        "changed_paths": changed_paths,
        "out_of_scope_paths": out_of_scope,
        "validation": {"status": "NOT_RUN"},
        "captured_at": utc_now(),
    }
    return evidence


def validate_evidence(
    state: dict[str, Any],
    evidence: dict[str, Any],
    config: Config,
    paths: GoalPaths,
    git: GitProvider,
) -> dict[str, Any]:
    contract = read_ref_json(state["contract_ref"])
    commands = list(contract.get("validation_commands") or [])
    patch_ref = evidence.get("patch_ref")
    base_commit = evidence.get("base_commit")
    if evidence.get("out_of_scope_paths"):
        evidence["validation"] = {
            "status": "FAIL",
            "reason": "Patch contains paths outside allowed scope",
            "paths": evidence["out_of_scope_paths"],
        }
        return evidence
    if bool(config.get("safety.require_patch_for_implementation", True)) and not patch_ref:
        evidence["validation"] = {"status": "FAIL", "reason": "No git patch artifact found for implementation result"}
        return evidence
    if not commands:
        if bool(config.get("safety.require_validation_for_pass", True)):
            evidence["validation"] = {"status": "BLOCKED", "reason": "No deterministic validation commands defined"}
        else:
            evidence["validation"] = {"status": "SKIPPED", "reason": "No validation commands defined"}
        return evidence
    if not patch_ref or not base_commit:
        evidence["validation"] = {"status": "BLOCKED", "reason": "Patch or base commit missing; cannot build isolated validation worktree"}
        return evidence

    workspace = pathlib.Path(contract["workspace"])
    validation_root = paths.evidence / "validation-worktrees"
    validation_root.mkdir(parents=True, exist_ok=True)
    worktree = pathlib.Path(tempfile.mkdtemp(prefix="run-", dir=validation_root))
    # git worktree requires target directory not to exist before addition
    worktree.rmdir()
    results: list[dict[str, Any]] = []
    try:
        git.create_worktree(workspace, worktree, str(base_commit))
        patch_path = ref_path(str(patch_ref))
        git.check_patch(worktree, patch_path)
        git.apply_patch(worktree, patch_path)
        timeout = float(config.get("timeouts.validation_command_seconds", 900))
        shell = str(config.get("executables.shell", "/bin/bash"))
        for command in commands:
            started = time.monotonic()
            result = run_cmd([shell, "-lc", command], cwd=str(worktree), timeout=timeout, check=False)
            results.append(
                {
                    "command": command,
                    "exit_code": result.rc,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "stdout_tail": result.stdout[-12000:],
                    "stderr_tail": result.stderr[-12000:],
                }
            )
            if result.rc != 0:
                evidence["validation"] = {"status": "FAIL", "commands": results, "worktree": str(worktree)}
                return evidence
        evidence["validation"] = {"status": "PASS", "commands": results, "worktree": str(worktree)}
        return evidence
    except CommandError as exc:
        evidence["validation"] = {
            "status": "BLOCKED",
            "reason": str(exc),
            "stdout_tail": exc.stdout[-8000:],
            "stderr_tail": exc.stderr[-8000:],
        }
        return evidence
    finally:
        keep = bool(config.get("controller.keep_validation_worktrees", False))
        if worktree.exists() and not keep:
            git.remove_worktree(workspace, worktree)

"""Isolated deterministic validation in temporary git worktrees."""
from __future__ import annotations

import pathlib
import tempfile
import time
from typing import Any

from labeeb.config import Config
from labeeb.core.events import materialization_response_activities
from labeeb.errors import CommandError
from labeeb.models import (
    ArtifactValidity,
    PathIntegrityStatus,
    sha256_text,
    utc_now,
)
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
    old_patch_hashes: set[str] = set()
    if state.get("materialization_anchor_ref"):
        anchor = read_ref_json(state["materialization_anchor_ref"])
        relevant = materialization_response_activities(logs, anchor)
        old_patch_hashes = set(anchor.get("patch_hashes") or [])
    elif state.get("repair_reserved") and state.get("round_anchor_ref"):
        old = set(read_ref_json(state["round_anchor_ref"]).get("activity_keys") or [])
        relevant = [a for a in activities if activity_key(a) not in old]
    patches = [p for p in patch_candidates(relevant) if sha256_text(p["patch"]) not in old_patch_hashes]
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
        evidence["goal_proof"] = {"proof_passed": False, "reason": "Patch contains paths outside allowed scope"}
        return evidence
    if bool(config.get("safety.require_patch_for_implementation", True)) and not patch_ref:
        evidence["validation"] = {"status": "FAIL", "reason": "No git patch artifact found for implementation result"}
        evidence["goal_proof"] = {"proof_passed": False, "reason": "No git patch artifact found for implementation result"}
        return evidence
    if not commands:
        if bool(config.get("safety.require_validation_for_pass", True)):
            evidence["validation"] = {"status": "BLOCKED", "reason": "No deterministic validation commands defined"}
        else:
            evidence["validation"] = {"status": "SKIPPED", "reason": "No validation commands defined"}
        evidence["goal_proof"] = {"proof_passed": False, "reason": "No deterministic validation commands defined"}
        return evidence
    if not patch_ref or not base_commit:
        evidence["validation"] = {"status": "BLOCKED", "reason": "Patch or base commit missing; cannot build isolated validation worktree"}
        evidence["goal_proof"] = {"proof_passed": False, "reason": "Patch or base commit missing; cannot build isolated validation worktree"}
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
                evidence["goal_proof"] = {"proof_passed": False, "reason": f"Validation command failed: {command}"}
                return evidence
        evidence["validation"] = {"status": "PASS", "commands": results, "worktree": str(worktree)}

        # Phase 4 Goal Proof: Execute the locked proof path in the isolated worktree
        path_integrity = str(state.get("path_integrity_status") or PathIntegrityStatus.ORIGINAL)
        proof_contract_data = None
        proof_art = (state.get("artifacts") or {}).get("proof_contract")
        proof_invalid_reason = None
        if state.get("proof_path_locked") and state.get("locked_proof_contract_ref"):
            locked_ref = state["locked_proof_contract_ref"]
            if isinstance(proof_art, dict) and proof_art.get("ref") and proof_art["ref"] != locked_ref:
                if path_integrity == PathIntegrityStatus.ALTERNATE_DIAGNOSTIC_ONLY:
                    proof_invalid_reason = "Proof path was replaced with diagnostic-only alternate exploration"
                else:
                    proof_invalid_reason = f"Proof contract artifact ({proof_art['ref']}) diverges from locked original proof ref ({locked_ref})"
            else:
                try:
                    proof_contract_data = read_ref_json(locked_ref).get("data", {})
                except Exception as exc:
                    proof_invalid_reason = f"Failed to read locked proof contract: {exc}"
        elif isinstance(proof_art, dict):
            if proof_art.get("validity") == ArtifactValidity.VALID:
                proof_contract_data = read_ref_json(proof_art["ref"]).get("data", {})
            else:
                proof_invalid_reason = "Proof contract artifact is STALE or invalid"
        if proof_contract_data is None and not proof_invalid_reason:
            proof_contract_data = contract.get("proof_contract")
            if proof_contract_data is None and not (state.get("reasoning_graph_active") or state.get("proof_path_locked")):
                val_cmds = contract.get("validation_commands") or []
                if val_cmds:
                    proof_contract_data = {"entrypoint": val_cmds[0]}

        if proof_invalid_reason:
            evidence["goal_proof"] = {
                "proof_passed": False,
                "reason": proof_invalid_reason,
                "path_integrity_status": path_integrity,
            }
        elif not isinstance(proof_contract_data, dict):
            evidence["goal_proof"] = {
                "proof_passed": False,
                "reason": "Missing locked proof contract",
                "path_integrity_status": path_integrity,
            }
        elif path_integrity == PathIntegrityStatus.ALTERNATE_DIAGNOSTIC_ONLY:
            evidence["goal_proof"] = {
                "proof_passed": False,
                "reason": "Alternate diagnostic proof path cannot produce terminal PASS",
                "path_integrity_status": path_integrity,
                "entrypoint": proof_contract_data.get("entrypoint"),
            }
        else:
            entrypoint = str(proof_contract_data.get("entrypoint") or "").strip()
            completion_probe = str(proof_contract_data.get("completion_probe") or "").strip()
            if not entrypoint:
                evidence["goal_proof"] = {
                    "proof_passed": False,
                    "reason": "Proof contract has no verifiable entrypoint defined",
                    "path_integrity_status": path_integrity,
                }
            else:
                p_started = time.monotonic()
                res_entry = run_cmd([shell, "-lc", entrypoint], cwd=str(worktree), timeout=timeout, check=False)
                entry_dur = round(time.monotonic() - p_started, 3)
                entry_ok = (res_entry.rc == 0)

                probe_ok = True
                probe_res = None
                if completion_probe:
                    pb_started = time.monotonic()
                    res_probe = run_cmd([shell, "-lc", completion_probe], cwd=str(worktree), timeout=timeout, check=False)
                    pb_dur = round(time.monotonic() - pb_started, 3)
                    probe_ok = (res_probe.rc == 0)
                    probe_res = {
                        "command": completion_probe,
                        "exit_code": res_probe.rc,
                        "duration_seconds": pb_dur,
                        "stdout_tail": res_probe.stdout[-8000:],
                        "stderr_tail": res_probe.stderr[-8000:],
                    }

                proof_passed = bool(entry_ok and probe_ok and evidence.get("validation", {}).get("status") == "PASS")
                evidence["goal_proof"] = {
                    "entrypoint": entrypoint,
                    "completion_probe": completion_probe or None,
                    "entrypoint_exit_code": res_entry.rc,
                    "entrypoint_duration_seconds": entry_dur,
                    "entrypoint_stdout": res_entry.stdout[-12000:],
                    "entrypoint_stderr": res_entry.stderr[-12000:],
                    "completion_probe_result": probe_res,
                    "proof_passed": proof_passed,
                    "path_integrity_status": path_integrity,
                    "reason": "" if proof_passed else ("Entrypoint command failed" if not entry_ok else "Completion probe failed"),
                }
        return evidence
    except CommandError as exc:
        evidence["validation"] = {
            "status": "BLOCKED",
            "reason": str(exc),
            "stdout_tail": exc.stdout[-8000:],
            "stderr_tail": exc.stderr[-8000:],
        }
        evidence["goal_proof"] = {
            "proof_passed": False,
            "reason": f"Command execution blocked: {exc}",
        }
        return evidence
    finally:
        keep = bool(config.get("controller.keep_validation_worktrees", False))
        if worktree.exists() and not keep:
            git.remove_worktree(workspace, worktree)

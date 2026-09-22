"""Subprocess execution helpers, JSON parsing, and abstract provider protocols."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any, Protocol

from labeeb.errors import CommandError, ControllerError
from labeeb.models import CmdResult


def run_cmd(
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> CmdResult:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        cp = subprocess.run(
            cmd,
            cwd=cwd,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=merged,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(
            f"Command timed out after {timeout}s",
            cmd=cmd,
            rc=124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        ) from exc
    result = CmdResult(cmd=cmd, rc=cp.returncode, stdout=cp.stdout, stderr=cp.stderr)
    if check and result.rc != 0:
        raise CommandError(
            f"Command failed ({result.rc}): {shlex.join(cmd)}",
            cmd=cmd,
            rc=result.rc,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result


def parse_json_stdout(result: CmdResult) -> Any:
    text = result.stdout.strip()
    if not text:
        raise ControllerError(f"Expected JSON but stdout was empty: {shlex.join(result.cmd)}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # Tolerate one JSON object on the last non-empty line.
        for line in reversed([x.strip() for x in text.splitlines() if x.strip()]):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        raise ControllerError(f"Malformed JSON from {shlex.join(result.cmd)}: {text[:500]}") from exc


def extract_enveloped_json(text: str, start: str, end: str) -> dict[str, Any]:
    s = text.find(start)
    e = text.find(end, s + len(start)) if s >= 0 else -1
    if s < 0 or e < 0:
        raise ControllerError(f"Missing structured response envelope {start} ... {end}")
    body = text[s + len(start) : e].strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ControllerError(f"Invalid structured JSON between {start}/{end}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ControllerError("Structured response must be a JSON object")
    return payload


class BrainProviderProtocol(Protocol):
    def launch(self, role_name: str, prompt: str, task_name: str, workspace: str) -> dict[str, Any]: ...
    def resume(self, role_name: str, source_task: str, prompt: str, task_name: str) -> dict[str, Any]: ...
    def wait_task(self, task_id: str, stop_checker: Any = None) -> dict[str, Any]: ...
    def find_by_name(self, name: str) -> dict[str, Any] | None: ...


class ImplementerProviderProtocol(Protocol):
    def create_session(self, repo: str, branch: str, marker: str, prompt: str, require_approval: bool) -> dict[str, Any]: ...
    def send_message(self, session_id: str, message: str) -> dict[str, Any]: ...
    def approve_plan(self, session_id: str) -> dict[str, Any]: ...
    def get_session(self, session_id: str, check: bool = True) -> dict[str, Any] | None: ...
    def get_logs(self, session_id: str, check: bool = True) -> dict[str, Any] | None: ...
    def find_sessions(self, marker: str, repo: str | None = None, branch: str | None = None) -> list[dict[str, Any]]: ...


class CriticProviderProtocol(Protocol):
    def review(self, prompt: str, workspace: str, op_id: str) -> dict[str, Any]: ...


class GitProviderProtocol(Protocol):
    def create_worktree(self, workspace: str, worktree_dir: str, base_commit: str) -> None: ...
    def apply_patch(self, worktree_dir: str, patch_path: str) -> None: ...
    def remove_worktree(self, workspace: str, worktree_dir: str) -> None: ...

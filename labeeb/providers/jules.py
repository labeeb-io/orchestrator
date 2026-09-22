"""Provider adapter for Google Jules via cjules CLI."""
from __future__ import annotations

import re
import shutil
import time
from typing import Any, Iterable

from labeeb.config import Config, expand, role_config
from labeeb.errors import ControllerError
from labeeb.models import canonical_json, sha256_text
from labeeb.providers.base import parse_json_stdout, run_cmd


def session_id(session: dict[str, Any]) -> str:
    value = session.get("id") or session.get("name")
    if not value:
        raise ControllerError("Jules session JSON has no id/name")
    return str(value).split("/")[-1]


def repo_from_session(session: dict[str, Any]) -> str:
    source = (((session.get("sourceContext") or {}).get("source")) or "")
    m = re.match(r"^sources/github/(.+)$", str(source))
    return m.group(1) if m else ""


def branch_from_session(session: dict[str, Any]) -> str:
    return str((((session.get("sourceContext") or {}).get("githubRepoContext") or {}).get("startingBranch")) or "")


def activity_key(activity: dict[str, Any]) -> str:
    if activity.get("id"):
        return f"id:{activity['id']}"
    return "fp:" + sha256_text(canonical_json(activity))


def activity_time(activity: dict[str, Any]) -> str:
    return str(activity.get("createTime") or "")


def ordered_activities(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decorated = [(activity_time(a), i, a) for i, a in enumerate(activities)]
    return [a for _, _, a in sorted(decorated, key=lambda x: (x[0], x[1]))]


def activity_text(activity: dict[str, Any]) -> str:
    for parent, child in (
        ("userMessaged", "userMessage"),
        ("agentMessaged", "agentMessage"),
        ("progressUpdated", "description"),
        ("sessionFailed", "reason"),
    ):
        value = activity.get(parent)
        if isinstance(value, dict) and value.get(child):
            return str(value[child])
    return str(activity.get("description") or "")


def latest_plan(activities: list[dict[str, Any]]) -> dict[str, Any] | None:
    for activity in reversed(ordered_activities(activities)):
        pg = activity.get("planGenerated")
        if isinstance(pg, dict) and isinstance(pg.get("plan"), dict):
            return pg["plan"]
    return None


def patch_candidates(activities: list[dict[str, Any]], only_keys: set[str] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for activity in ordered_activities(activities):
        key = activity_key(activity)
        if only_keys is not None and key not in only_keys:
            continue
        for artifact in activity.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            cs = artifact.get("changeSet")
            if not isinstance(cs, dict):
                continue
            gp = cs.get("gitPatch")
            if not isinstance(gp, dict):
                continue
            patch = gp.get("unidiffPatch")
            if patch:
                out.append(
                    {
                        "activity_key": key,
                        "activity_create_time": activity_time(activity),
                        "base_commit": gp.get("baseCommitId"),
                        "patch": str(patch),
                        "suggested_commit_message": gp.get("suggestedCommitMessage"),
                    }
                )
    return out


def changed_paths_from_patch(patch: str) -> list[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            raw = line[4:].strip()
            if raw == "/dev/null":
                continue
            if raw.startswith("a/") or raw.startswith("b/"):
                raw = raw[2:]
            paths.add(raw)
    return sorted(paths)


def path_allowed(path: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    path = path.lstrip("./")
    for prefix in allowed:
        p = prefix.lstrip("./").rstrip("/")
        if path == p or path.startswith(p + "/"):
            return True
    return False


def has_user_message_marker(activities: Iterable[dict[str, Any]], marker: str) -> bool:
    for activity in activities:
        if isinstance(activity.get("userMessaged"), dict) and marker in activity_text(activity):
            return True
    return False


class JulesProvider:
    def __init__(self, config: Config):
        self.config = config
        implementer_name = str(config.get("workflow.implementer_role", "implementer"))
        implementer = role_config(config, implementer_name)
        if implementer.get("transport", "jules") != "jules":
            raise ControllerError(f"V1 implementer role {implementer_name} must use transport='jules'")
        impl_command = str(implementer.get("command", config.get("executables.cjules", "cjules")))
        impl_command = expand(impl_command)
        self.cjules = shutil.which(impl_command) or impl_command

    def create_session(
        self,
        repo: str,
        branch: str,
        marker: str,
        prompt: str,
        require_approval: bool = False,
    ) -> dict[str, Any]:
        cmd = [
            self.cjules,
            "new",
            "-",
            "--repo",
            repo,
            "--branch",
            branch,
            "--title",
            marker,
            "--no-reconcile-on-error",
            "-f",
            "json",
        ]
        if require_approval:
            cmd.append("--require-approval")
        result = run_cmd(cmd, stdin=prompt, timeout=float(self.config.get("timeouts.jules_write_seconds", 120)))
        session = parse_json_stdout(result)
        if isinstance(session, list):
            if len(session) != 1:
                raise ControllerError(f"Expected one Jules session, got {len(session)}")
            session = session[0]
        if not isinstance(session, dict):
            raise ControllerError("Unexpected cjules new JSON")
        return session

    def send_message(self, session_id_val: str, message: str) -> dict[str, Any]:
        cmd = [self.cjules, "msg", session_id_val, "-"]
        result = run_cmd(cmd, stdin=message, timeout=float(self.config.get("timeouts.jules_write_seconds", 120)))
        return {"stdout": result.stdout.strip()}

    def approve_plan(self, session_id_val: str) -> dict[str, Any]:
        cmd = [self.cjules, "approve", session_id_val]
        result = run_cmd(cmd, timeout=float(self.config.get("timeouts.jules_write_seconds", 120)))
        return {"stdout": result.stdout.strip()}

    def get_session(self, session_id_val: str, check: bool = True) -> dict[str, Any] | None:
        result = run_cmd(
            [self.cjules, "get", session_id_val, "-f", "json"],
            timeout=float(self.config.get("timeouts.jules_read_seconds", 60)),
            check=check,
        )
        if result.rc != 0:
            return None
        payload = parse_json_stdout(result)
        return payload if isinstance(payload, dict) else None

    def get_logs(self, session_id_val: str, check: bool = True) -> dict[str, Any] | None:
        result = run_cmd(
            [self.cjules, "logs", session_id_val, "-f", "json"],
            timeout=float(self.config.get("timeouts.jules_read_seconds", 90)),
            check=check,
        )
        if result.rc != 0:
            return None
        payload = parse_json_stdout(result)
        return payload if isinstance(payload, dict) else None

    def find_sessions(self, marker: str, repo: str | None = None, branch: str | None = None) -> list[dict[str, Any]]:
        if not marker or not repo or not branch:
            return []
        attempts = int(self.config.get("controller.reconcile_attempts", 3))
        delay = float(self.config.get("controller.reconcile_delay_seconds", 5))
        for i in range(attempts):
            result = run_cmd(
                [self.cjules, "ls", "--all", "--search", marker, "-f", "json"],
                timeout=float(self.config.get("timeouts.jules_read_seconds", 60)),
                check=False,
            )
            if result.rc == 0:
                payload = parse_json_stdout(result)
                if isinstance(payload, list):
                    matches = []
                    for sess in payload:
                        if not isinstance(sess, dict):
                            continue
                        if sess.get("title") != marker and marker not in str(sess.get("prompt") or ""):
                            continue
                        if repo_from_session(sess) != repo:
                            continue
                        if branch_from_session(sess) != branch:
                            continue
                        matches.append(sess)
                    if matches:
                        return matches
            if i + 1 < attempts:
                time.sleep(delay)
        return []

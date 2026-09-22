"""Atomic file persistence and goal storage management."""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import pathlib
import tempfile
from typing import Any

from labeeb.errors import ControllerError
from labeeb.models import sha256_bytes, utc_now
from labeeb.storage.locking import GoalLock


@dataclasses.dataclass
class GoalPaths:
    root: pathlib.Path

    @property
    def state(self) -> pathlib.Path:
        return self.root / "state.json"

    @property
    def lock(self) -> pathlib.Path:
        return self.root / "goal.lock"

    @property
    def contract(self) -> pathlib.Path:
        return self.root / "contract.json"

    @property
    def plan(self) -> pathlib.Path:
        return self.root / "plan.json"

    @property
    def controller_log(self) -> pathlib.Path:
        return self.root / "controller.log"

    @property
    def requests(self) -> pathlib.Path:
        return self.root / "requests"

    @property
    def reviews(self) -> pathlib.Path:
        return self.root / "reviews"

    @property
    def evidence(self) -> pathlib.Path:
        return self.root / "evidence"

    @property
    def results(self) -> pathlib.Path:
        return self.root / "result.json"

    @property
    def stop_request(self) -> pathlib.Path:
        return self.root / "STOP"

    @property
    def artifacts(self) -> pathlib.Path:
        return self.root / "artifacts"

    @property
    def events(self) -> pathlib.Path:
        return self.root / "events.jsonl"

    @property
    def final_report_md(self) -> pathlib.Path:
        return self.root / "final_report.md"

    @property
    def final_report_json(self) -> pathlib.Path:
        return self.root / "final_report.json"


def file_ref(path: pathlib.Path) -> str:
    return f"file:{path.resolve()}#sha256={sha256_bytes(path.read_bytes())}"


def ref_path(ref: str) -> pathlib.Path:
    if not ref.startswith("file:"):
        raise ControllerError(f"Unsupported ref: {ref}")
    return pathlib.Path(ref[5:].split("#sha256=", 1)[0])


def read_ref_text(ref: str) -> str:
    path = ref_path(ref)
    data = path.read_bytes()
    if "#sha256=" in ref:
        expected = ref.rsplit("#sha256=", 1)[1]
        actual = sha256_bytes(data)
        if expected != actual:
            raise ControllerError(f"Immutable ref changed: {path}")
    return data.decode("utf-8")


def read_ref_json(ref: str) -> Any:
    return json.loads(read_ref_text(ref))


def atomic_text_write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_json_write(path: pathlib.Path, payload: Any) -> None:
    atomic_text_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def migrate_state_v1_to_v2(state: dict[str, Any]) -> dict[str, Any]:
    """Hydrate state with V2 fields if missing, without fabricating artifacts."""
    if "artifacts" not in state:
        state["artifacts"] = {}
    if "macro_phase" not in state:
        state["macro_phase"] = state.get("phase", "CREATED")
    if "execution_rounds" not in state:
        state["execution_rounds"] = 0
    if "proof_path_locked" not in state:
        state["proof_path_locked"] = False
    if "path_integrity_status" not in state:
        state["path_integrity_status"] = "ORIGINAL"
    if "reasoning_history" not in state:
        state["reasoning_history"] = []
    return state


class GoalStore:
    def __init__(self, paths: GoalPaths):
        self.paths = paths
        self.lock = GoalLock(paths.lock)

    def init_dirs(self) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        for p in (self.paths.requests, self.paths.reviews, self.paths.evidence, self.paths.artifacts):
            p.mkdir(parents=True, exist_ok=True)
        self.paths.lock.touch(exist_ok=True)

    @contextlib.contextmanager
    def locked(self):
        self.init_dirs()
        with self.lock.locked():
            yield

    def load(self, migrate: bool = True) -> dict[str, Any]:
        if not self.paths.state.exists():
            raise ControllerError(f"Missing state: {self.paths.state}")
        state = json.loads(self.paths.state.read_text())
        if migrate:
            state = migrate_state_v1_to_v2(state)
        return state

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        atomic_json_write(self.paths.state, state)

    def write_json(self, path: pathlib.Path, payload: Any) -> str:
        atomic_json_write(path, payload)
        return file_ref(path)

    def write_text(self, path: pathlib.Path, content: str) -> str:
        atomic_text_write(path, content)
        return file_ref(path)

    def append_log(self, message: str) -> None:
        self.init_dirs()
        line = f"{utc_now()} {message}\n"
        with self.paths.controller_log.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

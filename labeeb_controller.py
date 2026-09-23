#!/usr/bin/env python3
"""Labeeb Orchestrator V1 - CLI and backward-compatibility entry point.

This module exposes the backward-compatible interface of labeeb_controller
while delegating core domain logic, storage, providers, and API to the
modular `labeeb` package.
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from labeeb.cli.main import build_parser, load_intent, main
from labeeb.config import (
    Config,
    critic_needed,
    executable,
    expand,
    load_config,
    risk_matches,
    role_config,
    validate_config,
)
from labeeb.core.controller import LabeebController, doctor
from labeeb.core.effects import EffectManager
from labeeb.core.events import (
    EventBroadcaster,
    global_event_bus,
    jules_snapshot,
    meaningful_event,
    reserve_event_key,
)
from labeeb.core.state_machine import (
    convergence_prompt,
    critic_prompt,
    event_brain_prompt,
    initial_brain_prompt,
)
from labeeb.core.validation import prepare_review_evidence, validate_evidence
from labeeb.errors import (
    AmbiguousEffect,
    CommandError,
    ConfigError,
    ControllerError,
    GoalNotFoundError,
)
from labeeb.models import (
    CRITIC_END,
    CRITIC_START,
    DECISION_END,
    DECISION_START,
    JULES_TERMINAL_STATES,
    JULES_WAIT_STATES,
    TERMINAL_PHASES,
    VERSION,
    CmdResult,
    canonical_json,
    deadline_after,
    format_json_for_prompt,
    new_operation_id,
    parse_utc,
    safe_name,
    sha256_bytes,
    sha256_text,
    task_name,
    utc_now,
)
from labeeb.providers.base import (
    BrainProviderProtocol,
    CriticProviderProtocol,
    GitProviderProtocol,
    ImplementerProviderProtocol,
    extract_enveloped_json,
    parse_json_stdout,
    run_cmd,
)
from labeeb.providers.claude import ClaudeCriticProvider
from labeeb.providers.git import GitProvider
from labeeb.providers.jules import (
    JulesProvider,
    activity_key,
    activity_text,
    activity_time,
    branch_from_session,
    changed_paths_from_patch,
    has_user_message_marker,
    latest_plan,
    ordered_activities,
    patch_candidates,
    path_allowed,
    repo_from_session,
    session_id,
)
from labeeb.providers.orchestrator import OrchestratorProvider
from labeeb.storage.goal_store import (
    GoalPaths,
    GoalStore,
    atomic_json_write,
    atomic_text_write,
    file_ref,
    read_ref_json,
    read_ref_text,
    ref_path,
)
from labeeb.storage.locking import GoalLock

__all__ = [
    "VERSION",
    "TERMINAL_PHASES",
    "JULES_WAIT_STATES",
    "JULES_TERMINAL_STATES",
    "DECISION_START",
    "DECISION_END",
    "CRITIC_START",
    "CRITIC_END",
    "ControllerError",
    "AmbiguousEffect",
    "CommandError",
    "ConfigError",
    "GoalNotFoundError",
    "CmdResult",
    "Config",
    "GoalPaths",
    "GoalLock",
    "GoalStore",
    "utc_now",
    "parse_utc",
    "deadline_after",
    "sha256_text",
    "sha256_bytes",
    "canonical_json",
    "file_ref",
    "ref_path",
    "read_ref_text",
    "read_ref_json",
    "atomic_text_write",
    "atomic_json_write",
    "expand",
    "load_config",
    "validate_config",
    "executable",
    "run_cmd",
    "parse_json_stdout",
    "extract_enveloped_json",
    "safe_name",
    "new_operation_id",
    "task_name",
    "activity_key",
    "activity_time",
    "ordered_activities",
    "activity_text",
    "latest_plan",
    "patch_candidates",
    "changed_paths_from_patch",
    "path_allowed",
    "role_config",
    "risk_matches",
    "critic_needed",
    "format_json_for_prompt",
    "session_id",
    "repo_from_session",
    "branch_from_session",
    "has_user_message_marker",
    "doctor",
    "LabeebController",
    "build_parser",
    "load_intent",
    "main",
]

if __name__ == "__main__":
    import os
    import pathlib
    venv_python = pathlib.Path(__file__).resolve().parent / ".venv" / "bin" / "python3"
    if venv_python.exists() and pathlib.Path(sys.prefix).resolve() != venv_python.parent.parent.resolve():
        os.execv(str(venv_python), [str(venv_python)] + sys.argv)
    raise SystemExit(main())

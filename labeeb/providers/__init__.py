"""Provider package exports."""
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
from labeeb.providers.fakes import FakeCriticProvider, FakeJulesProvider, FakeOrchestratorProvider
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

__all__ = [
    "run_cmd",
    "parse_json_stdout",
    "extract_enveloped_json",
    "BrainProviderProtocol",
    "ImplementerProviderProtocol",
    "CriticProviderProtocol",
    "GitProviderProtocol",
    "OrchestratorProvider",
    "JulesProvider",
    "ClaudeCriticProvider",
    "GitProvider",
    "FakeOrchestratorProvider",
    "FakeJulesProvider",
    "FakeCriticProvider",
    "session_id",
    "repo_from_session",
    "branch_from_session",
    "activity_key",
    "activity_time",
    "activity_text",
    "ordered_activities",
    "latest_plan",
    "patch_candidates",
    "changed_paths_from_patch",
    "path_allowed",
    "has_user_message_marker",
]

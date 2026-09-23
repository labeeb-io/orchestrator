"""Exception hierarchy for Labeeb Orchestrator."""
from __future__ import annotations


class ControllerError(RuntimeError):
    """Base exception for all controller errors."""
    pass


class ConfigError(ControllerError):
    """Raised when configuration is invalid or missing."""
    pass


class GoalNotFoundError(ControllerError):
    """Raised when a specified goal cannot be found on disk."""
    pass


class AmbiguousEffect(ControllerError):
    """Raised when an in-flight side effect cannot be proven or reconciled safely."""
    pass


class CommandError(ControllerError):
    """Raised when an external subprocess command fails."""

    def __init__(self, message: str, *, cmd: list[str], rc: int, stdout: str, stderr: str):
        super().__init__(message)
        self.cmd = cmd
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr


class JulesSourceUnauthorizedError(ControllerError):
    """Raised when a repository is not authorized in Google Jules."""

    def __init__(self, repo: str, sources: list[str]):
        self.repo = repo
        self.sources = list(sources)
        sources_str = ", ".join(sources) if sources else "None discovered"
        msg = (
            f"Repository '{repo}' is not authorized in Google Jules. "
            f"Jules cannot mutate this repository without access. "
            f"Please authorize it at https://jules.google.com/ or select an authorized repository ({sources_str}). "
            f"Pass --force to bypass this check."
        )
        super().__init__(msg)

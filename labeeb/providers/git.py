"""Provider adapter for Git CLI and isolated worktree management."""
from __future__ import annotations

import pathlib
import shutil

from labeeb.config import Config, executable
from labeeb.providers.base import run_cmd


class GitProvider:
    def __init__(self, config: Config):
        self.config = config
        self.git = executable(config, "git", "git")

    def create_worktree(self, workspace: pathlib.Path, worktree: pathlib.Path, base_commit: str) -> None:
        run_cmd([self.git, "-C", str(workspace), "worktree", "add", "--detach", str(worktree), str(base_commit)], timeout=120)

    def check_patch(self, worktree: pathlib.Path, patch_path: pathlib.Path) -> None:
        run_cmd([self.git, "-C", str(worktree), "apply", "--check", str(patch_path)], timeout=60)

    def apply_patch(self, worktree: pathlib.Path, patch_path: pathlib.Path) -> None:
        run_cmd([self.git, "-C", str(worktree), "apply", str(patch_path)], timeout=60)

    def remove_worktree(self, workspace: pathlib.Path, worktree: pathlib.Path) -> None:
        run_cmd([self.git, "-C", str(workspace), "worktree", "remove", "--force", str(worktree)], timeout=120, check=False)
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)

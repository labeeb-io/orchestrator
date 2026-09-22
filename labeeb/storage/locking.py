"""Single-writer goal locking via POSIX fcntl.flock."""
from __future__ import annotations

import contextlib
import fcntl
import pathlib


class GoalLock:
    def __init__(self, lock_path: pathlib.Path):
        self.lock_path = lock_path

    @contextlib.contextmanager
    def locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        with self.lock_path.open("a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

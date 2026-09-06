"""Volatile storage shared with the supervising Atlas execution."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _storage_root() -> Path:
    root = Path("/dev/shm")
    if root.is_symlink() or not any(
        line.split()[1:3] == [str(root), "tmpfs"]
        for line in Path("/proc/mounts").read_text().splitlines()
    ):
        raise ValueError("volatile execution storage is unavailable")
    return root


@contextmanager
def temporary_run_directory() -> Iterator[Path]:
    """Create owner-only tmpfs storage; the caller must stop users before exit."""
    with tempfile.TemporaryDirectory(prefix="atlas-run-", dir=_storage_root()) as directory:
        yield Path(directory)


def get_run_directory() -> Path | None:
    """Return supervisor-owned storage, or None outside an Atlas execution.

    Children must remain in the supervisor's process group and leave storage
    cleanup to Atlas. Create a private subdirectory for application files;
    dotfiles and ``run-`` subdirectories are reserved for Atlas coordination.
    Nested directories remain until the outer execution finishes. An older
    supervisor without this contract is rejected.
    """
    value = os.environ.get("ATLAS_RUN_TEMP_DIR")
    if value is None:
        if os.environ.get("ATLAS_RUN_ID"):
            raise ValueError("Atlas supervisor does not provide volatile execution storage")
        return None
    path = Path(value)
    root = _storage_root()
    if not path.is_absolute() or root not in path.parents:
        raise ValueError("invalid Atlas run directory")
    directory = path
    while directory != root:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise ValueError("invalid Atlas run directory")
        directory = directory.parent
    return path

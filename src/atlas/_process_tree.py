"""Register execution groups before running code and serialize subtree shutdown."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def tree_root(directory: Path) -> Path:
    while directory.parent != Path("/dev/shm"):
        directory = directory.parent
    return directory


@contextmanager
def registration_lock(directory: Path) -> Iterator[None]:
    descriptor = os.open(tree_root(directory) / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def check_admission(directory: Path) -> None:
    root = tree_root(directory)
    ancestor = directory
    while True:
        if not ancestor.is_dir() or (ancestor / ".stopped").exists():
            raise ValueError("Atlas execution is stopping")
        if ancestor == root:
            break
        ancestor = ancestor.parent


def nested_directory(parent: Path) -> Path:
    with registration_lock(parent):
        check_admission(parent)
        return Path(tempfile.mkdtemp(prefix="run-", dir=parent))


def _signal_group(pid: int, signum: int) -> None:
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


def stop_tree(directory: Path, process: subprocess.Popen[object], signum: int) -> None:
    # A wrapper cannot detach or start target code while this lock is held.
    # Nested supervisors waiting for the lock remain in registered parent groups.
    with registration_lock(directory):
        (directory / ".stopped").touch(mode=0o600)
        markers = list(directory.rglob(".group"))
        groups = [int(marker.read_text()) for marker in markers]
        for group in groups:
            _signal_group(group, signum)
        # An unregistered wrapper still shares its caller's group: signal only it.
        if process.pid not in groups:
            process.send_signal(signum)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for group in groups:
            _signal_group(group, signal.SIGKILL)
        process.kill()
        process.wait()
        for marker in markers:
            marker.unlink()


def launch(directory: Path, argv: list[str]) -> int:
    try:
        with registration_lock(directory):
            check_admission(directory)
            os.setsid()
            (directory / ".group").write_text(str(os.getpid()))
        os.execv(argv[0], argv)
    except FileNotFoundError:
        return 127
    except PermissionError:
        return 126
    except ValueError:
        return 143


if __name__ == "__main__":  # pragma: no cover - executed by real process regressions
    raise SystemExit(launch(Path(sys.argv[1]), sys.argv[2:]))

from __future__ import annotations

import os
import signal
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import atlas.execution as executor
import atlas_core.execution as storage
from atlas_core.execution import get_run_directory, temporary_run_directory


def test_run_directory_contract(monkeypatch, tmp_path):
    monkeypatch.delenv("ATLAS_RUN_TEMP_DIR", raising=False)
    monkeypatch.delenv("ATLAS_RUN_ID", raising=False)
    assert get_run_directory() is None
    monkeypatch.setenv("ATLAS_RUN_ID", "older-supervisor")
    with pytest.raises(ValueError, match="supervisor"):
        get_run_directory()
    with temporary_run_directory() as directory:
        monkeypatch.setenv("ATLAS_RUN_TEMP_DIR", str(directory))
        assert get_run_directory() == directory
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        directory.chmod(0o755)
        with pytest.raises(ValueError, match="invalid"):
            get_run_directory()
        directory.chmod(0o700)
        with monkeypatch.context() as patch:
            patch.setattr(storage.os, "getuid", lambda: -1)
            with pytest.raises(ValueError, match="invalid"):
                get_run_directory()
        link = directory.with_name(directory.name + "-link")
        link.symlink_to(directory)
        try:
            monkeypatch.setenv("ATLAS_RUN_TEMP_DIR", str(link))
            with pytest.raises(ValueError, match="invalid"):
                get_run_directory()
        finally:
            link.unlink()
    assert not directory.exists()
    for value in ("relative", str(tmp_path)):
        monkeypatch.setenv("ATLAS_RUN_TEMP_DIR", value)
        with pytest.raises(ValueError, match="invalid"):
            get_run_directory()


def test_storage_rejects_missing_tmpfs_and_symlink(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *_args: "device /dev/shm ext4 rw 0 0\n")
    with pytest.raises(ValueError, match="unavailable"), temporary_run_directory():
        pytest.fail("disk-backed storage accepted")
    monkeypatch.setattr(Path, "is_symlink", lambda _path: True)
    with pytest.raises(ValueError, match="unavailable"), temporary_run_directory():
        pytest.fail("symlink accepted")


def test_signal_during_spawn_stops_owned_child_and_removes_storage(tmp_path, monkeypatch):
    from .test_edge_paths import native_command

    paths, _, command = native_command(tmp_path)
    original = executor.subprocess.Popen
    children = []
    directories = []

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        directories.append(Path(kwargs["env"]["ATLAS_RUN_TEMP_DIR"]))
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
        return child

    command.path.write_text("#!/bin/sh\nsleep 60\n")
    monkeypatch.setattr(executor.subprocess, "Popen", spawn)
    with temporary_run_directory() as parent:
        monkeypatch.setenv("ATLAS_RUN_TEMP_DIR", str(parent))
        assert executor.execute(paths, command, []) == 143
        assert parent.exists()
        assert directories[0] != parent
    assert children[0].poll() is not None
    assert not directories[0].exists()
    assert list(paths.context_dir.iterdir()) == []


def test_storage_cleanup_precedes_log_failure(tmp_path, monkeypatch):
    from .test_edge_paths import native_command

    paths, _, command = native_command(tmp_path)
    marker = tmp_path / "directory"
    command.path.write_text(f'#!/bin/sh\nprintf "%s" "$ATLAS_RUN_TEMP_DIR" > "{marker}"\n')

    def failed_record(*_args, **_kwargs):
        assert not Path(marker.read_text()).exists()
        raise OSError("record unavailable")

    monkeypatch.setattr(executor, "_record", failed_record)
    with pytest.raises(OSError, match="record unavailable"):
        executor.execute(paths, command, [])
    assert list(paths.context_dir.iterdir()) == []


def test_group_descendants_are_killed_when_leader_has_exited(monkeypatch):
    calls = []
    monkeypatch.setattr(executor.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    process = SimpleNamespace(pid=123, wait=lambda **_kwargs: 0)
    executor._terminate(process)
    assert calls == [(123, signal.SIGTERM), (123, signal.SIGKILL)]

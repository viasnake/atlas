from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import atlas._process_tree as tree
from atlas_core.execution import temporary_run_directory


def test_registration_and_subtree_shutdown(monkeypatch):
    calls = []
    with temporary_run_directory() as root:
        directory = tree.nested_directory(root)
        assert tree.tree_root(directory) == root
        (directory / ".group").write_text("123")
        nested = tree.nested_directory(directory)
        (nested / ".group").write_text("456")
        monkeypatch.setattr(tree.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
        process = SimpleNamespace(pid=123, wait=lambda **_kwargs: 0, kill=lambda: None)
        tree.stop_tree(directory, process, signal.SIGINT)
        assert set(calls) == {(123, signal.SIGINT), (456, signal.SIGINT),
                              (123, signal.SIGKILL), (456, signal.SIGKILL)}
        assert not list(directory.rglob(".group"))
        assert root.exists()
        with pytest.raises(ValueError, match="stopping"):
            tree.nested_directory(nested)


def test_unregistered_spawn_and_expired_grace(monkeypatch):
    calls = []
    waits = iter([subprocess.TimeoutExpired("test", 2), 0])

    def wait(**_kwargs):
        value = next(waits)
        if isinstance(value, Exception):
            raise value
        return value

    process = SimpleNamespace(pid=123, wait=wait, kill=lambda: calls.append("kill"),
                              send_signal=lambda sig: calls.append(sig))
    with temporary_run_directory() as root:
        tree.stop_tree(root, process, signal.SIGTERM)
    assert calls == [signal.SIGTERM, "kill"]
    monkeypatch.setattr(tree.os, "killpg", lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    tree._signal_group(123, signal.SIGTERM)


def test_launch_registers_before_exec_and_rejects_cancelled_ancestors(monkeypatch):
    monkeypatch.setattr(tree.os, "setsid", lambda: None)
    calls = []
    with temporary_run_directory() as root:
        def execute(binary, argv):
            assert (root / ".group").read_text() == str(os.getpid())
            calls.append((binary, argv))
            raise SystemExit(0)

        monkeypatch.setattr(tree.os, "execv", execute)
        with pytest.raises(SystemExit):
            tree.launch(root, ["/example", "literal argument"])
        assert calls == [("/example", ["/example", "literal argument"])]
        for error, status in [(FileNotFoundError, 127), (PermissionError, 126)]:
            monkeypatch.setattr(tree.os, "execv", lambda *_args, error=error: (_ for _ in ()).throw(error()))
            assert tree.launch(root, ["/example"]) == status
        (root / ".stopped").touch()
        assert tree.launch(root, ["/example"]) == 143
    assert tree.launch(root, ["/example"]) == 127


@pytest.mark.parametrize("depth", [1, 3])
def test_nested_supervisors_stop_descendants_and_remove_storage(tmp_path, depth):
    import json
    import shutil
    import sys
    import time

    from atlas.execution import execute

    from .test_edge_paths import native_command

    paths, _, command = native_command(tmp_path)
    marker = tmp_path / "ready.json"
    leaf = tmp_path / "leaf"
    leaf.write_text(f'''#!{sys.executable}
import json, os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
directory = Path(os.environ["ATLAS_RUN_TEMP_DIR"])
(directory / "synthetic-secret").write_text("synthetic-value")
Path({str(marker)!r}).write_text(json.dumps({{"pid": os.getpid(), "directory": str(directory)}}))
time.sleep(60)
''')
    leaf.chmod(0o755)
    target = leaf
    for index in range(depth):
        script = tmp_path / f"nested-{index}"
        script.write_text(f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})
from pathlib import Path
from atlas.catalog import CommandRef
from atlas.config import ProgramConfig, ProgramRuntime
from atlas.execution import execute
from atlas.paths import get_paths
p = ProgramConfig("nested", Path({str(tmp_path)!r}), ProgramRuntime("native"))
raise SystemExit(execute(get_paths(), CommandRef("nested", p, Path({str(target)!r}), "native"), []))
''')
        script.chmod(0o755)
        target = script
    command.path.write_text(f'#!/bin/sh\nexec "{target}"\n')
    data = None
    try:
        assert execute(paths, command, [], timeout_seconds=2) == 124
        data = json.loads(marker.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{data['pid']}/stat").read_text().split(")", 1)[1].split()[0]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("nested command survived outer timeout")
        assert not Path(data["directory"]).exists()
    finally:
        if data:
            try:
                os.kill(data["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            shutil.rmtree(tree.tree_root(Path(data["directory"])), ignore_errors=True)


def test_sigint_is_delivered_to_command_handler(tmp_path):
    import sys
    import threading
    import time

    from atlas.execution import execute

    from .test_edge_paths import native_command

    paths, _, command = native_command(tmp_path)
    ready = tmp_path / "ready"
    received = tmp_path / "received"
    command.path.write_text(f'''#!{sys.executable}
import signal, time
from pathlib import Path
def interrupted(signum, frame):
    Path({str(received)!r}).write_text(str(signum))
    raise SystemExit(130)
signal.signal(signal.SIGINT, interrupted)
Path({str(ready)!r}).touch()
time.sleep(60)
''')

    def interrupt():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)

    sender = threading.Thread(target=interrupt)
    sender.start()
    try:
        assert execute(paths, command, [], timeout_seconds=10) == 130
    finally:
        sender.join()
    assert received.read_text() == str(signal.SIGINT)

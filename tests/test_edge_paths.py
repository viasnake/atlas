from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import atlas.execution as execution_module
import atlas.runtime as runtime_module
from atlas.catalog import CommandRef
from atlas.config import AtlasConfig, ProgramConfig, ProgramRuntime, RuntimeConfig
from atlas.context import child_environment, execution_context, write_context
from atlas.execution import (
    _append_run_log,
    _execution_signals,
    _exit_code,
    _terminate,
    execute,
)
from atlas.paths import ensure_dirs, get_paths
from atlas.runtime import resolve_python, runtime_status, runtime_versions, venv_python
from atlas.yamlutil import load_yaml_file


def native_command(tmp_path: Path) -> tuple[object, object, object]:
    paths = get_paths(
        {
            "ATLAS_HOME": str(tmp_path / "home"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
        }
    )
    ensure_dirs(paths)
    paths.host_file.parent.mkdir(parents=True, exist_ok=True)
    paths.host_file.write_text("version: 1\nhost:\n  id: host\n", encoding="utf-8")
    root = tmp_path / "tool"
    path = root / "bin/run"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    program = ProgramConfig("tool", root, ProgramRuntime("native"))
    return paths, program, CommandRef("run", program, path, "native")


def test_paths_reject_traversal_and_bad_directories(tmp_path: Path) -> None:
    paths = get_paths(
        {
            "ATLAS_HOME": str(tmp_path / "home"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
        }
    )
    with pytest.raises(ValueError, match="invalid Python"):
        paths.python_runtime("../escape")
    with pytest.raises(ValueError, match="invalid venv"):
        paths.venv("../escape")
    paths.home.mkdir(parents=True)
    (paths.runtimes).write_text("file", encoding="utf-8")
    with pytest.raises(ValueError, match="Atlas path"):
        ensure_dirs(paths)


def test_yaml_loader_rejects_missing_and_duplicate(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="file not found"):
        load_yaml_file(tmp_path / "missing.yml")
    path = tmp_path / "duplicate.yml"
    path.write_text("a: 1\na: 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_yaml_file(path)


def test_context_payload_python_runtime_and_regular_file_guard(tmp_path: Path, monkeypatch) -> None:
    paths, program, command = native_command(tmp_path)
    python_program = ProgramConfig(
        "python-tool",
        program.root,
        ProgramRuntime("python", python_version="3.13", venv="python-tool"),
    )
    payload = execution_context(
        paths,
        CommandRef("run", python_program, command.path, "python"),
        run_id="run",
        parent_run_id="parent",
        operation_id="operation",
        working_directory=tmp_path,
    )
    assert payload["program"]["runtime"] == {
        "type": "python",
        "python": "3.13",
        "venv": str(paths.venv("python-tool")),
    }
    modules = program.root / "modules"
    modules.mkdir(parents=True)
    environment = child_environment(
        paths,
        CommandRef("run", python_program, command.path, "python"),
        payload,
        context_file=tmp_path / "context.json",
        run_id="run",
        parent_run_id="parent",
        operation_id="operation",
        python_path=tmp_path / "python",
        venv_path=paths.venv("python-tool"),
    )
    assert environment["PYTHONPATH"].split(os.pathsep)[0] == str(modules)

    target = tmp_path / "context.json"
    original = execution_module.stat.S_ISREG
    monkeypatch.setattr(execution_module.stat, "S_ISREG", lambda _mode: False)
    with pytest.raises(ValueError, match="regular file"):
        write_context(target, {})
    monkeypatch.setattr(execution_module.stat, "S_ISREG", original)


def test_append_log_regular_file_guard(tmp_path: Path, monkeypatch) -> None:
    paths, _program, _command = native_command(tmp_path)
    original = execution_module.stat.S_ISREG
    monkeypatch.setattr(execution_module.stat, "S_ISREG", lambda _mode: False)
    with pytest.raises(ValueError, match="run log"):
        _append_run_log(paths, {"x": 1})
    monkeypatch.setattr(execution_module.stat, "S_ISREG", original)


class FakeProcess:
    pid = 100

    def __init__(self, waits: list[object]) -> None:
        self.waits = waits

    def wait(self, timeout: float | None = None) -> int:
        value = self.waits.pop(0)
        if isinstance(value, BaseException):
            raise value
        return int(value)


def test_process_helpers_cover_signal_and_timeout_paths(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(execution_module.os, "killpg", lambda pid, signum: calls.append((pid, signum)))
    process = FakeProcess([subprocess.TimeoutExpired("x", 1), 0])
    _terminate(process)
    assert calls == [(100, signal.SIGTERM), (100, signal.SIGKILL)]

    monkeypatch.setattr(execution_module.os, "killpg", lambda _pid, _signum: (_ for _ in ()).throw(ProcessLookupError()))
    _terminate(FakeProcess([]))
    calls.clear()
    def kill_term_then_gone(_pid: int, signum: int) -> None:
        calls.append((100, signum))
        if signum == signal.SIGKILL:
            raise ProcessLookupError()
    monkeypatch.setattr(execution_module.os, "killpg", kill_term_then_gone)
    _terminate(FakeProcess([subprocess.TimeoutExpired("x", 1), 0]))
    assert _exit_code(-signal.SIGTERM) == 128 + signal.SIGTERM
    assert _exit_code(7) == 7


def test_execution_signals_defer_until_child_is_owned():
    previous = signal.getsignal(signal.SIGTERM)
    with _execution_signals() as received:
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        handler(signal.SIGINT, None)
        assert received == [signal.SIGTERM, signal.SIGINT]
    assert signal.getsignal(signal.SIGTERM) == previous


def test_runtime_resolution_edges(monkeypatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="version or executable"):
        resolve_python("")
    candidate = tmp_path / "python"
    candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    candidate.chmod(0o755)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="configure pyenv"):
        resolve_python("3.14.6")
    assert resolve_python("3.14.6", executable=candidate) == candidate.resolve()
    with pytest.raises(FileNotFoundError, match="not found"):
        resolve_python("9.9")

    pyenv_prefix = tmp_path / "pyenv/3.14"
    pyenv_python = pyenv_prefix / "bin/python"
    pyenv_python.parent.mkdir(parents=True)
    pyenv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    pyenv_python.chmod(0o755)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/pyenv" if name == "pyenv" else None)
    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"stdout": f"{pyenv_prefix}\n"})())
    assert resolve_python("3.14") == pyenv_python.resolve()

    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, args[0])))
    assert runtime_module._pyenv_python("3.14") is None
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/pyenv" if name == "pyenv" else None)
    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"stdout": ""})())
    assert runtime_module._pyenv_python("3.14") is None

    no_exec = tmp_path / "no-exec"
    no_exec.write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError, match="not executable"):
        resolve_python("3.14", executable=no_exec)


def test_pyenv_runtime_install_edges(monkeypatch, tmp_path: Path) -> None:
    candidate = tmp_path / "pyenv/3.14/bin/python"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    candidate.chmod(0o755)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/pyenv" if name == "pyenv" else None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda command, check: calls.append(command),
    )
    monkeypatch.setattr(runtime_module, "_pyenv_python", lambda _version: candidate)
    assert runtime_module._install_pyenv_python("3.14") == candidate.resolve()
    assert calls == [["/usr/bin/pyenv", "install", "--skip-existing", "3.14"]]

    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, "pyenv")
        ),
    )
    with pytest.raises(RuntimeError, match="could not install"):
        runtime_module._install_pyenv_python("3.14")

    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_module, "_pyenv_python", lambda _version: None)
    with pytest.raises(FileNotFoundError, match="not found"):
        runtime_module._install_pyenv_python("3.14")

    monkeypatch.setattr(runtime_module.shutil, "which", lambda _name: None)
    assert runtime_module._install_pyenv_python("3.14") is None
    monkeypatch.setattr(runtime_module, "_install_pyenv_python", lambda _version: candidate)
    assert runtime_module._managed_python("3.14", None) == candidate.resolve()
    monkeypatch.setattr(runtime_module, "_install_pyenv_python", lambda _version: None)
    monkeypatch.setattr(runtime_module, "resolve_python", lambda _version: candidate.resolve())
    assert runtime_module._managed_python("3.14", None) == candidate.resolve()


def test_runtime_link_and_venv_error_edges(tmp_path: Path, monkeypatch) -> None:
    paths, _program, _command = native_command(tmp_path)
    python = Path(sys.executable)
    program = ProgramConfig("python", tmp_path / "python", ProgramRuntime("python", venv="python"))
    config = AtlasConfig(tmp_path / "config.yml", RuntimeConfig("3.14", python), {"python": program})
    target = paths.python_runtime("3.14")
    target.parent.mkdir(parents=True)
    target.symlink_to(python)
    assert runtime_module.ensure_python_runtime(paths, config, program) == target
    target.unlink()
    alternate = tmp_path / "alternate-python"
    alternate.write_text("not python", encoding="utf-8")
    alternate.chmod(0o755)
    target.symlink_to(alternate)
    assert runtime_module.ensure_python_runtime(paths, config, program) == target
    target.unlink()
    target.write_text("file", encoding="utf-8")
    with pytest.raises(ValueError, match="not a symlink"):
        runtime_module.ensure_python_runtime(paths, config, program)

    target.unlink()
    target.symlink_to(python)
    venv = paths.venv("python")
    (venv / "Scripts").mkdir(parents=True)
    windows_python = venv / "Scripts/python.exe"
    windows_python.write_text("#!/bin/sh\n", encoding="utf-8")
    windows_python.chmod(0o755)
    assert venv_python(paths, program) == windows_python.resolve()
    with pytest.raises(ValueError, match="does not use Python"):
        venv_python(paths, ProgramConfig("native", tmp_path, ProgramRuntime("native")))
    with pytest.raises(FileNotFoundError, match="venv Python"):
        runtime_module._venv_python(tmp_path / "empty-venv")

    broken = paths.venv("broken")
    broken.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("venv failed")))
    broken_program = ProgramConfig("broken", tmp_path / "broken", ProgramRuntime("python", venv="broken"))
    with pytest.raises(RuntimeError, match="venv failed"):
        runtime_module.create_venv(paths, config, broken_program)
    assert not broken.exists()


def test_runtime_versions_and_status_catch_failures(tmp_path: Path, monkeypatch) -> None:
    program = ProgramConfig("tool", tmp_path, ProgramRuntime("python"))
    config = AtlasConfig(tmp_path / "config.yml", RuntimeConfig(), {"tool": program})
    with pytest.raises(ValueError, match="not configured"):
        runtime_versions(config)
    original_resolve = runtime_module.resolve_python
    monkeypatch.setattr(runtime_module, "resolve_python", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("no")))
    configured = AtlasConfig(tmp_path / "config.yml", RuntimeConfig("3.14"), {"tool": ProgramConfig("tool", tmp_path, ProgramRuntime("python", venv="tool"))})
    assert (
        runtime_status(
            get_paths(
                {
                    "ATLAS_HOME": str(tmp_path / "home"),
                    "ATLAS_ETC_DIR": str(tmp_path / "etc"),
                    "ATLAS_VAR_DIR": str(tmp_path / "var"),
                }
            ),
            configured,
        )[0].available
        is False
    )
    monkeypatch.setattr(runtime_module, "resolve_python", original_resolve)

    specific = AtlasConfig(
        tmp_path / "specific.yml",
        RuntimeConfig("3.14", Path(sys.executable)),
        {"tool": ProgramConfig("tool", tmp_path, ProgramRuntime("python", "3.13", "tool"))},
    )
    assert runtime_versions(specific) == [("3.14", Path(sys.executable)), ("3.13", None)]

    install_home = tmp_path / "install-home"
    install_paths = get_paths(
        {
            "ATLAS_HOME": str(install_home),
            "ATLAS_ETC_DIR": str(tmp_path / "install-etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "install-var"),
        }
    )
    target = install_paths.python_runtime("3.14")
    target.parent.mkdir(parents=True)
    wrong = tmp_path / "wrong-python"
    wrong.write_text("wrong", encoding="utf-8")
    wrong.chmod(0o755)
    target.symlink_to(wrong)
    install_configured = AtlasConfig(
        tmp_path / "config.yml",
        RuntimeConfig("3.14", Path(sys.executable)),
        {},
    )
    assert runtime_module.install_configured_runtimes(install_paths, install_configured) == [target]
    target.unlink()
    target.write_text("wrong", encoding="utf-8")
    with pytest.raises(ValueError, match="not a symlink"):
        runtime_module.install_configured_runtimes(install_paths, install_configured)


def test_execution_spawn_and_signal_error_paths(tmp_path: Path, monkeypatch) -> None:
    paths, _program, command = native_command(tmp_path)
    original_popen = execution_module.subprocess.Popen

    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(execution_module.subprocess, "Popen", missing)
    assert execute(paths, command, []) == 127
    assert json.loads(paths.run_log.read_text(encoding="utf-8").splitlines()[-1])["exit_code"] == 127

    def denied(*_args, **_kwargs):
        raise PermissionError

    monkeypatch.setattr(execution_module.subprocess, "Popen", denied)
    assert execute(paths, command, []) == 126

    class InterruptingProcess(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            raise KeyboardInterrupt

    monkeypatch.setattr(execution_module.subprocess, "Popen", lambda *_args, **_kwargs: InterruptingProcess([]))
    monkeypatch.setattr(execution_module, "_terminate", lambda _process: None)
    assert execute(paths, command, []) == 130

    command.path.write_text("#!/bin/sh\nrm \"$ATLAS_CONTEXT_FILE\"\n", encoding="utf-8")
    command.path.chmod(0o755)
    monkeypatch.setattr(execution_module.subprocess, "Popen", original_popen)
    assert execute(paths, command, []) == 0

"""The single process execution path used by ``atlas run`` and shims."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from getpass import getuser
from pathlib import Path
from uuid import uuid4

from atlas_core.execution import temporary_run_directory
from atlas_core.host import get_host

from .catalog import CommandRef
from .context import child_environment, execution_context, write_context
from .paths import AtlasPaths
from .runtime import venv_python

TIMEOUT_EXIT_CODE = 124
MISSING_EXECUTABLE_EXIT_CODE = 127
SIGNAL_EXIT_OFFSET = 128
_TERMINATE_GRACE_SECONDS = 2


def _append_run_log(paths: AtlasPaths, record: dict[str, object]) -> None:
    """Append one non-sensitive run record to the JSONL log."""
    if paths.logs.is_symlink() or (paths.logs.exists() and not paths.logs.is_dir()):
        raise ValueError(f"logs path must be a directory: {paths.logs}")
    paths.logs.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        paths.run_log,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"run log must be a regular file: {paths.run_log}")
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _terminate(process: subprocess.Popen[object]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # The group can outlive its leader, including after a successful command.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


@contextmanager
def _execution_signals() -> Iterator[list[int]]:
    received: list[int] = []
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}

    def receive(signum: int, _frame: object) -> None:
        received.append(signum)

    for number in previous:
        signal.signal(number, receive)
    try:
        yield received
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _wait(process: subprocess.Popen[object], timeout: int | None, received: list[int]) -> int:
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if received:
            return SIGNAL_EXIT_OFFSET + received[0]
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return _exit_code(process.wait(timeout=0.1))
        except subprocess.TimeoutExpired:
            pass


def _exit_code(return_code: int) -> int:
    if return_code < 0:
        return SIGNAL_EXIT_OFFSET + abs(return_code)
    return return_code


def _command_argv(
    paths: AtlasPaths,
    command: CommandRef,
) -> tuple[list[str], Path | None, Path | None]:
    if command.type == "native":
        return [str(command.path)], None, None
    venv = paths.venv(command.program.runtime.venv or command.program.name)
    python = venv_python(paths, command.program)
    return [str(python), str(command.path)], python, venv


def _record(
    paths: AtlasPaths,
    command: CommandRef,
    *,
    run_id: str,
    parent_run_id: str | None,
    operation_id: str,
    started_at: datetime,
    started: float,
    exit_code: int,
    timed_out: bool,
    working_directory: Path,
) -> None:
    host = get_host(paths.host_file)
    _append_run_log(
        paths,
        {
            "timestamp": started_at.isoformat().replace("+00:00", "Z"),
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "operation_id": operation_id,
            "host_id": host.id,
            "user": getuser(),
            "program": command.program.name,
            "command": command.name,
            "command_type": command.type,
            "working_directory": str(working_directory),
            "exit_code": exit_code,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "timed_out": timed_out,
        },
    )


def execute(
    paths: AtlasPaths,
    command: CommandRef,
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int | None = None,
) -> int:
    """Execute one command, expose its context, and append one run record."""
    working_directory = Path.cwd() if cwd is None else cwd
    if not working_directory.is_absolute() or not working_directory.is_dir():
        raise ValueError(f"working directory not found: {working_directory}")
    if timeout_seconds is not None and (timeout_seconds <= 0 or isinstance(timeout_seconds, bool)):
        raise ValueError("timeout must be a positive integer")

    run_id = str(uuid4())
    parent_run_id = os.environ.get("ATLAS_RUN_ID") or None
    operation_id = os.environ.get("ATLAS_OPERATION_ID") or run_id
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    context_file = paths.context_dir / f"{run_id}.json"
    payload = execution_context(
        paths,
        command,
        run_id=run_id,
        parent_run_id=parent_run_id,
        operation_id=operation_id,
        working_directory=working_directory,
    )
    argv, python_path, venv_path = _command_argv(paths, command)
    argv.extend(args)
    write_context(context_file, payload)
    timed_out = False
    process: subprocess.Popen[object] | None = None
    exit_code = MISSING_EXECUTABLE_EXIT_CODE
    try:
        environment = child_environment(
            paths,
            command,
            payload,
            context_file=context_file,
            run_id=run_id,
            parent_run_id=parent_run_id,
            operation_id=operation_id,
            python_path=python_path,
            venv_path=venv_path,
        )
        if command.program.runtime.python_version is not None:
            environment["ATLAS_PYTHON_VERSION"] = command.program.runtime.python_version
        with _execution_signals() as received, temporary_run_directory() as directory:
            environment["ATLAS_RUN_TEMP_DIR"] = str(directory)
            try:
                try:
                    process = subprocess.Popen(
                        argv,
                        cwd=working_directory,
                        env=environment,
                        start_new_session=True,
                    )
                except FileNotFoundError:
                    exit_code = MISSING_EXECUTABLE_EXIT_CODE
                except PermissionError:
                    exit_code = 126
                else:
                    try:
                        exit_code = _wait(process, timeout_seconds, received)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        exit_code = TIMEOUT_EXIT_CODE
                    except KeyboardInterrupt:
                        exit_code = 130
            finally:
                if process is not None:
                    _terminate(process)
    finally:
        try:
            context_file.unlink()
        except FileNotFoundError:
            pass
        _record(
            paths,
            command,
            run_id=run_id,
            parent_run_id=parent_run_id,
            operation_id=operation_id,
            started_at=started_at,
            started=started,
            exit_code=exit_code,
            timed_out=timed_out,
            working_directory=working_directory,
        )
    return exit_code

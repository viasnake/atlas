"""Command-line interface for the small Atlas execution standard."""

from __future__ import annotations

import argparse
import json
import sys

from atlas_core.host import get_host

from .catalog import command_index, resolve_command
from .config import AtlasConfig, ProgramConfig, load_config
from .execution import execute
from .paths import AtlasPaths, ensure_dirs, get_paths
from .runtime import (
    create_venv,
    install_configured_runtimes,
    runtime_status,
    venv_python,
)
from .shims import generate_shims


def _config(paths: AtlasPaths) -> AtlasConfig:
    return load_config(paths.config_file)


def _program(config: AtlasConfig, name: str) -> ProgramConfig:
    program = config.programs.get(name)
    if program is None:
        raise ValueError(f"unknown program: {name}")
    return program


def cmd_status(_: argparse.Namespace) -> int:
    """Print the current local program and Atlas path state."""
    paths = get_paths()
    config = _config(paths)
    commands = command_index(config)
    host_id = "unavailable"
    try:
        host_id = get_host(paths.host_file).id
    except (FileNotFoundError, TypeError, ValueError):
        pass
    print(f"config file: {paths.config_file}")
    print(f"host file: {paths.host_file}")
    print(f"host id: {host_id}")
    print(f"programs: {len(config.programs)}")
    print(f"commands: {len(commands)}")
    print(f"runtimes: {paths.runtimes}")
    print(f"venvs: {paths.venvs}")
    print(f"shims: {paths.shims}")
    print(f"run log: {paths.run_log}")
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    """Check configured programs, runtimes, venvs, and host identity."""
    paths = get_paths()
    config = _config(paths)
    failures: list[str] = []
    try:
        commands = command_index(config)
    except (OSError, ValueError) as exc:
        commands = {}
        failures.append(str(exc))
    try:
        get_host(paths.host_file)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        failures.append(str(exc))
    for status in runtime_status(paths, config):
        if not status.available:
            failures.append(f"Python runtime unavailable: {status.version}")
    for program in config.programs.values():
        if program.runtime.type == "python":
            try:
                venv_python(paths, program)
            except (FileNotFoundError, PermissionError, ValueError) as exc:
                failures.append(str(exc))
    if failures:
        for failure in failures:
            print(f"atlas check: {failure}", file=sys.stderr)
        return 2
    print(f"ok: {len(config.programs)} programs, {len(commands)} commands")
    return 0


def cmd_secret_check(args: argparse.Namespace) -> int:
    """Verify required logical names without displaying their values."""
    from atlas_core.secrets import load_provider

    provider = load_provider()
    values = provider.get_many(args.names)
    print(f"secrets: {len(values)}/{len(set(args.names))} available")
    return 0


def cmd_context(_: argparse.Namespace) -> int:
    """Print the host and Atlas path context as JSON."""
    paths = get_paths()
    from .context import base_context

    print(json.dumps(base_context(paths), ensure_ascii=False, sort_keys=True))
    return 0


def cmd_program_list(args: argparse.Namespace) -> int:
    """List registered local programs."""
    config = _config(get_paths())
    for program in config.programs.values():
        if args.verbose:
            runtime = program.runtime.type
            if program.runtime.venv is not None:
                runtime += f" venv={program.runtime.venv}"
            print(f"{program.name}\t{program.root}\t{runtime}")
        else:
            print(program.name)
    return 0


def cmd_command_list(args: argparse.Namespace) -> int:
    """List discovered public commands."""
    commands = command_index(_config(get_paths()))
    for name, command in commands.items():
        if args.verbose:
            print(f"{name}\t{command.program.name}\t{command.type}\t{command.path}")
        else:
            print(name)
    return 0


def cmd_which(args: argparse.Namespace) -> int:
    """Print the local executable selected for one command."""
    command = resolve_command(_config(get_paths()), args.command_name)
    print(command.path)
    return 0


def cmd_shim_generate(_: argparse.Namespace) -> int:
    """Generate shims for all discovered commands."""
    paths = get_paths()
    ensure_dirs(paths)
    names = generate_shims(paths, _config(paths))
    for name in names:
        print(paths.shims / name)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run one command through the shared executor."""
    paths = get_paths()
    ensure_dirs(paths)
    command = resolve_command(_config(paths), args.command_name)
    command_args = args.args[1:] if args.args[:1] == ["--"] else args.args
    return execute(paths, command, command_args, timeout_seconds=args.timeout)


def cmd_runtime_status(_: argparse.Namespace) -> int:
    """Print selected Python runtime status."""
    paths = get_paths()
    config = _config(paths)
    for status in runtime_status(paths, config):
        executable = "unavailable" if status.executable is None else str(status.executable)
        print(f"{status.version}\t{str(status.available).lower()}\t{status.atlas_path}\t{executable}")
    return 0


def cmd_runtime_install(_: argparse.Namespace) -> int:
    """Register configured Python interpreters below the Atlas runtime path."""
    paths = get_paths()
    ensure_dirs(paths)
    for path in install_configured_runtimes(paths, _config(paths)):
        print(path)
    return 0


def cmd_venv_list(_: argparse.Namespace) -> int:
    """List configured Python venvs and their current state."""
    paths = get_paths()
    config = _config(paths)
    for program in config.programs.values():
        if program.runtime.type != "python":
            continue
        assert program.runtime.venv is not None
        path = paths.venv(program.runtime.venv)
        state = "ready" if path.is_dir() else "missing"
        print(f"{program.name}\t{program.runtime.venv}\t{state}")
    return 0


def cmd_venv_create(args: argparse.Namespace) -> int:
    """Create the dedicated venv for one configured Python program."""
    paths = get_paths()
    ensure_dirs(paths)
    config = _config(paths)
    path = create_venv(paths, config, _program(config, args.program))
    print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the minimal Atlas CLI."""
    parser = argparse.ArgumentParser(prog="atlas")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, function in (("status", cmd_status), ("check", cmd_check), ("context", cmd_context)):
        command = sub.add_parser(name)
        command.set_defaults(func=function)

    secrets = sub.add_parser("secret")
    secrets_sub = secrets.add_subparsers(dest="secret_command", required=True)
    secret_check = secrets_sub.add_parser("check")
    secret_check.add_argument("names", nargs="+")
    secret_check.set_defaults(func=cmd_secret_check)

    program = sub.add_parser("program")
    program_sub = program.add_subparsers(dest="program_command", required=True)
    program_list = program_sub.add_parser("list")
    program_list.add_argument("--verbose", action="store_true")
    program_list.set_defaults(func=cmd_program_list)

    command = sub.add_parser("command")
    command_sub = command.add_subparsers(dest="command_command", required=True)
    command_list = command_sub.add_parser("list")
    command_list.add_argument("--verbose", action="store_true")
    command_list.set_defaults(func=cmd_command_list)

    which = sub.add_parser("which")
    which.add_argument("command_name")
    which.set_defaults(func=cmd_which)

    shim = sub.add_parser("shim")
    shim_sub = shim.add_subparsers(dest="shim_command", required=True)
    shim_generate = shim_sub.add_parser("generate")
    shim_generate.set_defaults(func=cmd_shim_generate)

    run = sub.add_parser("run")
    run.add_argument("--timeout", type=int)
    run.add_argument("command_name")
    run.add_argument("args", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    runtime = sub.add_parser("runtime")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_status = runtime_sub.add_parser("status")
    runtime_status.set_defaults(func=cmd_runtime_status)
    runtime_install = runtime_sub.add_parser("install")
    runtime_install.set_defaults(func=cmd_runtime_install)

    venv = sub.add_parser("venv")
    venv_sub = venv.add_subparsers(dest="venv_command", required=True)
    venv_list = venv_sub.add_parser("list")
    venv_list.set_defaults(func=cmd_venv_list)
    venv_create = venv_sub.add_parser("create")
    venv_create.add_argument("program")
    venv_create.set_defaults(func=cmd_venv_create)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run Atlas and convert domain errors into a concise diagnostic."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"atlas: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

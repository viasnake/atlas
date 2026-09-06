# Atlas

Atlas separates infrastructure automation from host operating-system changes by providing a
stable execution environment, host identity, invocation path, and run record.

Atlas manages:

- selected Python interpreters and one virtual environment per Python program;
- registration of locally present programs;
- discovery of public commands and generation of shims;
- `/etc/atlas/host.yml` and the language-neutral execution context;
- one shared executor for `atlas run` and shims; and
- a small JSONL record of completed executions.

Atlas does not fetch or version automation programs. It does not contain infrastructure provider, inventory,
workflow, scheduler, or infrastructure lifecycle code. Put those responsibilities in the
programs registered with Atlas and invoke them through Atlas when a common execution contract is
needed.

## Configuration

`/etc/atlas/config.yml` registers programs that already exist on the host:

```yaml
runtime:
  python:
    version: "3.13"

programs:
  provisioning:
    root: /srv/provisioning
    runtime:
      type: python
      python: "3.13"
      venv: provisioning
  pve-tools:
    root: /opt/pve-tools
    runtime:
      type: native
```

Python commands are discovered below `commands/**/*.py`. Native commands are executable files
below `commands/` or `bin/`. Nested paths become hyphenated command names, so
`commands/host/diff.py` becomes `host-diff`. Duplicate names fail closed.

The host identity is separate from program configuration:

```yaml
version: 1
host:
  id: control01
  role: control
  site: kanagawa01
```

`host.yml` contains only the identity of the host running Atlas. It is not an inventory or a
secret store.

## Execution

```bash
atlas runtime install
atlas venv create provisioning
atlas command list
atlas shim generate
export PATH="/opt/atlas/shims:$PATH"
atlas run host-diff web01
host-diff web01
```

Both invocations resolve the current registered program and enter the same executor. A Python
command runs with its program venv; a native command runs directly. The child receives
`ATLAS_CONTEXT_FILE` plus the `ATLAS_*` identifiers. The JSON context contains the host, Atlas
paths, program, command, working directory, and run identifiers, so non-Python programs can read
the same information without importing `atlas_core`.

Python programs may use the small public API:

```python
from atlas_core import get_context

context = get_context()
print(context.host.id, context.execution.run_id)
```

Execution facts are appended to `/var/lib/atlas/logs/runs.jsonl`. Atlas records the timestamp,
host, user, program, command, identifiers, duration, and exit status; command output remains the
responsibility of the invoking terminal, service manager, or CI system.

## External secrets

Programs can use `atlas_core.secrets` to retrieve logical secret names from an external
secret manager. Host-owned mappings and owner-only bootstrap credentials stay outside
automation repositories. `atlas secret check <logical-name> ...` verifies availability
without displaying values. See [the operator reference](docs/reference.rst) for setup,
program integration, and recovery.

## Development

The repository uses mise:

```bash
mise run setup
mise run check
make clean-docs html SPHINXOPTS=-W
```

The examples under `examples/` are local program trees, not Atlas-managed distributions.

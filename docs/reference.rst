Operator reference
==================

Install Atlas as the host-side runtime and keep the automation programs it executes outside the
Atlas directory. Atlas owns runtime links, Python venvs, generated shims, context files, and run
records.

Filesystem layout
-----------------

The default layout is:

.. code-block:: text

   /etc/atlas/
   ├── config.yml
   └── host.yml

   /opt/atlas/
   ├── runtimes/python/
   ├── venvs/
   ├── shims/
   └── launchers/

   /var/lib/atlas/
   ├── logs/runs.jsonl
   └── runtime-state/

The paths can be redirected for tests with ``ATLAS_HOME``, ``ATLAS_ETC_DIR``,
``ATLAS_VAR_DIR``, ``ATLAS_RUNTIMES_DIR``, ``ATLAS_VENVS_DIR``, ``ATLAS_SHIMS_DIR``, and
``ATLAS_HOST_FILE``.

Register programs
-----------------

``config.yml`` contains only runtime selections and local program registrations:

.. code-block:: yaml

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
     image-tool:
       root: /opt/image-tool
       runtime:
         type: native

``root`` must be an absolute path. A Python program uses its own named venv. A native program is
started without a Python intermediary. The global Python selection is used when a Python program
does not specify its own version. A configured Python executable may be supplied as
``runtime.python.executable`` when the host's managed interpreter is not exposed through pyenv.

Atlas never clones, fetches, downloads, extracts, installs, updates, rolls back, or garbage
collects program trees. Place and update those trees using the program's normal packaging or
deployment workflow.

Command discovery and shims
---------------------------

Python files below ``program-root/commands`` are commands. Executable files below
``program-root/commands`` and, for native programs, ``program-root/bin`` are native commands.
Path segments use lowercase letters, digits, and hyphens. Nested paths are joined with hyphens.
Atlas rejects symlinks and duplicate command names.

Generate shims after registering or changing a program:

.. code-block:: console

   $ atlas command list --verbose
   $ atlas shim generate
   $ export PATH="/opt/atlas/shims:$PATH"
   $ host-diff web01

Generated shims call ``python -m atlas.cli run <command>``. They do not contain a second
execution implementation.

Runtime and venvs
-----------------

``atlas runtime install`` resolves each configured Python version and exposes it below
``/opt/atlas/runtimes/python``. If ``pyenv`` is available, Atlas installs a missing selected
version through that existing runtime manager; otherwise use ``runtime.python.executable`` or an
explicitly provisioned interpreter. Atlas does not use the host's system Python implicitly and
does not install program dependencies. ``atlas venv create <program>`` creates the dedicated venv.
Install dependencies using the program's existing ``pyproject.toml``, requirements file, lock
file, or other standard tooling.

.. code-block:: console

   $ atlas runtime status
   $ atlas runtime install
   $ atlas venv create provisioning
   $ atlas venv list

Execution context
-----------------

``/etc/atlas/host.yml`` identifies only the current host:

.. code-block:: yaml

   version: 1
   host:
     id: control01
     role: control
     site: kanagawa01

During execution Atlas writes a short-lived JSON document and sets ``ATLAS_CONTEXT_FILE``. The
document contains the host, standard paths, program, command, working directory, and
``run_id``, ``parent_run_id``, and ``operation_id``. The same identifiers are available as
environment variables. Python code can call ``atlas_core.get_context()``; other languages can
parse the JSON file directly.

The diagnostic command does not require a registered program:

.. code-block:: console

   $ atlas context
   $ atlas status
   $ atlas check

Run records
-----------

Each spawned command appends one JSON object to ``/var/lib/atlas/logs/runs.jsonl``. It records the
UTC timestamp, host id, user, program, command, run identifiers, working directory, duration, and
exit status. Stdout and stderr are not copied into this log.

The executor preserves the child exit status, returns ``124`` for a timeout, and maps a signal
termination to ``128 + signal``. The child is launched with the caller's working directory and
with an exact argument vector; shell interpretation is not used.

Each execution requires Linux with ``/dev/shm`` mounted as tmpfs. Atlas creates
an owner-only directory there and exposes its path as ``ATLAS_RUN_TEMP_DIR``.
Programs can obtain it through ``atlas_core.execution.get_run_directory()``.
Keep subprocesses in the inherited process group when using this storage; do
not start a new session or leave background work running beyond the command.
Nested Atlas executions receive separate subdirectories within the same execution
tree. Atlas registers their process groups before starting command code and
serializes registration with subtree shutdown.

On completion, timeout, SIGINT or SIGTERM, Atlas stops the command's process
group and registered nested groups, including descendants left by an exited
group leader. The outer supervisor deletes the execution tree's directories
after stopping those groups. SIGINT and SIGTERM are forwarded without changing
the signal, with SIGKILL escalation after the grace period. Run records are
written after shutdown. SIGKILL of the Atlas supervisor or host
failure cannot run cleanup. Clear abandoned volatile files before reusing a
recovered host. Disable swap or use encrypted swap for secret-bearing storage.

External secrets
----------------

Programs can retrieve required secrets through ``atlas_core.secrets``. A logical
name has the form ``system.purpose[.detail]``, for example
``mysql.backup.password``. Programs declare these names; the Atlas host owns the
mapping to external identifiers. Values remain in the retrieving process, and
successful reads are cached only for the lifetime of that provider instance.
Create a new instance for each execution to observe subsequent secret updates.

The initial adapter uses the official Bitwarden Secrets Manager Python SDK.
Install the optional dependency in every interpreter that retrieves secrets,
including a program's dedicated virtual environment::

    python -m pip install 'atlas[secrets]'

The SDK requires a supported platform wheel. Atlas's core installation does not
require it. ``SecretProvider`` exposes only ``get`` and ``get_many``; provider
configuration, authentication and backend identifiers stay behind the adapter.
There is no secret creation, rotation, persistent cache, or secret export API.

Host setup
~~~~~~~~~~

In Bitwarden Secrets Manager Cloud, create a project and a machine account with
read permission on that project. Create an access token for that account. Keep
administrative recovery access outside the infrastructure being rebuilt. Do not
use a production access token in CI.

Place the access token in ``/etc/atlas/credentials/secret-provider`` without
printing it or putting it in shell arguments. The regular file must belong to
the user executing the program, have mode ``0600`` and a single hard link, and
have no symlink components. Each execution account needs its own securely
provisioned credential. Atlas does not change users or grant access.

Create ``/etc/atlas/secrets.yml`` with identifiers, never values:

.. code-block:: yaml

    provider: bitwarden
    config:
      region: us
      credential_file: /etc/atlas/credentials/secret-provider
      project_id: 00000000-0000-4000-8000-000000000001
    mappings:
      mysql.backup.password:
        secret_id: 00000000-0000-4000-8000-000000000002

Replace these example UUIDs with the project's and secret's identifiers. The
``region`` is ``us`` or ``eu``. Responses must belong to the configured project.
Duplicate YAML keys, unsupported providers, malformed mappings, missing values,
empty values and retrieval errors fail closed. Configuration errors are distinct
from retrieval errors. A missing SDK is a local configuration error. Provider
error bodies are not included in diagnostics.
The adapter never requests an SDK state file.

``ATLAS_ETC_DIR`` selects a different Atlas configuration directory, as it does
for other Atlas configuration files. Keep this host configuration outside the
Provisioning repository. The bootstrap credential is separate from this mapping
and must not be backed up into Git.

Check the names needed by an execution without displaying their values::

    atlas secret check mysql.backup.password

The command reports the available count and returns zero only if every requested
name resolves. Failure returns status 2. There is no value-printing command.
An empty request is not an authentication check and is rejected by the CLI.

Program integration
~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    from atlas_core.secrets import load_provider

    provider = load_provider()
    values = provider.get_many(["mysql.backup.password"])

Resolve the complete required set before starting operations. Do not log values,
returned dictionaries, or local variables containing them. Program output is
outside Atlas's run-record redaction boundary. Consumers must protect all output
channels and use Ansible's ``no_log: true`` for tasks that expose secrets.
The public Python API returns strings; it is not a sandbox for trusted programs.
Tests can supply any object implementing ``get`` and ``get_many`` without loading
the SDK or connecting to a service.

To add a secret, create it in the external project, add its logical-name mapping
on the Atlas host, and declare the logical name in the consuming program. To
rotate a value, update the external secret and coordinate the corresponding
service credential change. A new program execution retrieves the updated value;
Atlas does not rotate the target service credential itself.

Recovery and manual acceptance
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use a dedicated test project and synthetic values for this acceptance procedure.
Do not copy an existing control host's disk, snapshot, secret cache, or credential
file into the test host.

1. Provision a new Linux control host and install Atlas and its optional secrets
   dependency. Register a clean checkout of the automation program and prepare
   its interpreter and Ansible dependencies.
2. Sign in to Bitwarden using recovery access held outside Proxmox. Issue a new
   access token for the read-only machine account and securely place it in the
   new host's owner-only credential file.
3. Restore the non-secret Atlas configuration and logical-name mappings from
   their independent backup. Restore host identity and program registration.
4. Run ``atlas secret check`` with every name required by the test execution.
5. Start the program against a disposable local Ansible target and verify the
   expected result without displaying the injected value.
6. Change a synthetic value in Bitwarden, start a new execution, and verify it
   observes the change. Revoke the test token and verify a subsequent execution
   fails before Ansible starts. Confirm that logs and persistent directories
   contain neither synthetic values nor the bootstrap token.

A mocked-provider test does not prove this external recovery procedure. Record
live acceptance evidence separately from repository test results. Existing
secret copies must remain recoverable until external storage and recovery have
been verified; remove superseded local or Ansible Vault copies after that check.
A credential committed in plaintext requires rotation, even if removed from HEAD.
History rewriting requires a separately agreed scope.

Changing providers
~~~~~~~~~~~~~~~~~~

Implement the same retrieval contract in another adapter, then migrate data using
that provider's supported tools. Maintain a controlled correspondence between
each logical name, the old identifier, and the new identifier. Keep exported
values out of Git, command output, and persistent plaintext staging files.
Verify destination values and recovery with a test execution before switching
host configuration and retiring old access. Only the adapter, host configuration,
mappings and bootstrap credential change; program declarations and Ansible roles
retain their logical names. Atlas does not currently ship other adapters.

Official setup details are available in the `SDK documentation
<https://bitwarden.com/help/secrets-manager-sdk/>`_ and `machine account guidance
<https://bitwarden.com/help/machine-accounts/>`_.

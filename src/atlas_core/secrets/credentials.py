"""Read owner-only bootstrap credentials without following symlinks."""

import os
import stat
from pathlib import Path

from .provider import SecretConfigurationError


def read_credential(path: Path) -> str:
    """Read a regular, caller-owned, mode 0600 file through pinned directories."""
    if not path.is_absolute():
        raise SecretConfigurationError("credential file must be absolute")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor = -1
    try:
        for component in path.parts[1:-1]:
            following = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            os.close(directory)
            directory = following
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = handle.read(65537).strip()
        if not value or len(value) > 65536:
            raise ValueError
        return value
    except (OSError, ValueError):
        raise SecretConfigurationError("bootstrap credential is unavailable or unsafe") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)

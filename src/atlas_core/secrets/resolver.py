"""Validate names and resolve a complete set before a consumer starts work."""

import re
from collections.abc import Callable

from .provider import SecretConfigurationError, SecretResolutionError

_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,2}\Z")


def validate_name(name: object) -> str:
    """Accept system.purpose[.detail] without reflecting invalid input."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise SecretConfigurationError("invalid logical secret name")
    return name


class SecretResolver:
    """Translate logical names and cache successful reads for this instance only."""

    def __init__(
        self,
        mappings: dict[str, str],
        fetch: Callable[[list[str]], dict[str, str]],
    ) -> None:
        self._mappings = {validate_name(name): identifier for name, identifier in mappings.items()}
        self._fetch = fetch
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        """Resolve one required logical name."""
        return self.get_many([name])[name]

    def get_many(self, names: list[str]) -> dict[str, str]:
        """Resolve the whole request without accepting missing or empty values."""
        names = list(dict.fromkeys(validate_name(name) for name in names))
        missing = [name for name in names if name not in self._mappings]
        if missing:
            raise SecretConfigurationError("missing secret mappings: " + ", ".join(missing))
        pending = [name for name in names if name not in self._cache]
        if pending:
            identifiers = list(dict.fromkeys(self._mappings[name] for name in pending))
            try:
                values = self._fetch(identifiers)
                if not isinstance(values, dict):
                    raise TypeError
                missing = [
                    name for name in pending
                    if not isinstance(values.get(self._mappings[name]), str)
                    or not values[self._mappings[name]]
                ]
            except SecretConfigurationError:
                raise
            except Exception:
                raise SecretResolutionError("required secrets could not be resolved") from None
            if missing:
                raise SecretResolutionError("unresolved required secrets: " + ", ".join(missing))
            self._cache.update({name: values[self._mappings[name]] for name in pending})
        return {name: self._cache[name] for name in names}

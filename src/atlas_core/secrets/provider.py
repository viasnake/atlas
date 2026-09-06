"""Provider-neutral retrieval contracts and safe errors."""

from typing import Protocol


class SecretConfigurationError(ValueError):
    """A local secret configuration cannot be used."""


class SecretResolutionError(RuntimeError):
    """Required secrets could not be retrieved; values are never diagnostic data."""


class SecretProvider(Protocol):
    """Resolve logical names within one execution; never persist retrieved values."""

    def get(self, name: str) -> str:
        """Return a required value or raise SecretResolutionError."""
        ...

    def get_many(self, names: list[str]) -> dict[str, str]:
        """Return all required values or fail without a partial result."""
        ...

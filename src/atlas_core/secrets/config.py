"""Load host-owned provider configuration without exposing raw YAML errors."""

import importlib
import re
from pathlib import Path

from atlas.yamlutil import load_yaml_file
from atlas_core.paths import get_paths

from .provider import SecretConfigurationError, SecretProvider


def load_provider(path: Path | None = None) -> SecretProvider:
    """Construct an execution-scoped provider from the Atlas secrets.yml file."""
    path = get_paths().etc / "secrets.yml" if path is None else path
    try:
        raw = load_yaml_file(path)
        if not isinstance(raw, dict) or set(raw) != {"provider", "config", "mappings"}:
            raise ValueError
        name = raw["provider"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError
        if not isinstance(raw["config"], dict) or not isinstance(raw["mappings"], dict):
            raise TypeError
    except Exception:
        raise SecretConfigurationError("invalid secret provider configuration") from None
    try:
        module = importlib.import_module(f"atlas_core.secrets.providers.{name}")
    except ImportError:
        raise SecretConfigurationError("configured secret provider is unavailable") from None
    return module.create_provider(raw["config"], raw["mappings"])

"""Read-only Bitwarden Secrets Manager Cloud integration through its official SDK."""

from pathlib import Path
from uuid import UUID

from ..credentials import read_credential
from ..provider import SecretConfigurationError, SecretResolutionError
from ..resolver import SecretResolver, validate_name


def create_provider(config: dict, mappings: dict) -> SecretResolver:
    """Validate adapter-specific configuration before authentication."""
    try:
        if set(config) != {"region", "credential_file", "project_id"}:
            raise ValueError
        region = config["region"]
        if region not in ("us", "eu"):
            raise ValueError
        credential = Path(config["credential_file"])
        if not credential.is_absolute():
            raise ValueError
        project = UUID(config["project_id"])
        identifiers = {}
        for name, entry in mappings.items():
            validate_name(name)
            if not isinstance(entry, dict) or set(entry) != {"secret_id"}:
                raise ValueError
            identifiers[name] = str(UUID(entry["secret_id"]))
    except (TypeError, ValueError, AttributeError):
        raise SecretConfigurationError("invalid secret provider settings or mappings") from None
    return SecretResolver(identifiers, _Reader(region, credential, project).fetch)


class _Reader:
    def __init__(self, region: str, credential: Path, project: UUID) -> None:
        self.region = region
        self.credential = credential
        self.project = project
        self.client = None

    def fetch(self, identifiers: list[str]) -> dict[str, str]:
        """Fetch only requested IDs; never create an SDK state file."""
        try:
            if self.client is None:
                from bitwarden_sdk import BitwardenClient, client_settings_from_dict

                suffix = "com" if self.region == "us" else "eu"
                client = BitwardenClient(client_settings_from_dict({
                    "apiUrl": f"https://api.bitwarden.{suffix}",
                    "identityUrl": f"https://identity.bitwarden.{suffix}",
                }))
                token = read_credential(self.credential)
                try:
                    response = client.auth().login_access_token(token, state_file=None)
                finally:
                    del token
                if response.success is not True or response.data.authenticated is not True:
                    raise ValueError
                self.client = client
            response = self.client.secrets().get_by_ids([UUID(value) for value in identifiers])
            if response.success is not True:
                raise ValueError
            values = {}
            for secret in response.data.data:
                identifier = str(secret.id)
                if (
                    identifier not in identifiers
                    or identifier in values
                    or secret.project_id != self.project
                    or not isinstance(secret.value, str)
                    or not secret.value
                ):
                    raise ValueError
                values[identifier] = secret.value
            return values
        except SecretConfigurationError:
            raise
        except Exception:
            raise SecretResolutionError("required secrets could not be resolved") from None

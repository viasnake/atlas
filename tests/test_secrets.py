"""Secret resolution must fail before consumers receive partial or unsafe values."""

import traceback

import pytest

from atlas_core.secrets import (
    SecretConfigurationError,
    SecretResolutionError,
    SecretResolver,
)
from atlas_core.secrets.credentials import read_credential


def test_retrieval_and_execution_cache():
    calls = []

    def fetch(ids):
        calls.append(ids)
        return {key: "synthetic-test-value" for key in ids}

    provider = SecretResolver({"mysql.backup.password": "id", "mysql.backup.username": "user"}, fetch)
    assert provider.get("mysql.backup.password") == "synthetic-test-value"
    assert provider.get_many(["mysql.backup.password", "mysql.backup.username"]) == {
        "mysql.backup.password": "synthetic-test-value", "mysql.backup.username": "synthetic-test-value"
    }
    assert calls == [["id"], ["user"]]
    assert provider.get_many([]) == {}


@pytest.mark.parametrize("name", ["", "single", "a.b.c.d", "bad\n.name", "A.b", 3])
def test_invalid_names(name):
    with pytest.raises(SecretConfigurationError, match="invalid logical"):
        SecretResolver({name: "id"}, lambda ids: {})


def test_missing_mappings_do_not_fetch():
    provider = SecretResolver({}, lambda ids: pytest.fail("must validate first"))
    with pytest.raises(SecretConfigurationError, match=r"mysql\.backup\.password, cloudflare\.api_token"):
        provider.get_many(["mysql.backup.password", "cloudflare.api_token"])


@pytest.mark.parametrize("result", [{}, {"id": ""}, {"id": 123}, None])
def test_missing_or_malformed_value(result):
    provider = SecretResolver({"mysql.backup.password": "id"}, lambda ids: result)
    with pytest.raises(SecretResolutionError):
        provider.get("mysql.backup.password")


def test_provider_exception_is_not_rendered():
    def fetch(ids):
        raise RuntimeError("synthetic-credential-must-not-appear")

    provider = SecretResolver({"mysql.backup.password": "id"}, fetch)
    try:
        provider.get("mysql.backup.password")
    except SecretResolutionError:
        assert "synthetic-credential-must-not-appear" not in traceback.format_exc()
    else:
        pytest.fail("provider error was ignored")


def test_safe_credential(tmp_path):
    path = tmp_path / "credential"
    path.write_text("synthetic-bootstrap\n")
    path.chmod(0o600)
    assert read_credential(path) == "synthetic-bootstrap"
    path.chmod(0o640)
    with pytest.raises(SecretConfigurationError):
        read_credential(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(SecretConfigurationError):
        read_credential(link)


def test_configuration_and_diagnostic(tmp_path, monkeypatch, capsys):
    from atlas.cli import main
    from atlas_core.secrets import load_provider
    from atlas_core.secrets.providers import bitwarden

    path = tmp_path / "secrets.yml"
    monkeypatch.setenv("ATLAS_ETC_DIR", str(tmp_path))
    path.write_text("provider: bitwarden\nconfig: {}\nmappings: {}\n")
    provider = SecretResolver({"mysql.backup.password": "id"}, lambda ids: {"id": "hidden"})
    monkeypatch.setattr(bitwarden, "create_provider", lambda config, mappings: provider)
    assert load_provider() is provider
    assert main(["secret", "check", "mysql.backup.password"]) == 0
    assert capsys.readouterr().out == "secrets: 1/1 available\n"
    for text in ["not a mapping", "provider: x\nprovider: y", "provider: missing\nconfig: {}\nmappings: {}", "provider: '../x'\nconfig: {}\nmappings: {}", "provider: x\nconfig: []\nmappings: {}"]:
        path.write_text(text)
        with pytest.raises(SecretConfigurationError):
            load_provider(path)


def test_adapter_validates_authentication_response_and_project(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace as NS
    from uuid import UUID

    from atlas_core.secrets.providers.bitwarden import create_provider

    secret_id = UUID("00000000-0000-4000-8000-000000000001")
    project_id = UUID("00000000-0000-4000-8000-000000000002")
    credential = tmp_path / "credential"
    credential.write_text("synthetic-bootstrap")
    credential.chmod(0o600)
    login = NS(success=True, data=NS(authenticated=True))
    secret = NS(id=secret_id, project_id=project_id, value="synthetic-value")
    response = NS(success=True, data=NS(data=[secret]))
    calls = []

    def authenticate(token, *, state_file):
        assert token == "synthetic-bootstrap"
        assert state_file is None
        calls.append("login")
        return login

    client = NS(
        auth=lambda: NS(login_access_token=authenticate),
        secrets=lambda: NS(get_by_ids=lambda ids: response),
    )
    monkeypatch.setitem(sys.modules, "bitwarden_sdk", NS(
        BitwardenClient=lambda settings: client,
        client_settings_from_dict=lambda settings: settings,
    ))
    config = {"region": "eu", "credential_file": str(credential), "project_id": str(project_id)}
    mappings = {"mysql.backup.password": {"secret_id": str(secret_id)}}
    provider = create_provider(config, mappings)
    assert provider.get("mysql.backup.password") == "synthetic-value"
    assert provider.get("mysql.backup.password") == "synthetic-value"
    assert calls == ["login"]
    for invalid in [{}, {**config, "region": "other"}, {**config, "credential_file": "relative"}, {**config, "project_id": "bad"}]:
        with pytest.raises(SecretConfigurationError):
            create_provider(invalid, mappings)
    for invalid in [{"bad": {}}, {"mysql.backup.password": {}}, {"mysql.backup.password": {"secret_id": "bad"}}]:
        with pytest.raises(SecretConfigurationError):
            create_provider(config, invalid)
    login.success = False
    with pytest.raises(SecretResolutionError):
        create_provider(config, mappings).get("mysql.backup.password")
    login.success = True
    response.success = False
    with pytest.raises(SecretResolutionError):
        create_provider(config, mappings).get("mysql.backup.password")
    response.success = True
    secret.project_id = secret_id
    with pytest.raises(SecretResolutionError):
        create_provider(config, mappings).get("mysql.backup.password")


def test_empty_oversized_and_relative_credentials(tmp_path):
    from pathlib import Path

    with pytest.raises(SecretConfigurationError):
        read_credential(Path("relative"))
    path = tmp_path / "credential"
    path.touch(mode=0o600)
    for value in ["", "x" * 65537]:
        path.write_text(value)
        with pytest.raises(SecretConfigurationError):
            read_credential(path)


def test_adapter_reuses_authenticated_client(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from uuid import UUID

    from atlas_core.secrets.providers.bitwarden import _Reader

    identifier = UUID("00000000-0000-4000-8000-000000000001")
    project = UUID("00000000-0000-4000-8000-000000000002")
    response = NS(success=True, data=NS(data=[NS(id=identifier, project_id=project, value="synthetic")]))
    reader = _Reader("us", tmp_path / "unused", project)
    reader.client = NS(secrets=lambda: NS(get_by_ids=lambda ids: response))
    assert reader.fetch([str(identifier)]) == {str(identifier): "synthetic"}


def test_credential_configuration_errors_remain_distinct(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace as NS

    from atlas_core.secrets.providers.bitwarden import create_provider

    monkeypatch.setitem(sys.modules, "bitwarden_sdk", NS(
        BitwardenClient=lambda settings: object(),
        client_settings_from_dict=lambda settings: settings,
    ))
    provider = create_provider({
        "region": "us", "credential_file": str(tmp_path / "absent"),
        "project_id": "00000000-0000-4000-8000-000000000001",
    }, {"mysql.backup.password": {"secret_id": "00000000-0000-4000-8000-000000000002"}})
    with pytest.raises(SecretConfigurationError):
        provider.get("mysql.backup.password")


def test_official_sdk_response_contract(tmp_path, monkeypatch):
    sdk = pytest.importorskip("bitwarden_sdk")
    from atlas_core.secrets.providers.bitwarden import create_provider

    credential = tmp_path / "credential"
    credential.write_text("synthetic-bootstrap")
    credential.chmod(0o600)
    secret_id = "00000000-0000-4000-8000-000000000001"
    project_id = "00000000-0000-4000-8000-000000000002"
    client = sdk.BitwardenClient(sdk.client_settings_from_dict({
        "apiUrl": "https://api.bitwarden.com", "identityUrl": "https://identity.bitwarden.com",
    }))
    calls = []

    def response(command):
        calls.append(command.to_dict())
        if len(calls) == 1:
            return {"success": True, "data": {"authenticated": True, "forcePasswordReset": False, "resetMasterPassword": False}}
        return {"success": True, "data": {"data": [{
            "id": secret_id, "projectId": project_id,
            "organizationId": "00000000-0000-4000-8000-000000000003",
            "creationDate": "2026-01-01T00:00:00Z", "revisionDate": "2026-01-01T00:00:00Z",
            "key": "test", "note": "", "value": "synthetic-sdk-value",
        }]}}

    monkeypatch.setattr(client, "_run_command", response)
    monkeypatch.setattr(sdk, "BitwardenClient", lambda settings: client)
    provider = create_provider({"region": "us", "credential_file": str(credential), "project_id": project_id},
                               {"mysql.backup.password": {"secret_id": secret_id}})
    assert provider.get("mysql.backup.password") == "synthetic-sdk-value"
    assert calls[0]["loginAccessToken"].get("stateFile") is None
    assert calls[1]["secrets"]["getByIds"]["ids"] == [secret_id]

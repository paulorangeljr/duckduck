"""Unit tests for AzureKeyVaultSecrets (Azure Key Vault)."""

import json
from unittest.mock import MagicMock

import pytest

from duckduck import AzureKeyVaultSecrets


def _fake_client(secret_dict):
    client = MagicMock()
    client.get_secret.return_value = MagicMock(value=json.dumps(secret_dict))
    return client


def test_get_secret_parses_json():
    client = _fake_client({"host": "x", "username": "a", "password": "b"})
    kv = AzureKeyVaultSecrets(client=client)

    secret = kv.get_secret("prod-insightvm")

    assert secret == {"host": "x", "username": "a", "password": "b"}
    client.get_secret.assert_called_once_with("prod-insightvm")


def test_get_secret_is_cached():
    client = _fake_client({"host": "x", "username": "a", "password": "b"})
    kv = AzureKeyVaultSecrets(client=client)

    kv.get_secret("prod-insightvm")
    kv.get_secret("prod-insightvm")

    client.get_secret.assert_called_once()  # second call uses the cache


def test_get_secret_different_ids_not_cached_together():
    client = MagicMock()
    client.get_secret.side_effect = [
        MagicMock(value=json.dumps({"a": 1})),
        MagicMock(value=json.dumps({"b": 2})),
    ]
    kv = AzureKeyVaultSecrets(client=client)

    assert kv.get_secret("secret-a") == {"a": 1}
    assert kv.get_secret("secret-b") == {"b": 2}
    assert client.get_secret.call_count == 2


def test_get_secret_non_json_value_raises():
    client = MagicMock()
    client.get_secret.return_value = MagicMock(value="not-json")
    kv = AzureKeyVaultSecrets(client=client)

    with pytest.raises(ValueError, match="not valid JSON"):
        kv.get_secret("bad-secret")


def test_without_client_or_sdk_raises_import_error(monkeypatch):
    import duckduck.azure_secrets as azure_secrets_module

    monkeypatch.setattr(azure_secrets_module, "SecretClient", None)
    with pytest.raises(ImportError, match="azure-keyvault-secrets"):
        AzureKeyVaultSecrets(vault_url="https://my-vault.vault.azure.net/")


def test_client_built_with_vault_url_and_credential():
    fake_credential = MagicMock()
    with_client_mock = MagicMock()

    import duckduck.azure_secrets as azure_secrets_module

    class _FakeSecretClient:
        def __init__(self, vault_url, credential):
            self.vault_url = vault_url
            self.credential = credential
            with_client_mock(vault_url=vault_url, credential=credential)

    real_secret_client = azure_secrets_module.SecretClient
    try:
        azure_secrets_module.SecretClient = _FakeSecretClient
        AzureKeyVaultSecrets(
            vault_url="https://my-vault.vault.azure.net/", credential=fake_credential
        )
    finally:
        azure_secrets_module.SecretClient = real_secret_client

    with_client_mock.assert_called_once_with(
        vault_url="https://my-vault.vault.azure.net/", credential=fake_credential
    )


# ---------------------------------------------------------------------------
# tenant_id — pinning DefaultAzureCredential to a specific Azure AD tenant
# ---------------------------------------------------------------------------


def test_tenant_id_passed_to_default_azure_credential(monkeypatch):
    import duckduck.azure_secrets as azure_secrets_module

    fake_default_credential_cls = MagicMock()
    fake_secret_client_cls = MagicMock()
    monkeypatch.setattr(azure_secrets_module, "DefaultAzureCredential", fake_default_credential_cls)
    monkeypatch.setattr(azure_secrets_module, "SecretClient", fake_secret_client_cls)

    AzureKeyVaultSecrets(vault_url="https://my-vault.vault.azure.net/", tenant_id="tenant-123")

    fake_default_credential_cls.assert_called_once_with(
        interactive_browser_tenant_id="tenant-123",
        additionally_allowed_tenants=["tenant-123"],
    )
    fake_secret_client_cls.assert_called_once_with(
        vault_url="https://my-vault.vault.azure.net/",
        credential=fake_default_credential_cls.return_value,
    )


def test_no_tenant_id_uses_plain_default_credential(monkeypatch):
    import duckduck.azure_secrets as azure_secrets_module

    fake_default_credential_cls = MagicMock()
    fake_secret_client_cls = MagicMock()
    monkeypatch.setattr(azure_secrets_module, "DefaultAzureCredential", fake_default_credential_cls)
    monkeypatch.setattr(azure_secrets_module, "SecretClient", fake_secret_client_cls)

    AzureKeyVaultSecrets(vault_url="https://my-vault.vault.azure.net/")

    fake_default_credential_cls.assert_called_once_with()


def test_explicit_credential_skips_default_azure_credential(monkeypatch):
    import duckduck.azure_secrets as azure_secrets_module

    fake_default_credential_cls = MagicMock()
    fake_secret_client_cls = MagicMock()
    monkeypatch.setattr(azure_secrets_module, "DefaultAzureCredential", fake_default_credential_cls)
    monkeypatch.setattr(azure_secrets_module, "SecretClient", fake_secret_client_cls)

    my_credential = MagicMock()
    AzureKeyVaultSecrets(
        vault_url="https://my-vault.vault.azure.net/",
        tenant_id="tenant-123",
        credential=my_credential,
    )

    fake_default_credential_cls.assert_not_called()
    fake_secret_client_cls.assert_called_once_with(
        vault_url="https://my-vault.vault.azure.net/", credential=my_credential
    )

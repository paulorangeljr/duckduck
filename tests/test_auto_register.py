"""Unit tests for DuckAPI.auto_register (automatic wrapper instantiation)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from duckduck import AzureKeyVaultSecrets, DuckAPI, SecretsManager, SharePoint


def _mock_msal():
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
    return mock_app


def _fake_aws_client(secret_dict):
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": json.dumps(secret_dict)}
    return client


def _fake_azure_client(secret_dict):
    client = MagicMock()
    client.get_secret.return_value = MagicMock(value=json.dumps(secret_dict))
    return client


# ---------------------------------------------------------------------------
# authentication.type = "local" — fully hardcoded, no secret store
# ---------------------------------------------------------------------------


def test_auto_register_local_credentials():
    duck = DuckAPI()

    instances = duck.auto_register({
        "insightvm": {
            "host": "console.local",
            "authentication": {"type": "local", "username": "a", "password": "b"},
        },
    })

    assert instances["insightvm"].base_url == "https://console.local/api/3"
    assert "insightvm_assets" in duck.functions
    assert "insightvm_vulnerabilities" in duck.functions
    assert "insightvm_assets" in duck._streaming_functions
    duck.close()


def test_auto_register_host_inside_authentication_also_works():
    """host can live in the authentication block itself, not just top-level."""
    duck = DuckAPI()
    instances = duck.auto_register({
        "insightvm": {
            "authentication": {
                "type": "local", "host": "console.local", "username": "a", "password": "b",
            },
        },
    })
    assert instances["insightvm"].base_url == "https://console.local/api/3"
    duck.close()


def test_auto_register_missing_authentication_block_raises():
    duck = DuckAPI()
    with pytest.raises(ValueError, match="authentication"):
        duck.auto_register({"insightvm": {"host": "console.local"}})
    duck.close()


def test_auto_register_unknown_connector_raises():
    duck = DuckAPI()
    with pytest.raises(ValueError, match="is not recognized"):
        duck.auto_register({
            "does_not_exist": {"authentication": {"type": "local"}},
        })
    duck.close()


def test_auto_register_unknown_authentication_type_raises():
    duck = DuckAPI()
    with pytest.raises(ValueError, match="'local', 'aws' or 'azure'"):
        duck.auto_register({
            "insightvm": {
                "host": "h",
                "authentication": {"type": "gcp", "username": "a", "password": "b"},
            },
        })
    duck.close()


def test_auto_register_multiple_instances_same_connector():
    """Explicit connector= allows two instances of the same wrapper with distinct prefixes."""
    duck = DuckAPI()
    instances = duck.auto_register({
        "insightvm_prod": {
            "connector": "insightvm",
            "host": "prod.local",
            "authentication": {"type": "local", "username": "a", "password": "b"},
        },
        "insightvm_dev": {
            "connector": "insightvm",
            "host": "dev.local",
            "authentication": {"type": "local", "username": "a", "password": "b"},
        },
    })

    assert instances["insightvm_prod"].base_url == "https://prod.local/api/3"
    assert instances["insightvm_dev"].base_url == "https://dev.local/api/3"
    assert "insightvm_prod_assets" in duck.functions
    assert "insightvm_dev_assets" in duck.functions
    duck.close()


def test_auto_register_extra_kwargs_passed_to_constructor():
    duck = DuckAPI()
    instances = duck.auto_register({
        "insightvm": {
            "host": "h",
            "default_page_size": 50,
            "authentication": {"type": "local", "username": "u", "password": "p"},
        },
    })
    assert instances["insightvm"].default_page_size == 50
    duck.close()


# ---------------------------------------------------------------------------
# authentication.type = "aws" — AWS Secrets Manager
# ---------------------------------------------------------------------------


def test_auto_register_aws_authentication():
    fake_client = _fake_aws_client({"host": "x.local", "username": "u", "password": "p"})
    secrets_manager = SecretsManager(client=fake_client)

    duck = DuckAPI()
    instances = duck.auto_register(
        {
            "insightvm": {
                "authentication": {
                    "type": "aws", "region_name": "us-east-1", "secret_id": "prod/insightvm",
                },
            },
        },
        secrets={"aws": secrets_manager},
    )

    fake_client.get_secret_value.assert_called_once_with(SecretId="prod/insightvm")
    assert instances["insightvm"].base_url == "https://x.local/api/3"
    duck.close()


def test_auto_register_aws_missing_secret_id_raises():
    duck = DuckAPI()
    with pytest.raises(ValueError, match="requires 'secret_id'"):
        duck.auto_register({
            "insightvm": {
                "authentication": {"type": "aws", "region_name": "us-east-1"},
            },
        })
    duck.close()


def test_auto_register_aws_field_override_via_secret_ref():
    """A field written as "$secret.<key>" is pulled from that key in the fetched secret."""
    fake_client = _fake_aws_client({"host": "x.local", "username": "u", "svc_password": "p"})
    secrets_manager = SecretsManager(client=fake_client)

    duck = DuckAPI()
    instances = duck.auto_register(
        {
            "insightvm": {
                "authentication": {
                    "type": "aws",
                    "secret_id": "prod/insightvm",
                    "password": "$secret.svc_password",
                },
            },
        },
        secrets={"aws": secrets_manager},
    )

    # InsightVM doesn't expose the password, but base_url proves construction succeeded
    # using the remapped field (from_secret would KeyError on secret["password"] otherwise).
    assert instances["insightvm"].base_url == "https://x.local/api/3"
    duck.close()


def test_auto_register_aws_field_override_missing_key_raises():
    fake_client = _fake_aws_client({"host": "x.local", "username": "u", "password": "p"})
    secrets_manager = SecretsManager(client=fake_client)

    duck = DuckAPI()
    with pytest.raises(ValueError, match="missing key 'does_not_exist'"):
        duck.auto_register(
            {
                "insightvm": {
                    "authentication": {
                        "type": "aws",
                        "secret_id": "prod/insightvm",
                        "password": "$secret.does_not_exist",
                    },
                },
            },
            secrets={"aws": secrets_manager},
        )
    duck.close()


def test_auto_register_aws_literal_field_override():
    """A field with a plain (non-$secret.) value overrides the secret's own key."""
    fake_client = _fake_aws_client({"host": "wrong.local", "username": "u", "password": "p"})
    secrets_manager = SecretsManager(client=fake_client)

    duck = DuckAPI()
    instances = duck.auto_register(
        {
            "insightvm": {
                "authentication": {
                    "type": "aws",
                    "secret_id": "prod/insightvm",
                    "host": "override.local",
                },
            },
        },
        secrets={"aws": secrets_manager},
    )

    assert instances["insightvm"].base_url == "https://override.local/api/3"
    duck.close()


def test_auto_register_aws_without_override_builds_secrets_manager(monkeypatch):
    """With no secrets= override, a SecretsManager is auto-built from region_name."""
    import duckduck.secrets as secrets_module

    fake_client = _fake_aws_client({"host": "x.local", "username": "u", "password": "p"})
    fake_boto3 = MagicMock()
    fake_boto3.Session.return_value.client.return_value = fake_client
    monkeypatch.setattr(secrets_module, "boto3", fake_boto3)

    duck = DuckAPI()
    instances = duck.auto_register({
        "insightvm": {
            "authentication": {
                "type": "aws", "region_name": "us-east-1", "secret_id": "prod/insightvm",
            },
        },
    })

    fake_boto3.Session.return_value.client.assert_called_once_with("secretsmanager", region_name="us-east-1")
    assert instances["insightvm"].base_url == "https://x.local/api/3"
    duck.close()


def test_auto_register_aws_without_boto3_or_override_raises_import_error():
    """No override and boto3 not installed: a clear ImportError, not a confusing one."""
    duck = DuckAPI()
    with pytest.raises(ImportError, match="boto3"):
        duck.auto_register({
            "insightvm": {
                "authentication": {
                    "type": "aws", "region_name": "us-east-1", "secret_id": "prod/insightvm",
                },
            },
        })
    duck.close()


def test_auto_register_aws_backend_shared_across_same_region(monkeypatch):
    """Two services in the same region reuse a single auto-built SecretsManager."""
    import duckduck.secrets as secrets_module

    fake_client = MagicMock()
    fake_client.get_secret_value.side_effect = [
        {"SecretString": json.dumps({"host": "a.local", "username": "u", "password": "p"})},
        {"SecretString": json.dumps({"host": "b.local", "username": "u", "password": "p"})},
    ]
    fake_boto3 = MagicMock()
    fake_boto3.Session.return_value.client.return_value = fake_client
    monkeypatch.setattr(secrets_module, "boto3", fake_boto3)

    duck = DuckAPI()
    duck.auto_register({
        "insightvm_a": {
            "connector": "insightvm",
            "authentication": {
                "type": "aws", "region_name": "us-east-1", "secret_id": "svc-a",
            },
        },
        "insightvm_b": {
            "connector": "insightvm",
            "authentication": {
                "type": "aws", "region_name": "us-east-1", "secret_id": "svc-b",
            },
        },
    })

    fake_boto3.Session.return_value.client.assert_called_once_with("secretsmanager", region_name="us-east-1")
    duck.close()


def test_auto_register_aws_backend_separate_per_region(monkeypatch):
    """Two services in different regions get their own SecretsManager/client."""
    import duckduck.secrets as secrets_module

    fake_boto3 = MagicMock()
    fake_boto3.Session.return_value.client.side_effect = [
        _fake_aws_client({"host": "a.local", "username": "u", "password": "p"}),
        _fake_aws_client({"host": "b.local", "username": "u", "password": "p"}),
    ]
    monkeypatch.setattr(secrets_module, "boto3", fake_boto3)

    duck = DuckAPI()
    duck.auto_register({
        "insightvm_a": {
            "connector": "insightvm",
            "authentication": {
                "type": "aws", "region_name": "us-east-1", "secret_id": "svc-a",
            },
        },
        "insightvm_b": {
            "connector": "insightvm",
            "authentication": {
                "type": "aws", "region_name": "eu-west-1", "secret_id": "svc-b",
            },
        },
    })

    assert fake_boto3.Session.return_value.client.call_count == 2
    duck.close()


def test_auto_register_aws_profile_name_passed_through(monkeypatch):
    """authentication.profile_name selects a specific AWS account via boto3.Session."""
    import duckduck.secrets as secrets_module

    fake_boto3 = MagicMock()
    fake_boto3.Session.return_value.client.return_value = _fake_aws_client(
        {"host": "prod.local", "username": "u", "password": "p"}
    )
    monkeypatch.setattr(secrets_module, "boto3", fake_boto3)

    duck = DuckAPI()
    instances = duck.auto_register({
        "insightvm": {
            "authentication": {
                "type": "aws",
                "region_name": "us-east-1",
                "profile_name": "prod-account",
                "secret_id": "prod/insightvm",
            },
        },
    })

    fake_boto3.Session.assert_called_once_with(profile_name="prod-account")
    assert instances["insightvm"].base_url == "https://prod.local/api/3"
    duck.close()


def test_auto_register_aws_backend_separate_per_profile(monkeypatch):
    """Same region, different profile_name: two separate SecretsManager/client instances."""
    import duckduck.secrets as secrets_module

    fake_boto3 = MagicMock()
    fake_boto3.Session.return_value.client.side_effect = [
        _fake_aws_client({"host": "a.local", "username": "u", "password": "p"}),
        _fake_aws_client({"host": "b.local", "username": "u", "password": "p"}),
    ]
    monkeypatch.setattr(secrets_module, "boto3", fake_boto3)

    duck = DuckAPI()
    duck.auto_register({
        "insightvm_a": {
            "connector": "insightvm",
            "authentication": {
                "type": "aws", "region_name": "us-east-1", "profile_name": "account-a",
                "secret_id": "svc-a",
            },
        },
        "insightvm_b": {
            "connector": "insightvm",
            "authentication": {
                "type": "aws", "region_name": "us-east-1", "profile_name": "account-b",
                "secret_id": "svc-b",
            },
        },
    })

    assert fake_boto3.Session.return_value.client.call_count == 2
    duck.close()


# ---------------------------------------------------------------------------
# authentication.type = "azure" — Azure Key Vault
# ---------------------------------------------------------------------------


def test_auto_register_azure_authentication():
    fake_client = _fake_azure_client({"host": "x.local", "username": "u", "password": "p"})
    backend = AzureKeyVaultSecrets(client=fake_client)

    duck = DuckAPI()
    instances = duck.auto_register(
        {
            "insightvm": {
                "authentication": {
                    "type": "azure",
                    "vault_url": "https://my-vault.vault.azure.net/",
                    "secret_id": "prod-insightvm",
                },
            },
        },
        secrets={"azure": backend},
    )

    fake_client.get_secret.assert_called_once_with("prod-insightvm")
    assert instances["insightvm"].base_url == "https://x.local/api/3"
    duck.close()


def test_auto_register_azure_missing_vault_url_raises():
    duck = DuckAPI()
    with pytest.raises(ValueError, match="requires 'vault_url'"):
        duck.auto_register({
            "insightvm": {
                "authentication": {"type": "azure", "secret_id": "prod-insightvm"},
            },
        })
    duck.close()


def test_auto_register_azure_tenant_id_passed_through(monkeypatch):
    """authentication.tenant_id pins DefaultAzureCredential to a specific Azure AD tenant."""
    import duckduck.azure_secrets as azure_secrets_module

    fake_credential_cls = MagicMock()
    fake_client = _fake_azure_client({"host": "x.local", "username": "u", "password": "p"})
    fake_secret_client_cls = MagicMock(return_value=fake_client)
    monkeypatch.setattr(azure_secrets_module, "DefaultAzureCredential", fake_credential_cls)
    monkeypatch.setattr(azure_secrets_module, "SecretClient", fake_secret_client_cls)

    duck = DuckAPI()
    instances = duck.auto_register({
        "insightvm": {
            "authentication": {
                "type": "azure",
                "vault_url": "https://my-vault.vault.azure.net/",
                "tenant_id": "tenant-123",
                "secret_id": "prod-insightvm",
            },
        },
    })

    fake_credential_cls.assert_called_once_with(
        interactive_browser_tenant_id="tenant-123",
        additionally_allowed_tenants=["tenant-123"],
    )
    assert instances["insightvm"].base_url == "https://x.local/api/3"
    duck.close()


def test_auto_register_azure_backend_separate_per_tenant(monkeypatch):
    """Same vault_url, different tenant_id: two separate AzureKeyVaultSecrets instances."""
    import duckduck.azure_secrets as azure_secrets_module

    fake_credential_cls = MagicMock()
    fake_secret_client_cls = MagicMock()
    fake_secret_client_cls.side_effect = [
        _fake_azure_client({"host": "a.local", "username": "u", "password": "p"}),
        _fake_azure_client({"host": "b.local", "username": "u", "password": "p"}),
    ]
    monkeypatch.setattr(azure_secrets_module, "DefaultAzureCredential", fake_credential_cls)
    monkeypatch.setattr(azure_secrets_module, "SecretClient", fake_secret_client_cls)

    duck = DuckAPI()
    duck.auto_register({
        "insightvm_a": {
            "connector": "insightvm",
            "authentication": {
                "type": "azure",
                "vault_url": "https://my-vault.vault.azure.net/",
                "tenant_id": "tenant-a",
                "secret_id": "svc-a",
            },
        },
        "insightvm_b": {
            "connector": "insightvm",
            "authentication": {
                "type": "azure",
                "vault_url": "https://my-vault.vault.azure.net/",
                "tenant_id": "tenant-b",
                "secret_id": "svc-b",
            },
        },
    })

    assert fake_secret_client_cls.call_count == 2
    duck.close()


def test_auto_register_azure_field_override_via_secret_ref():
    fake_client = _fake_azure_client({"host": "x.local", "username": "u", "svc_password": "p"})
    backend = AzureKeyVaultSecrets(client=fake_client)

    duck = DuckAPI()
    instances = duck.auto_register(
        {
            "insightvm": {
                "authentication": {
                    "type": "azure",
                    "vault_url": "https://my-vault.vault.azure.net/",
                    "secret_id": "prod-insightvm",
                    "password": "$secret.svc_password",
                },
            },
        },
        secrets={"azure": backend},
    )

    assert instances["insightvm"].base_url == "https://x.local/api/3"
    duck.close()


def test_auto_register_azure_without_sdk_or_override_raises_import_error():
    duck = DuckAPI()
    with pytest.raises(ImportError, match="azure-keyvault-secrets"):
        duck.auto_register({
            "insightvm": {
                "authentication": {
                    "type": "azure",
                    "vault_url": "https://my-vault.vault.azure.net/",
                    "secret_id": "prod-insightvm",
                },
            },
        })
    duck.close()


# ---------------------------------------------------------------------------
# SharePoint — construction via mocked MSAL
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_auto_register_sharepoint_local(msal_cls):
    msal_cls.return_value = _mock_msal()

    duck = DuckAPI()
    instances = duck.auto_register({
        "sharepoint": {
            "hostname": "company.sharepoint.com",
            "site_path": "/teams/myteam",
            "authentication": {
                "type": "local",
                "tenant_id": "t", "client_id": "c", "client_secret": "s",
            },
        },
    })

    sp = instances["sharepoint"]
    assert isinstance(sp, SharePoint)
    assert sp._default_hostname == "company.sharepoint.com"
    assert sp._default_site_path == "/teams/myteam"
    assert "sharepoint_list_items" in duck.functions
    assert "sharepoint_sites" in duck.functions
    assert "sharepoint_list_items" in duck._streaming_functions
    duck.close()


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_auto_register_sharepoint_aws_thumbprint(msal_cls):
    msal_cls.return_value = _mock_msal()

    fake_client = _fake_aws_client({
        "tenant_id": "t", "client_id": "c",
        "thumbprint": "AABBCC",
        "private_key_pem": "-----BEGIN PRIVATE KEY-----\n...",
    })
    secrets_manager = SecretsManager(client=fake_client)

    duck = DuckAPI()
    instances = duck.auto_register(
        {
            "sharepoint": {
                "authentication": {
                    "type": "aws", "region_name": "us-east-1", "secret_id": "prod/sharepoint",
                },
            },
        },
        secrets={"aws": secrets_manager},
    )

    assert isinstance(instances["sharepoint"], SharePoint)
    duck.close()


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_auto_register_two_different_connectors(msal_cls):
    """SharePoint and InsightVM registered together don't collide (prefix by name)."""
    msal_cls.return_value = _mock_msal()

    duck = DuckAPI()
    duck.auto_register({
        "sharepoint": {
            "authentication": {
                "type": "local", "tenant_id": "t", "client_id": "c", "client_secret": "s",
            },
        },
        "insightvm": {
            "host": "h",
            "authentication": {"type": "local", "username": "u", "password": "p"},
        },
    })

    # both define a "sites" table — without the prefix, one would overwrite the other
    assert "sharepoint_sites" in duck.functions
    assert "insightvm_sites" in duck.functions
    duck.close()


# ---------------------------------------------------------------------------
# Loading services from a JSON file — duck.auto_register() with no arguments
# ---------------------------------------------------------------------------


def _local_insightvm_config():
    return {
        "insightvm": {
            "host": "console.local",
            "authentication": {"type": "local", "username": "a", "password": "b"},
        },
    }


def test_auto_register_no_config_found_raises_helpful_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # empty dir, no duckduck.json
    duck = DuckAPI()
    with pytest.raises(ValueError, match="found no config file"):
        duck.auto_register()
    duck.close()


def test_auto_register_loads_from_default_json_file(tmp_path, monkeypatch):
    """With no services= and no config_path=, reads ./duckduck.json."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "duckduck.json").write_text(json.dumps({"services": _local_insightvm_config()}))

    duck = DuckAPI()
    instances = duck.auto_register()

    assert instances["insightvm"].base_url == "https://console.local/api/3"
    assert "insightvm_assets" in duck.functions
    duck.close()


def test_auto_register_finds_default_json_in_parent_directory(tmp_path, monkeypatch):
    """Called from a subdirectory of the project, still finds duckduck.json at its root."""
    (tmp_path / "duckduck.json").write_text(json.dumps({"services": _local_insightvm_config()}))
    subdir = tmp_path / "scripts" / "nested"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    duck = DuckAPI()
    instances = duck.auto_register()

    assert instances["insightvm"].base_url == "https://console.local/api/3"
    duck.close()


def test_auto_register_config_path_argument(tmp_path):
    config_file = tmp_path / "custom.json"
    config_file.write_text(json.dumps({"services": _local_insightvm_config()}))

    duck = DuckAPI()
    instances = duck.auto_register(config_path=str(config_file))

    assert instances["insightvm"].base_url == "https://console.local/api/3"
    duck.close()


def test_auto_register_duckduck_config_env_var(tmp_path, monkeypatch):
    config_file = tmp_path / "env-config.json"
    config_file.write_text(json.dumps({"services": _local_insightvm_config()}))
    monkeypatch.setenv("DUCKDUCK_CONFIG", str(config_file))

    duck = DuckAPI()
    instances = duck.auto_register()

    assert instances["insightvm"].base_url == "https://console.local/api/3"
    duck.close()


def test_auto_register_json_config_missing_services_key_raises(tmp_path):
    config_file = tmp_path / "bad.json"
    config_file.write_text(json.dumps({"foo": "bar"}))

    duck = DuckAPI()
    with pytest.raises(ValueError, match="'services' key"):
        duck.auto_register(config_path=str(config_file))
    duck.close()


def test_auto_register_explicit_services_skips_json_lookup(tmp_path, monkeypatch):
    """Passing services= directly never touches the filesystem, even with an empty cwd."""
    monkeypatch.chdir(tmp_path)  # no duckduck.json here
    duck = DuckAPI()
    instances = duck.auto_register(_local_insightvm_config())
    assert instances["insightvm"].base_url == "https://console.local/api/3"
    duck.close()


def test_auto_register_json_file_with_aws_secret(tmp_path, monkeypatch):
    """The example.json shape: a JSON file whose service uses AWS Secrets Manager."""
    import duckduck.secrets as secrets_module

    fake_client = _fake_aws_client({"host": "x.local", "username": "u", "password": "p"})
    fake_boto3 = MagicMock()
    fake_boto3.Session.return_value.client.return_value = fake_client
    monkeypatch.setattr(secrets_module, "boto3", fake_boto3)

    config = {
        "services": {
            "insightvm": {
                "authentication": {
                    "type": "aws", "region_name": "us-east-1", "secret_id": "prod/insightvm",
                },
            },
        },
    }
    config_file = tmp_path / "duckduck.json"
    config_file.write_text(json.dumps(config))

    duck = DuckAPI()
    instances = duck.auto_register(config_path=str(config_file))

    fake_boto3.Session.return_value.client.assert_called_once_with("secretsmanager", region_name="us-east-1")
    assert instances["insightvm"].base_url == "https://x.local/api/3"
    duck.close()


# ---------------------------------------------------------------------------
# connector = "database" — generic SQL database (SQL Server, MySQL, ...)
# ---------------------------------------------------------------------------


def test_auto_register_database_connector(monkeypatch):
    import duckduck.database as database_module

    fake_sa = MagicMock()
    monkeypatch.setattr(database_module, "sa", fake_sa)

    duck = DuckAPI()
    instances = duck.auto_register({
        "sqlserver": {
            "connector": "database",
            "authentication": {
                "type": "local",
                "connection_string": (
                    "mssql+pyodbc://user:pass@host:1433/db"
                    "?driver=ODBC+Driver+17+for+SQL+Server"
                ),
            },
        },
    })

    fake_sa.create_engine.assert_called_once_with(
        "mssql+pyodbc://user:pass@host:1433/db?driver=ODBC+Driver+17+for+SQL+Server"
    )
    assert "sqlserver_table" in duck.functions
    assert "sqlserver_query" in duck.functions
    assert "sqlserver_table" in duck._streaming_functions
    assert instances["sqlserver"] is not None
    duck.close()


# ---------------------------------------------------------------------------
# connector = "servicenow"
# ---------------------------------------------------------------------------


def test_auto_register_servicenow_connector():
    duck = DuckAPI()
    instances = duck.auto_register({
        "snow": {
            "connector": "servicenow",
            "authentication": {
                "type": "local",
                "instance": "dev12345",
                "username": "admin",
                "password": "secret",
            },
        },
    })

    assert instances["snow"].base_url == "https://dev12345.service-now.com/api/now"
    assert "snow_incidents" in duck.functions
    assert "snow_problems" in duck.functions
    assert "snow_change_requests" in duck.functions
    assert "snow_users" in duck.functions
    assert "snow_cmdb_ci" in duck.functions
    assert "snow_table" in duck.functions
    assert "snow_incidents" in duck._streaming_functions
    duck.close()


# ---------------------------------------------------------------------------
# connector = "axonius"
# ---------------------------------------------------------------------------


def test_auto_register_axonius_connector():
    duck = DuckAPI()
    instances = duck.auto_register({
        "axonius": {
            "authentication": {
                "type": "local",
                "instance": "axonius.example.com",
                "api_key": "key123",
                "api_secret": "secret456",
            },
        },
    })

    assert instances["axonius"].base_url == "https://axonius.example.com/api"
    assert "axonius_devices" in duck.functions
    assert "axonius_users" in duck.functions
    assert "axonius_devices" in duck._streaming_functions
    duck.close()


# ---------------------------------------------------------------------------
# connector = "glue" — S3 + AWS Glue Data Catalog
# ---------------------------------------------------------------------------


def test_auto_register_glue_connector(monkeypatch):
    import duckduck.glue as glue_module
    import duckduck.lakehouse as lakehouse_module

    monkeypatch.setattr(lakehouse_module.duckdb, "connect", MagicMock(return_value=MagicMock()))
    fake_boto3 = MagicMock()
    monkeypatch.setattr(glue_module, "boto3", fake_boto3)

    duck = DuckAPI()
    instances = duck.auto_register({
        "s3_data": {
            "connector": "glue",
            "authentication": {
                "type": "local",
                "region_name": "us-east-1",
            },
        },
    })

    assert instances["s3_data"] is not None
    assert "s3_data_table" in duck.functions
    assert "s3_data_path" in duck.functions
    assert "s3_data_table" in duck._streaming_functions
    duck.close()


# ---------------------------------------------------------------------------
# connector = "blob_storage" — Azure Blob Storage / ADLS Gen2
# ---------------------------------------------------------------------------


def test_auto_register_blob_storage_connector(monkeypatch):
    import duckduck.lakehouse as lakehouse_module

    monkeypatch.setattr(lakehouse_module.duckdb, "connect", MagicMock(return_value=MagicMock()))

    duck = DuckAPI()
    instances = duck.auto_register({
        "adls": {
            "connector": "blob_storage",
            "authentication": {
                "type": "local",
                "account_name": "mystorageacct",
            },
        },
    })

    assert instances["adls"] is not None
    assert "adls_table" in duck.functions
    assert "adls_table" in duck._streaming_functions
    duck.close()

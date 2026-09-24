"""Unit tests for DuckAPI.auto_register (automatic wrapper instantiation)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from duckduck import DuckAPI, SecretsManager, SharePoint


def _mock_msal():
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
    return mock_app


# ---------------------------------------------------------------------------
# InsightVM — no network mock needed at construction time
# ---------------------------------------------------------------------------


def test_auto_register_offline_credentials():
    """Offline mode: credentials provided directly, no AWS Secrets Manager."""
    duck = DuckAPI()

    instances = duck.auto_register({
        "insightvm": {
            "credentials": {"host": "console.local", "username": "a", "password": "b"},
        },
    })

    assert "insightvm" in instances
    assert instances["insightvm"].base_url == "https://console.local/api/3"
    # tables prefixed by the service name
    assert "insightvm_assets" in duck.functions
    assert "insightvm_vulnerabilities" in duck.functions
    assert "insightvm_assets" in duck._streaming_functions
    duck.close()


def test_auto_register_via_secret_id():
    """secret_id fetches credentials via SecretsManager (hardcoded or runtime reference)."""
    client = MagicMock()
    client.get_secret_value.return_value = {
        "SecretString": json.dumps({"host": "x.local", "username": "u", "password": "p"})
    }
    secrets = SecretsManager(client=client)

    duck = DuckAPI()
    instances = duck.auto_register(
        {"insightvm": {"secret_id": "prod/insightvm"}},
        secrets=secrets,
    )

    client.get_secret_value.assert_called_once_with(SecretId="prod/insightvm")
    assert instances["insightvm"].base_url == "https://x.local/api/3"
    duck.close()


def test_auto_register_secret_id_without_secrets_manager_raises():
    duck = DuckAPI()
    with pytest.raises(ValueError, match="SecretsManager"):
        duck.auto_register({"insightvm": {"secret_id": "prod/insightvm"}})
    duck.close()


def test_auto_register_both_secret_id_and_credentials_raises():
    duck = DuckAPI()
    with pytest.raises(ValueError, match="secret_id.*OR.*credentials|credentials.*OR.*secret_id"):
        duck.auto_register({
            "insightvm": {
                "secret_id": "x",
                "credentials": {"host": "h", "username": "u", "password": "p"},
            },
        })
    duck.close()


def test_auto_register_neither_secret_id_nor_credentials_raises():
    duck = DuckAPI()
    with pytest.raises(ValueError, match="secret_id.*credentials|credentials.*secret_id"):
        duck.auto_register({"insightvm": {}})
    duck.close()


def test_auto_register_unknown_service_raises():
    duck = DuckAPI()
    with pytest.raises(ValueError, match="is not recognized"):
        duck.auto_register({"does_not_exist": {"credentials": {}}})
    duck.close()


def test_auto_register_multiple_instances_same_type():
    """Explicit type= allows two instances of the same wrapper with distinct prefixes."""
    duck = DuckAPI()
    instances = duck.auto_register({
        "insightvm_prod": {
            "type": "insightvm",
            "credentials": {"host": "prod.local", "username": "a", "password": "b"},
        },
        "insightvm_dev": {
            "type": "insightvm",
            "credentials": {"host": "dev.local", "username": "a", "password": "b"},
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
            "credentials": {"host": "h", "username": "u", "password": "p"},
            "default_page_size": 50,
        },
    })
    assert instances["insightvm"].default_page_size == 50
    duck.close()


# ---------------------------------------------------------------------------
# SharePoint — construction via mocked MSAL
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_auto_register_sharepoint_client_secret(msal_cls):
    msal_cls.return_value = _mock_msal()

    duck = DuckAPI()
    instances = duck.auto_register({
        "sharepoint": {
            "credentials": {
                "tenant_id": "t", "client_id": "c", "client_secret": "s",
            },
            "hostname": "company.sharepoint.com",
            "site_path": "/teams/myteam",
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
def test_auto_register_sharepoint_thumbprint(msal_cls):
    msal_cls.return_value = _mock_msal()

    duck = DuckAPI()
    instances = duck.auto_register({
        "sharepoint": {
            "credentials": {
                "tenant_id": "t", "client_id": "c",
                "thumbprint": "AABBCC",
                "private_key_pem": "-----BEGIN PRIVATE KEY-----\n...",
            },
        },
    })

    assert isinstance(instances["sharepoint"], SharePoint)
    duck.close()


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_auto_register_two_different_services(msal_cls):
    """SharePoint and InsightVM registered together don't collide (prefix by name)."""
    msal_cls.return_value = _mock_msal()

    duck = DuckAPI()
    duck.auto_register({
        "sharepoint": {"credentials": {"tenant_id": "t", "client_id": "c", "client_secret": "s"}},
        "insightvm": {"credentials": {"host": "h", "username": "u", "password": "p"}},
    })

    # both define a "sites" table — without the prefix, one would overwrite the other
    assert "sharepoint_sites" in duck.functions
    assert "insightvm_sites" in duck.functions
    duck.close()


# ---------------------------------------------------------------------------
# Loading services from a JSON file — duck.auto_register() with no arguments
# ---------------------------------------------------------------------------


def test_auto_register_no_config_found_raises_helpful_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # empty dir, no duckduck.json
    duck = DuckAPI()
    with pytest.raises(ValueError, match="found no config file"):
        duck.auto_register()
    duck.close()


def test_auto_register_loads_from_default_json_file(tmp_path, monkeypatch):
    """With no services= and no config_path=, reads ./duckduck.json."""
    monkeypatch.chdir(tmp_path)
    config = {
        "services": {
            "insightvm": {
                "credentials": {"host": "console.local", "username": "a", "password": "b"},
            },
        },
    }
    (tmp_path / "duckduck.json").write_text(json.dumps(config))

    duck = DuckAPI()
    instances = duck.auto_register()

    assert instances["insightvm"].base_url == "https://console.local/api/3"
    assert "insightvm_assets" in duck.functions
    duck.close()


def test_auto_register_config_path_argument(tmp_path):
    config = {
        "services": {
            "insightvm": {
                "credentials": {"host": "console.local", "username": "a", "password": "b"},
            },
        },
    }
    config_file = tmp_path / "custom.json"
    config_file.write_text(json.dumps(config))

    duck = DuckAPI()
    instances = duck.auto_register(config_path=str(config_file))

    assert instances["insightvm"].base_url == "https://console.local/api/3"
    duck.close()


def test_auto_register_duckduck_config_env_var(tmp_path, monkeypatch):
    config = {
        "services": {
            "insightvm": {
                "credentials": {"host": "console.local", "username": "a", "password": "b"},
            },
        },
    }
    config_file = tmp_path / "env-config.json"
    config_file.write_text(json.dumps(config))
    monkeypatch.setenv("DUCKDUCK_CONFIG", str(config_file))

    duck = DuckAPI()
    instances = duck.auto_register()

    assert instances["insightvm"].base_url == "https://console.local/api/3"
    duck.close()


def test_auto_register_json_config_missing_services_key_raises(tmp_path):
    config_file = tmp_path / "bad.json"
    config_file.write_text(json.dumps({"region_name": "us-east-1"}))

    duck = DuckAPI()
    with pytest.raises(ValueError, match="'services' key"):
        duck.auto_register(config_path=str(config_file))
    duck.close()


def test_auto_register_json_secret_id_builds_secrets_manager_automatically(tmp_path, monkeypatch):
    """
    A JSON config with secret_id + region_name but no secrets= passed in
    should build a SecretsManager on its own, so `duck.auto_register()`
    alone is enough even when AWS Secrets Manager is involved.
    """
    import duckduck.secrets as secrets_module

    fake_client = MagicMock()
    fake_client.get_secret_value.return_value = {
        "SecretString": json.dumps({"host": "x.local", "username": "u", "password": "p"})
    }
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = fake_client
    monkeypatch.setattr(secrets_module, "boto3", fake_boto3)

    config = {
        "region_name": "us-east-1",
        "services": {"insightvm": {"secret_id": "prod/insightvm"}},
    }
    config_file = tmp_path / "duckduck.json"
    config_file.write_text(json.dumps(config))

    duck = DuckAPI()
    instances = duck.auto_register(config_path=str(config_file))

    fake_boto3.client.assert_called_once_with("secretsmanager", region_name="us-east-1")
    assert instances["insightvm"].base_url == "https://x.local/api/3"
    duck.close()


def test_auto_register_explicit_services_skips_json_lookup(tmp_path, monkeypatch):
    """Passing services= directly never touches the filesystem, even with an empty cwd."""
    monkeypatch.chdir(tmp_path)  # no duckduck.json here
    duck = DuckAPI()
    instances = duck.auto_register({
        "insightvm": {"credentials": {"host": "h", "username": "u", "password": "p"}},
    })
    assert instances["insightvm"].base_url == "https://h/api/3"
    duck.close()

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

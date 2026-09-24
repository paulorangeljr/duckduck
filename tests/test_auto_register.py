"""Testes unitários para DuckAPI.auto_register (instanciação automática de wrappers)."""

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
# InsightVM — não precisa de mock de rede na construção
# ---------------------------------------------------------------------------


def test_auto_register_offline_credentials():
    """Modo offline: credenciais fornecidas diretamente, sem AWS Secrets Manager."""
    duck = DuckAPI()

    instances = duck.auto_register({
        "insightvm": {
            "credentials": {"host": "console.local", "username": "a", "password": "b"},
        },
    })

    assert "insightvm" in instances
    assert instances["insightvm"].base_url == "https://console.local/api/3"
    # tabelas prefixadas pelo nome do serviço
    assert "insightvm_assets" in duck.functions
    assert "insightvm_vulnerabilities" in duck.functions
    assert "insightvm_assets" in duck._streaming_functions
    duck.close()


def test_auto_register_via_secret_id():
    """secret_id busca credenciais via SecretsManager (referência hardcoded ou em runtime)."""
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
    with pytest.raises(ValueError, match="secret_id.*OU.*credentials|credentials.*OU.*secret_id"):
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
    with pytest.raises(ValueError, match="não é reconhecido"):
        duck.auto_register({"nao_existe": {"credentials": {}}})
    duck.close()


def test_auto_register_multiple_instances_same_type():
    """type= explícito permite duas instâncias do mesmo wrapper com prefixos distintos."""
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
# SharePoint — construção via MSAL mockado
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
            "hostname": "empresa.sharepoint.com",
            "site_path": "/teams/meutime",
        },
    })

    sp = instances["sharepoint"]
    assert isinstance(sp, SharePoint)
    assert sp._default_hostname == "empresa.sharepoint.com"
    assert sp._default_site_path == "/teams/meutime"
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
    """SharePoint e InsightVM registrados juntos não colidem (prefixo por nome)."""
    msal_cls.return_value = _mock_msal()

    duck = DuckAPI()
    duck.auto_register({
        "sharepoint": {"credentials": {"tenant_id": "t", "client_id": "c", "client_secret": "s"}},
        "insightvm": {"credentials": {"host": "h", "username": "u", "password": "p"}},
    })

    # ambos definem uma tabela "sites" — sem o prefixo, um sobrescreveria o outro
    assert "sharepoint_sites" in duck.functions
    assert "insightvm_sites" in duck.functions
    duck.close()

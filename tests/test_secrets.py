"""Testes unitários para SecretsManager (AWS Secrets Manager)."""

import json
from unittest.mock import MagicMock

import pytest

from duckduck import SecretsManager


def _fake_client(secret_dict):
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": json.dumps(secret_dict)}
    return client


def test_get_secret_parses_json():
    client = _fake_client({"host": "x", "username": "a", "password": "b"})
    sm = SecretsManager(client=client)

    secret = sm.get_secret("prod/insightvm")

    assert secret == {"host": "x", "username": "a", "password": "b"}
    client.get_secret_value.assert_called_once_with(SecretId="prod/insightvm")


def test_get_secret_is_cached():
    client = _fake_client({"host": "x", "username": "a", "password": "b"})
    sm = SecretsManager(client=client)

    sm.get_secret("prod/insightvm")
    sm.get_secret("prod/insightvm")

    client.get_secret_value.assert_called_once()  # segunda chamada usa cache


def test_get_secret_different_ids_not_cached_together():
    client = MagicMock()
    client.get_secret_value.side_effect = [
        {"SecretString": json.dumps({"a": 1})},
        {"SecretString": json.dumps({"b": 2})},
    ]
    sm = SecretsManager(client=client)

    assert sm.get_secret("secret-a") == {"a": 1}
    assert sm.get_secret("secret-b") == {"b": 2}
    assert client.get_secret_value.call_count == 2


def test_get_secret_binary_raises():
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretBinary": b"raw-bytes"}
    sm = SecretsManager(client=client)

    with pytest.raises(ValueError, match="SecretString"):
        sm.get_secret("binary-secret")


def test_without_client_or_boto3_raises_import_error(monkeypatch):
    import duckduck.secrets as secrets_module

    monkeypatch.setattr(secrets_module, "boto3", None)
    with pytest.raises(ImportError, match="boto3"):
        SecretsManager()

"""Unit tests for SecretsManager (AWS Secrets Manager)."""

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

    client.get_secret_value.assert_called_once()  # second call uses the cache


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


# ---------------------------------------------------------------------------
# profile_name — selecting a specific AWS account
# ---------------------------------------------------------------------------


def test_profile_name_builds_session_with_profile(monkeypatch):
    import duckduck.secrets as secrets_module

    fake_boto3 = MagicMock()
    monkeypatch.setattr(secrets_module, "boto3", fake_boto3)

    SecretsManager(region_name="us-east-1", profile_name="prod")

    fake_boto3.Session.assert_called_once_with(profile_name="prod")
    fake_boto3.Session.return_value.client.assert_called_once_with(
        "secretsmanager", region_name="us-east-1"
    )


def test_no_profile_name_uses_default_session(monkeypatch):
    import duckduck.secrets as secrets_module

    fake_boto3 = MagicMock()
    monkeypatch.setattr(secrets_module, "boto3", fake_boto3)

    SecretsManager(region_name="us-east-1")

    fake_boto3.Session.assert_called_once_with(profile_name=None)

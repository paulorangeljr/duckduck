"""Unit tests for the BlobStorage connector (Azure Blob Storage / ADLS Gen2)."""

from unittest.mock import MagicMock

import pandas as pd
import pytest

import duckduck.lakehouse as lakehouse_module
from duckduck import BlobStorage


def _make_blob(monkeypatch, **kwargs):
    fake_duck_conn = MagicMock()
    monkeypatch.setattr(lakehouse_module.duckdb, "connect", MagicMock(return_value=fake_duck_conn))
    bs = BlobStorage(**kwargs)
    return bs, fake_duck_conn


# ---------------------------------------------------------------------------
# Construction / secret setup
# ---------------------------------------------------------------------------


def test_missing_account_name_and_connection_string_raises(monkeypatch):
    monkeypatch.setattr(lakehouse_module.duckdb, "connect", MagicMock())
    with pytest.raises(ValueError, match="connection_string or account_name"):
        BlobStorage()


def test_connection_string_secret(monkeypatch):
    _, fake_duck_conn = _make_blob(monkeypatch, connection_string="DefaultEndpointsProtocol=https;...")

    executed = [call.args[0] for call in fake_duck_conn.execute.call_args_list]
    secret_calls = [sql for sql in executed if "CREATE OR REPLACE SECRET" in sql]
    assert len(secret_calls) == 1
    assert "CONNECTION_STRING 'DefaultEndpointsProtocol=https;...'" in secret_calls[0]


def test_account_name_credential_chain_secret(monkeypatch):
    _, fake_duck_conn = _make_blob(monkeypatch, account_name="mystorageacct")

    executed = [call.args[0] for call in fake_duck_conn.execute.call_args_list]
    secret_calls = [sql for sql in executed if "CREATE OR REPLACE SECRET" in sql]
    assert len(secret_calls) == 1
    assert "PROVIDER CREDENTIAL_CHAIN" in secret_calls[0]
    assert "ACCOUNT_NAME 'mystorageacct'" in secret_calls[0]


def test_connection_string_takes_precedence_over_account_name(monkeypatch):
    _, fake_duck_conn = _make_blob(
        monkeypatch, account_name="mystorageacct", connection_string="conn-str"
    )

    executed = [call.args[0] for call in fake_duck_conn.execute.call_args_list]
    secret_calls = [sql for sql in executed if "CREATE OR REPLACE SECRET" in sql]
    assert len(secret_calls) == 1
    assert "CONNECTION_STRING" in secret_calls[0]
    assert "ACCOUNT_NAME" not in secret_calls[0]


def test_ensures_azure_extension(monkeypatch):
    _, fake_duck_conn = _make_blob(monkeypatch, account_name="mystorageacct")

    fake_duck_conn.execute.assert_any_call("INSTALL azure")
    fake_duck_conn.execute.assert_any_call("LOAD azure")


def test_from_secret_builds_instance(monkeypatch):
    fake_duck_conn = MagicMock()
    monkeypatch.setattr(lakehouse_module.duckdb, "connect", MagicMock(return_value=fake_duck_conn))

    bs = BlobStorage.from_secret({"account_name": "mystorageacct"})
    assert bs is not None


# ---------------------------------------------------------------------------
# _scan_expression / table()
# ---------------------------------------------------------------------------


def test_scan_expression_parquet(monkeypatch):
    bs, _ = _make_blob(monkeypatch, account_name="acct")
    assert bs._scan_expression("data", "events/*.parquet", "parquet") == (
        "read_parquet('az://data/events/*.parquet')"
    )


def test_scan_expression_csv(monkeypatch):
    bs, _ = _make_blob(monkeypatch, account_name="acct")
    assert bs._scan_expression("data", "file.csv", "csv") == "read_csv('az://data/file.csv')"


def test_scan_expression_json(monkeypatch):
    bs, _ = _make_blob(monkeypatch, account_name="acct")
    assert bs._scan_expression("data", "file.json", "json") == "read_json('az://data/file.json')"


def test_scan_expression_delta_loads_extension(monkeypatch):
    bs, fake_duck_conn = _make_blob(monkeypatch, account_name="acct")
    expr = bs._scan_expression("data", "delta_table", "delta")
    assert expr == "delta_scan('az://data/delta_table')"
    fake_duck_conn.execute.assert_any_call("INSTALL delta")


def test_scan_expression_iceberg_loads_extension(monkeypatch):
    bs, fake_duck_conn = _make_blob(monkeypatch, account_name="acct")
    expr = bs._scan_expression("data", "iceberg_table", "iceberg")
    assert expr == "iceberg_scan('az://data/iceberg_table')"
    fake_duck_conn.execute.assert_any_call("INSTALL iceberg")


def test_scan_expression_strips_leading_slash(monkeypatch):
    bs, _ = _make_blob(monkeypatch, account_name="acct")
    assert bs._scan_expression("data", "/events/*.parquet", "parquet") == (
        "read_parquet('az://data/events/*.parquet')"
    )


def test_scan_expression_invalid_format_raises(monkeypatch):
    bs, _ = _make_blob(monkeypatch, account_name="acct")
    with pytest.raises(ValueError, match="Unsupported format"):
        bs._scan_expression("data", "file.xyz", "xyz")


def test_table_defaults_to_parquet(monkeypatch):
    bs, fake_duck_conn = _make_blob(monkeypatch, account_name="acct")
    fake_duck_conn.sql.return_value.df.return_value = pd.DataFrame([{"id": 1}])

    df = bs.table("data", "events/*.parquet")

    fake_duck_conn.sql.assert_called_once_with(
        "SELECT * FROM read_parquet('az://data/events/*.parquet')"
    )
    assert list(df["id"]) == [1]


def test_table_pushes_down_limit(monkeypatch):
    bs, fake_duck_conn = _make_blob(monkeypatch, account_name="acct")
    fake_duck_conn.sql.return_value.df.return_value = pd.DataFrame([{"id": 1}])

    bs.table("data", "events/*.parquet", limit=10)

    fake_duck_conn.sql.assert_called_once_with(
        "SELECT * FROM read_parquet('az://data/events/*.parquet') LIMIT 10"
    )


# ---------------------------------------------------------------------------
# Streaming (iter_table)
# ---------------------------------------------------------------------------


def test_iter_table_chunks_result(monkeypatch):
    bs, fake_duck_conn = _make_blob(monkeypatch, account_name="acct")
    fake_duck_conn.sql.return_value.df.return_value = pd.DataFrame([{"id": i} for i in range(5)])

    chunks = list(bs.iter_table("data", "events/*.parquet", chunksize=2))

    assert [len(c) for c in chunks] == [2, 2, 1]

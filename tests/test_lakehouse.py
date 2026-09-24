"""Unit tests for LakehouseConnection (shared DuckDB object-storage scan helper)."""

from unittest.mock import MagicMock, patch

import pandas as pd

import duckduck.lakehouse as lakehouse_module
from duckduck.lakehouse import LakehouseConnection


def _make_lake(monkeypatch):
    fake_conn = MagicMock()
    monkeypatch.setattr(lakehouse_module.duckdb, "connect", MagicMock(return_value=fake_conn))
    return LakehouseConnection(), fake_conn


def test_ensure_extension_installs_and_loads(monkeypatch):
    lake, fake_conn = _make_lake(monkeypatch)

    lake.ensure_extension("httpfs")

    fake_conn.execute.assert_any_call("INSTALL httpfs")
    fake_conn.execute.assert_any_call("LOAD httpfs")


def test_ensure_extension_is_idempotent(monkeypatch):
    lake, fake_conn = _make_lake(monkeypatch)

    lake.ensure_extension("httpfs")
    lake.ensure_extension("httpfs")

    assert fake_conn.execute.call_count == 2  # INSTALL + LOAD, only once total


def test_ensure_extension_different_names_both_load(monkeypatch):
    lake, fake_conn = _make_lake(monkeypatch)

    lake.ensure_extension("httpfs")
    lake.ensure_extension("delta")

    assert fake_conn.execute.call_count == 4


def test_create_secret_ensures_httpfs_then_executes(monkeypatch):
    lake, fake_conn = _make_lake(monkeypatch)

    lake.create_secret("CREATE SECRET (TYPE s3)")

    fake_conn.execute.assert_any_call("INSTALL httpfs")
    fake_conn.execute.assert_any_call("LOAD httpfs")
    fake_conn.execute.assert_any_call("CREATE SECRET (TYPE s3)")


def test_scan_without_limit(monkeypatch):
    lake, fake_conn = _make_lake(monkeypatch)
    fake_conn.sql.return_value.df.return_value = pd.DataFrame([{"id": 1}])

    df = lake.scan("read_parquet('s3://bucket/*.parquet')")

    fake_conn.sql.assert_called_once_with("SELECT * FROM read_parquet('s3://bucket/*.parquet')")
    assert list(df["id"]) == [1]


def test_scan_with_limit(monkeypatch):
    lake, fake_conn = _make_lake(monkeypatch)
    fake_conn.sql.return_value.df.return_value = pd.DataFrame([{"id": 1}])

    lake.scan("read_parquet('s3://bucket/*.parquet')", limit=10)

    fake_conn.sql.assert_called_once_with(
        "SELECT * FROM read_parquet('s3://bucket/*.parquet') LIMIT 10"
    )

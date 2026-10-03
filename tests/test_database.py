"""Unit tests for the generic SQLDatabase wrapper (SQL Server, MySQL, etc.)."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import duckduck.connectors.databases.sql as database_module
from duckduck import SQLDatabase


def _make_db(monkeypatch):
    fake_sa = MagicMock()
    fake_engine = MagicMock()
    fake_sa.create_engine.return_value = fake_engine
    fake_sa.MetaData.return_value = MagicMock()
    monkeypatch.setattr(database_module, "sa", fake_sa)
    db = SQLDatabase("sqlite:///:memory:")
    return db, fake_sa, fake_engine


def test_without_sqlalchemy_raises_import_error(monkeypatch):
    monkeypatch.setattr(database_module, "sa", None)
    with pytest.raises(ImportError, match="sqlalchemy"):
        SQLDatabase("sqlite:///:memory:")


# ---------------------------------------------------------------------------
# table()
# ---------------------------------------------------------------------------


def test_table_reflects_schema_qualified_name(monkeypatch):
    db, fake_sa, fake_engine = _make_db(monkeypatch)
    fake_table = MagicMock()
    fake_sa.Table.return_value = fake_table
    fake_sa.select.return_value = MagicMock()

    with patch.object(database_module.pd, "read_sql", return_value=pd.DataFrame([{"id": 1}])):
        db.table("dbo.Customers")

    fake_sa.Table.assert_called_once_with(
        "Customers", db._metadata, schema="dbo", autoload_with=fake_engine
    )


def test_table_without_schema(monkeypatch):
    db, fake_sa, fake_engine = _make_db(monkeypatch)
    fake_sa.select.return_value = MagicMock()

    with patch.object(database_module.pd, "read_sql", return_value=pd.DataFrame([{"id": 1}])):
        db.table("customers")

    fake_sa.Table.assert_called_once_with(
        "customers", db._metadata, schema=None, autoload_with=fake_engine
    )


def test_table_pushes_down_limit(monkeypatch):
    db, fake_sa, fake_engine = _make_db(monkeypatch)
    fake_stmt = MagicMock()
    fake_sa.select.return_value = fake_stmt
    fake_stmt.limit.return_value = fake_stmt

    with patch.object(database_module.pd, "read_sql", return_value=pd.DataFrame([{"id": 1}])):
        db.table("customers", limit=10)

    fake_stmt.limit.assert_called_once_with(10)


def test_table_without_limit_does_not_call_limit(monkeypatch):
    db, fake_sa, fake_engine = _make_db(monkeypatch)
    fake_stmt = MagicMock()
    fake_sa.select.return_value = fake_stmt

    with patch.object(database_module.pd, "read_sql", return_value=pd.DataFrame([{"id": 1}])):
        db.table("customers")

    fake_stmt.limit.assert_not_called()


def test_table_reflection_is_cached(monkeypatch):
    db, fake_sa, fake_engine = _make_db(monkeypatch)
    fake_sa.select.return_value = MagicMock()

    with patch.object(database_module.pd, "read_sql", return_value=pd.DataFrame([{"id": 1}])):
        db.table("customers")
        db.table("customers")

    fake_sa.Table.assert_called_once()  # second call uses the cache


def test_table_returns_dataframe(monkeypatch):
    db, fake_sa, fake_engine = _make_db(monkeypatch)
    fake_sa.select.return_value = MagicMock()
    expected_df = pd.DataFrame([{"id": 1, "name": "Alice"}])

    with patch.object(database_module.pd, "read_sql", return_value=expected_df):
        df = db.table("customers")

    assert list(df["name"]) == ["Alice"]


# ---------------------------------------------------------------------------
# query()
# ---------------------------------------------------------------------------


def test_query_runs_raw_sql(monkeypatch):
    db, fake_sa, fake_engine = _make_db(monkeypatch)
    expected_df = pd.DataFrame([{"id": 1}])

    with patch.object(database_module.pd, "read_sql_query", return_value=expected_df) as mock_read:
        df = db.query("SELECT * FROM orders")

    mock_read.assert_called_once_with("SELECT * FROM orders", fake_engine)
    assert list(df["id"]) == [1]


def test_query_applies_limit_client_side(monkeypatch):
    db, fake_sa, fake_engine = _make_db(monkeypatch)
    big_df = pd.DataFrame([{"id": i} for i in range(5)])

    with patch.object(database_module.pd, "read_sql_query", return_value=big_df):
        df = db.query("SELECT * FROM orders", limit=2)

    assert len(df) == 2


# ---------------------------------------------------------------------------
# from_secret
# ---------------------------------------------------------------------------


def test_from_secret_with_connection_string(monkeypatch):
    fake_sa = MagicMock()
    monkeypatch.setattr(database_module, "sa", fake_sa)

    SQLDatabase.from_secret({"connection_string": "sqlite:///:memory:"})

    fake_sa.create_engine.assert_called_once_with("sqlite:///:memory:")


def test_from_secret_connection_string_override_wins(monkeypatch):
    fake_sa = MagicMock()
    monkeypatch.setattr(database_module, "sa", fake_sa)

    SQLDatabase.from_secret(
        {"connection_string": "sqlite:///from-secret.db"},
        connection_string="sqlite:///override.db",
    )

    fake_sa.create_engine.assert_called_once_with("sqlite:///override.db")


def test_from_secret_with_discrete_fields(monkeypatch):
    fake_sa = MagicMock()
    fake_url = MagicMock()
    fake_sa.engine.URL.create.return_value = fake_url
    monkeypatch.setattr(database_module, "sa", fake_sa)

    SQLDatabase.from_secret({
        "drivername": "mssql+pyodbc",
        "username": "u", "password": "p", "host": "h", "port": 1433, "database": "db",
        "query": {"driver": "ODBC Driver 17 for SQL Server"},
    })

    fake_sa.engine.URL.create.assert_called_once_with(
        drivername="mssql+pyodbc", username="u", password="p", host="h",
        port=1433, database="db", query={"driver": "ODBC Driver 17 for SQL Server"},
    )
    fake_sa.create_engine.assert_called_once_with(fake_url)


def test_from_secret_missing_drivername_raises(monkeypatch):
    fake_sa = MagicMock()
    monkeypatch.setattr(database_module, "sa", fake_sa)

    with pytest.raises(ValueError, match="drivername"):
        SQLDatabase.from_secret({"username": "u"})


# ---------------------------------------------------------------------------
# Streaming (iter_*) — for use with DuckAPI.stream()
# ---------------------------------------------------------------------------


def _sqlite(tmp_path, rows=5):
    import sqlite3

    path = tmp_path / "t.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE customers (id INTEGER, name TEXT)")
    c.executemany("INSERT INTO customers VALUES (?, ?)", [(i, f"c{i}") for i in range(rows)])
    c.commit()
    return SQLDatabase(f"sqlite:///{path}")


def test_iter_table_yields_chunks_from_a_server_side_cursor(tmp_path):
    db = _sqlite(tmp_path)
    result = list(db.iter_table("customers", chunksize=2))
    assert [len(r) for r in result] == [2, 2, 1]
    assert list(pd.concat(result)["id"]) == [0, 1, 2, 3, 4]


def test_iter_table_of_nothing_yields_the_columns(tmp_path):
    from duckduck.common.pushdown import Condition

    db = _sqlite(tmp_path)
    (only,) = list(db.iter_table("customers", where=[Condition("id", "gt", 99)]))
    assert only.empty and list(only.columns) == ["id", "name"]


def test_iter_query_skips_empty_chunks(tmp_path):
    db = _sqlite(tmp_path)
    assert [len(r) for r in db.iter_query("SELECT * FROM customers", chunksize=3)] == [3, 2]
    assert list(db.iter_query("SELECT * FROM customers WHERE id < 0")) == []

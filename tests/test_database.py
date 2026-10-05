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


# --- Oracle (checked against Oracle Free 23ai with python-oracledb; these need neither) ---------------


def test_date_values_are_bound_as_dates_not_text():
    """``WHERE opened_at >= '2026-10-01'`` (or DuckDB's TIMESTAMP '…', which arrives as text): Oracle reads a text
    bound to a DATE/TIMESTAMP with NLS_DATE_FORMAT and fails (ORA-01843) — the value goes as a datetime."""
    import datetime as dt

    import sqlalchemy as sa

    from duckduck.common.pushdown import Condition

    tbl = sa.Table("t", sa.MetaData(), sa.Column("opened", sa.DateTime), sa.Column("day", sa.Date),
                   sa.Column("at", sa.Time), sa.Column("name", sa.String), sa.Column("n", sa.Integer))
    db = SQLDatabase("sqlite:///:memory:")
    where = [Condition("opened", "gte", "2026-10-01"), Condition("opened", "lt", "2026-10-02T08:30:00"),
             Condition("day", "eq", "2026-10-01"), Condition("day", "lt", "2026-10-01 10:00:00"),
             Condition("at", "gt", "08:00"), Condition("name", "eq", "2026-10-01"), Condition("n", "eq", "7"),
             Condition("opened", "in", ("2026-10-01", "2026-10-03")),
             Condition("opened", "gt", "2026-10-01T00:00:00+02:00"), Condition("opened", "gt", "yesterday")]
    clauses = db._where_clauses(tbl, where)
    values = [c.right.value for c in clauses[:7]]
    assert values == [dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 2, 8, 30), dt.date(2026, 10, 1),
                      dt.datetime(2026, 10, 1, 10, 0),  # a moment on a DATE column stays a moment: no bound moved
                      dt.time(8, 0), "2026-10-01", "7"]  # text and numbers as they were
    assert clauses[7].right.value == [dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 3)]
    assert [c.right.value for c in clauses[8:]] == ["2026-10-01T00:00:00+02:00", "yesterday"]  # left alone


def test_oracle_jdbc_url_follows_the_sqlalchemy_url():
    import sqlalchemy as sa

    url = sa.engine.make_url
    assert SQLDatabase._oracle_jdbc(url("oracle+oracledb://u:p@db:1521/?service_name=ORCLPDB1")).format(
        host="db", port=1521, db="") == "jdbc:oracle:thin:@//db:1521/ORCLPDB1"
    assert SQLDatabase._oracle_jdbc(url("oracle+oracledb://u:p@db:1521/ORCL")).format(
        host="db", port=1521, db="ORCL") == "jdbc:oracle:thin:@db:1521:ORCL"  # SQLAlchemy reads it as a SID
    assert SQLDatabase._oracle_jdbc(url("oracle+oracledb://u:p@prod_tns")).format(
        host="prod_tns", port=1521, db="") == "jdbc:oracle:thin:@prod_tns"


def test_oracle_tables_leave_out_the_schemas_oracle_maintains(monkeypatch):
    """Oracle lists every user as a schema (SYS, XDB, MDSYS… — thousands of views): ``tables`` keeps the ones
    ``ALL_USERS.ORACLE_MAINTAINED`` doesn't mark."""
    db = SQLDatabase("sqlite:///:memory:")
    monkeypatch.setattr(db.engine.dialect, "name", "oracle")
    inspector = MagicMock()
    inspector.get_schema_names.return_value = ["app", "hr", "sys", "xdb", "mdsys"]
    inspector.dialect.normalize_name = str.lower
    conn = MagicMock()
    conn.__enter__.return_value.execute.return_value = [("SYS",), ("XDB",), ("MDSYS",)]
    monkeypatch.setattr(type(db.engine), "connect", lambda self: conn)
    assert db._user_schemas(inspector) == ["app", "hr"]
    conn.__enter__.return_value.execute.side_effect = RuntimeError("ORA-00904: invalid identifier")  # before 12c
    assert db._user_schemas(inspector) == ["app", "hr", "xdb", "mdsys"]  # every schema but the generic system ones


def test_a_connection_string_takes_its_credentials_from_the_secret():
    """One form for every engine: the URL in duckduck.json with ``${key}``, the values in the secret — escaped,
    so a password with ``@ / : %`` stays one password (checked against Oracle with ``p@ss/w:rd%9``)."""
    import sqlalchemy as sa

    url = "postgresql+psycopg2://${username}:${password}@db:5432/${dbname}"
    secret = {"username": "app", "password": "p@ss/w:rd%9", "dbname": "sales"}
    filled = SQLDatabase._filled(url, secret)
    parsed = sa.engine.make_url(filled)
    assert (parsed.username, parsed.password, parsed.host, parsed.database) == ("app", "p@ss/w:rd%9", "db", "sales")
    with pytest.raises(ValueError, match=r"no 'pasword' \(it has: dbname, password, username\)"):
        SQLDatabase._filled("x://${username}:${pasword}@h", secret)
    assert SQLDatabase._filled("sqlite:///a.db", {}) == "sqlite:///a.db"


def test_auto_register_fills_the_connection_string_from_the_authentication_block(tmp_path):
    from duckduck import DuckAPI

    db_file = tmp_path / "x.db"
    duck = DuckAPI()
    duck.auto_register({"local_db": {"connector": "database", "connection_string": "sqlite:///${path}",
                                     "authentication": {"type": "local", "path": str(db_file)}}})
    assert duck.sql("SELECT * FROM local_db_query(sql='SELECT 1 AS ok')").fetchall() == [(1,)]


def test_a_connection_string_of_placeholders_isnt_masked_but_one_with_a_password_is():
    from duckduck.system import mask

    shown = "oracle+oracledb://${username}:${password}@db:1521/?service_name=X"
    assert mask({"connection_string": shown}) == {"connection_string": shown}
    for literal in ("oracle+oracledb://app:hunter2@db/?service_name=${svc}",
                    "mssql+pyodbc://${u}:${p}@h/db?pwd=hunter2", "sqlite:///a.db"):
        assert mask({"connection_string": literal}) == {"connection_string": "***"}

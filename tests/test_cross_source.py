"""Joining two *different* real connectors in one query — a SQL database (SQLAlchemy over SQLite) and Athena
(its boto3 client faked, as test_athena does). Each has its own push-down (SQL dialect vs Athena SQL, both
translate a join's keys to a real ``IN``), so this guards that the rewrite and the join push-down work when
the sources don't share a mechanism. Needs sqlalchemy (the `[database]` extra); skipped without it.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
sys.path.insert(0, str(Path(__file__).parent))

from duckduck import DuckAPI  # noqa: E402
from test_athena import FakeAthena  # noqa: E402  (the suite's documented-shape boto3 fake)


def _cross(tmp_path):
    """A DuckAPI with `sqldb` (a real SQLite customers table) and `athena` (fake client). Athena's events have
    host 'a' (n=1) and 'b' (n=2); customers are a/gold, b/silver, c/gold — so a key join matches a and b."""
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE customers (name TEXT, tier TEXT)")
    con.executemany("INSERT INTO customers VALUES (?, ?)", [("a", "gold"), ("b", "silver"), ("c", "gold")])
    con.commit()
    con.close()
    fake = FakeAthena(rows=[("a", "1", "[x]", "2026-09-30", "s"), ("b", "2", "[y]", "2026-09-30", "t")])
    duck = DuckAPI()
    duck.auto_register({  # both brought up the real way, so address resolution and push-down are the real paths
        "sqldb": {"connector": "database", "connection_string": f"sqlite:///{path}",
                  "authentication": {"type": "local"}},
        "athena": {"connector": "athena", "client": fake, "poll_interval": 0.001, "results": "api"},
    })
    return duck, fake


def test_sql_database_joined_to_athena_pushes_the_keys_to_athena(tmp_path):
    duck, fake = _cross(tmp_path)
    df = duck.sql("SELECT c.name, c.tier, e.n FROM sqldb.customers c "
                  "JOIN athena.logs.events e ON c.name = e.host").df()
    assert len(df) == 2 and set(df["name"]) == {"a", "b"}
    # the customers' keys reached Athena as a real IN — not a full-table read
    sql = fake.started[-1]["QueryString"]
    assert '"host" IN (' in sql and "'a'" in sql and "'b'" in sql


def test_a_filter_on_the_sql_side_narrows_what_athena_reads(tmp_path):
    duck, fake = _cross(tmp_path)
    df = duck.sql("SELECT c.name, e.n FROM sqldb.customers c JOIN athena.logs.events e ON c.name = e.host "
                  "WHERE c.tier = 'gold'").df()
    assert list(df["name"]) == ["a"]  # only gold a/c reach the join; only a has an event
    sql = fake.started[-1]["QueryString"]
    assert "'a'" in sql and "'c'" in sql and "'b'" not in sql  # silver b never sent to Athena


def test_a_filter_on_the_athena_side_reaches_athena(tmp_path):
    duck, fake = _cross(tmp_path)
    df = duck.sql("SELECT c.name, e.n FROM sqldb.customers c JOIN athena.logs.events e ON c.name = e.host "
                  "WHERE e.n > 1").df()
    assert list(df["name"]) == ["b"]
    assert '"n" > 1' in fake.started[-1]["QueryString"]


def test_an_implicit_comma_join_across_two_addressed_sources(tmp_path):
    duck, fake = _cross(tmp_path)
    df = duck.sql("SELECT c.name, e.n FROM sqldb.customers c, athena.logs.events e WHERE c.name = e.host").df()
    assert len(df) == 2 and set(df["name"]) == {"a", "b"}  # used to fail: 'Catalog athena does not exist'
    # the comma join's key equality is in the WHERE — its keys must still reach Athena, not read it whole
    assert '"host" IN (' in fake.started[-1]["QueryString"]


def test_a_cte_over_one_source_joined_to_the_other(tmp_path):
    duck, fake = _cross(tmp_path)
    df = duck.sql("WITH gold AS (SELECT * FROM sqldb.customers WHERE tier = 'gold') "
                  "SELECT g.name, e.n FROM gold g JOIN athena.logs.events e ON g.name = e.host").df()
    assert list(df["name"]) == ["a"]  # gold a/c, only a has an event
    # the join key is traced through the CTE to the source: Athena is read for the gold names only, not whole
    sql = fake.started[-1]["QueryString"]
    assert '"host" IN (' in sql and "'a'" in sql and "'c'" in sql and "'b'" not in sql  # silver b never sent


def test_a_subquery_in_from_over_one_source_joined_to_the_other(tmp_path):
    duck, fake = _cross(tmp_path)
    df = duck.sql("SELECT g.name, e.n FROM (SELECT * FROM sqldb.customers WHERE tier = 'gold') g "
                  "JOIN athena.logs.events e ON g.name = e.host").df()
    assert list(df["name"]) == ["a"]
    sql = fake.started[-1]["QueryString"]
    assert '"host" IN (' in sql and "'a'" in sql and "'c'" in sql and "'b'" not in sql

"""Push-down with subqueries, CTEs and joins of derived tables: each WHERE reaches only the table it filters.

Before, the first WHERE found anywhere in the query was handed to every function: a filter
on one side of a join reached the other side, and a filter on a computed or aggregated
column (``Bytes * 2 AS Bytes``, ``COUNT(*) AS n``) reached the source as if it were its own
column — both can lose rows."""

from typing import Optional

import pandas as pd

from duckduck import DuckAPI


def _duck():
    calls = []

    def proxy(where=None, limit=None):
        calls.append(("proxy", sorted((c.column, c.op, c.value) for c in where or []), limit))
        return pd.DataFrame({"Host": ["a", "b", "c"], "Bytes": [5, 8, 20]})

    def inc(host: Optional[str] = None, limit=None):
        calls.append(("inc", host, limit))
        return pd.DataFrame({"host": ["a", "c"], "number": ["INC1", "INC2"]})

    duck = DuckAPI()
    duck.register_api_function("proxy", proxy)
    duck.register_api_function("inc", inc)
    return duck, calls


def test_one_select_is_unchanged():
    duck, calls = _duck()
    duck.sql("SELECT * FROM proxy WHERE Host = 'a' LIMIT 5").df()
    assert calls == [("proxy", [("host", "eq", "a")], 5)]


def test_a_computed_column_stays_with_duckdb():
    duck, calls = _duck()
    out = duck.sql("SELECT * FROM (SELECT Host, Bytes * 2 AS Bytes FROM proxy) WHERE Bytes > 10").df()
    assert calls == [("proxy", [], None)]
    assert sorted(out["Host"]) == ["b", "c"]  # b: 8 * 2 = 16 — a source filtering Bytes > 10 would lose it


def test_an_aggregate_stays_with_duckdb_and_the_inner_where_is_pushed():
    duck, calls = _duck()
    duck.sql("SELECT * FROM (SELECT Host, COUNT(*) AS n FROM (SELECT * FROM proxy WHERE Bytes > 6) GROUP BY ALL) WHERE n > 0").df()
    assert calls == [("proxy", [("bytes", "gt", 6)], None)]


def test_each_side_of_a_join_gets_its_own_filter():
    duck, calls = _duck()
    out = duck.sql("SELECT * FROM (SELECT * FROM proxy WHERE Host = 'a') AS L "
                   "JOIN (SELECT host, number FROM inc) AS R ON L.Host = R.host").df()
    assert ("proxy", [("host", "eq", "a")], None) in calls and ("inc", None, None) in calls
    assert out["number"].tolist() == ["INC1"]


def test_the_same_table_twice_gets_its_filter_by_alias_or_none():
    duck, calls = _duck()
    duck.sql("SELECT * FROM (SELECT * FROM proxy AS p1 WHERE Host = 'a') AS L "
             "JOIN (SELECT * FROM proxy AS p2 WHERE Host = 'c') AS R ON L.Bytes < R.Bytes").df()
    assert sorted(calls) == [("proxy", [("host", "eq", "a")], None), ("proxy", [("host", "eq", "c")], None)]
    calls.clear()  # without aliases nothing tells them apart: DuckDB filters both
    duck.sql("SELECT * FROM (SELECT * FROM proxy WHERE Host = 'a') AS L "
             "JOIN (SELECT * FROM proxy WHERE Host = 'c') AS R ON L.Bytes < R.Bytes").df()
    assert calls == [("proxy", [], None), ("proxy", [], None)]


def test_a_where_on_a_select_star_passes_through():
    duck, calls = _duck()
    duck.sql("SELECT * FROM (SELECT * FROM proxy WHERE Bytes > 1) WHERE Host = 'c'").df()
    assert calls == [("proxy", [("bytes", "gt", 1), ("host", "eq", "c")], None)]


def test_a_cte_is_scoped_too():
    duck, calls = _duck()
    out = duck.sql("WITH x AS (SELECT * FROM proxy WHERE Host = 'b') SELECT * FROM x JOIN inc AS i ON x.Host = i.host").df()
    assert calls[0] == ("proxy", [("host", "eq", "b")], None) and ("inc", None, None) in calls and out.empty

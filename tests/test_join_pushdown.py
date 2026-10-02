"""A JOIN between two sources: one side is read first and its key values narrow the other side's read
(``DuckAPI._join_plan`` / ``_join_conditions`` / ``_plan_calls``) — and a query DuckDB can't parse fails before
any source is read."""

import duckdb
import pandas as pd
import pytest

from duckduck import DuckAPI


class Tickets:
    """A ServiceNow-like table function: its where applies IN, a few values per request."""

    WHERE_OPS = frozenset({"eq", "like", "ilike", "gt", "gte", "lt", "lte", "in"})
    IN_MAX = 2

    def __init__(self):
        self.calls = []

    def table(self, table_name: str, where=None, limit=None):
        self.calls.append({"table_name": table_name, "where": list(where or [])})
        rows = pd.DataFrame({"sys_id": ["a", "b", "c", "d", "e"], "number": ["I1", "I2", "I3", "I4", "I5"]})
        for c in where or []:
            if c.op == "in":
                rows = rows[rows["sys_id"].isin(c.value)]
        return rows


def _duck(**kwargs):
    duck, tickets, reads = DuckAPI(**kwargs), Tickets(), []

    def s3(where=None, limit=None):
        reads.append(where)
        df = pd.DataFrame({"name": ["x", "x", "x", "y"], "sn_id": ["a", "c", None, "d"]})
        for c in where or []:
            if c.op == "eq":
                df = df[df[c.column] == c.value]
        return df

    duck.register_api_function("sn_table", tickets.table)
    duck.register_api_function("s3_files", s3)
    return duck, tickets, reads


def _ins(call):
    return [c for c in call["where"] if c.op == "in"]


def test_the_left_side_s_key_values_narrow_the_right_side():
    duck, tickets, _ = _duck()
    rows = duck.sql("SELECT s3.name, s3.sn_id, sn.number FROM s3_files s3 LEFT JOIN sn_table sn "
                    "ON sn.sys_id = s3.sn_id AND sn.table_name = 'incident' WHERE s3.name = 'x' "
                    "ORDER BY 2 NULLS LAST").fetchall()
    assert rows == [("x", "a", "I1"), ("x", "c", "I3"), ("x", None, None)]
    [call] = tickets.calls
    assert call["table_name"] == "incident"  # an ON constant on the joined table filled its argument
    assert [sorted(c.value) for c in _ins(call)] == [["a", "c"]]  # only x's keys (not y's d, not NULL)


def test_a_join_without_on_fails_before_any_source_is_read():
    duck, tickets, reads = _duck()
    with pytest.raises(duckdb.ParserException):
        duck.sql("SELECT * FROM s3_files s3 LEFT JOIN sn_table sn WHERE s3.name = 'x'")
    assert reads == [] and tickets.calls == []
    # inline arguments aren't SQL the parser takes, but they're ours: still fine
    assert duck.sql("SELECT count(*) FROM sn_table(table_name='incident')").fetchone() == (5,)


def test_nothing_on_the_left_means_the_right_side_isn_t_read():
    duck, tickets, _ = _duck()
    rows = duck.sql("SELECT sn.number FROM s3_files s3 JOIN sn_table sn ON sn.sys_id = s3.sn_id "
                    "WHERE s3.name = 'nobody' AND sn.table_name = 'incident'").fetchall()
    assert rows == [] and tickets.calls == []


def test_more_values_than_one_request_takes_are_split_into_calls():
    duck, tickets, _ = _duck()
    rows = duck.sql("SELECT sn.number FROM s3_files s3 JOIN sn_table sn ON sn.sys_id = s3.sn_id "
                    "WHERE sn.table_name = 'incident' ORDER BY 1").fetchall()
    assert rows == [("I1",), ("I3",), ("I4",)]
    assert sorted(len(_ins(c)[0].value) for c in tickets.calls) == [1, 2]  # 3 values, IN_MAX 2
    duck2, tickets2, _ = _duck(join_calls_max=1)
    duck2.sql("SELECT sn.number FROM s3_files s3 JOIN sn_table sn ON sn.sys_id = s3.sn_id "
              "WHERE sn.table_name = 'incident'").fetchall()
    assert len(tickets2.calls) == 1 and _ins(tickets2.calls[0]) == []  # too many calls: read whole


def test_too_many_values_aren_t_sent():
    duck, tickets, _ = _duck(join_values_max=2)
    assert len(duck.sql("SELECT sn.number FROM s3_files s3 JOIN sn_table sn ON sn.sys_id = s3.sn_id "
                        "WHERE sn.table_name = 'incident'").fetchall()) == 3
    assert _ins(tickets.calls[0]) == []


def test_a_lookup_join_calls_once_per_value_and_keeps_the_argument_as_a_column():
    duck, calls = DuckAPI(), []

    def assets(limit=None):
        return pd.DataFrame({"id": [1, 2, 3], "host": ["a", "b", "c"]})

    def vulns(asset_id: int, limit=None):  # the id builds the URL: not a column of what comes back
        calls.append(asset_id)
        return pd.DataFrame({"cve": [f"CVE-{asset_id}"]})

    duck.register_api_function("assets", assets)
    duck.register_api_function("vulns", vulns)
    rows = duck.sql("SELECT a.host, v.cve FROM assets a JOIN vulns v ON v.asset_id = a.id "
                    "WHERE a.id > 1 ORDER BY 1").fetchall()
    assert rows == [("b", "CVE-2"), ("c", "CVE-3")] and sorted(calls) == [2, 3]


def test_which_side_is_narrowed_follows_the_join():
    duck, tickets, reads = _duck()
    # RIGHT JOIN: the right side is kept whole, the left one narrowed
    duck.sql("SELECT * FROM sn_table sn RIGHT JOIN s3_files s3 ON sn.sys_id = s3.sn_id "
             "WHERE sn.table_name = 'incident'").fetchall()
    assert sorted(v for call in tickets.calls for c in _ins(call) for v in c.value) == ["a", "c", "d"]
    # FULL JOIN: neither
    duck.sql("SELECT * FROM s3_files s3 FULL JOIN sn_table sn ON sn.sys_id = s3.sn_id "
             "WHERE sn.table_name = 'incident'").fetchall()
    assert _ins(tickets.calls[-1]) == []
    # a constant on the kept side of a LEFT JOIN's ON doesn't filter it: never sent to it
    reads.clear()
    rows = duck.sql("SELECT count(*) FROM s3_files s3 LEFT JOIN sn_table sn "
                    "ON sn.sys_id = s3.sn_id AND s3.name = 'y' WHERE sn.table_name = 'incident'").fetchone()
    assert rows == (4,) and reads == [None]


def test_a_where_without_declared_in_never_gets_one():
    duck, got = DuckAPI(), []

    def other(where=None, limit=None):  # an older where: knows eq/like/comparisons only
        got.append(where)
        return pd.DataFrame({"k": ["a", "z"]})

    duck.register_api_function("other", other)
    duck.register_api_function("s3_files", lambda where=None, limit=None: pd.DataFrame({"sn_id": ["a"]}))
    assert duck.sql("SELECT o.k FROM s3_files s JOIN other o ON o.k = s.sn_id").fetchall() == [("a",)]
    assert got == [None]


def test_join_pushdown_can_be_turned_off():
    duck, tickets, _ = _duck(join_pushdown=False)
    duck.sql("SELECT * FROM s3_files s3 JOIN sn_table sn ON sn.sys_id = s3.sn_id "
             "WHERE sn.table_name = 'incident'").fetchall()
    assert _ins(tickets.calls[0]) == []


def test_servicenow_sends_the_values_as_an_encoded_in_a_hundred_per_request():
    from unittest.mock import patch

    from duckduck import ServiceNow

    sn, duck = ServiceNow("dev1", "u", "p"), DuckAPI()
    duck.register_api_function("sn_table", sn.table)
    keys = [f"k{i:03d}" for i in range(150)]
    duck.register_api_function("s3_files", lambda where=None, limit=None: pd.DataFrame({"sn_id": keys}))
    with patch.object(sn, "_get", return_value={"result": [{"sys_id": "k001", "number": "I1"}]}) as get:
        rows = duck.sql("SELECT sn.number FROM s3_files s3 JOIN sn_table sn ON sn.sys_id = s3.sn_id "
                        "WHERE sn.table_name = 'incident'").fetchall()
    queries = [c.args[1]["sysparm_query"] for c in get.call_args_list]
    assert len(queries) == 2 and all(q.startswith("sys_idIN") for q in queries)
    assert sorted(v for q in queries for v in q[len("sys_idIN"):].split(",")) == keys
    assert rows == [("I1",), ("I1",)]  # the fake answers k001 to both requests: DuckDB still joins exactly
    assert sn._condition_clause(__import__("duckduck").common.pushdown.Condition("sys_id", "in", ("a,b",)))[0] is None


def test_a_sql_database_gets_a_real_in(tmp_path):
    import sqlalchemy as sa

    from duckduck.connectors.databases.sql import SQLDatabase

    db = SQLDatabase(f"sqlite:///{tmp_path}/crm.db")
    with db.engine.begin() as c:
        c.execute(sa.text("CREATE TABLE customers (id INTEGER, name TEXT)"))
        c.execute(sa.text("INSERT INTO customers VALUES (1, 'acme'), (2, 'globex'), (3, 'initech')"))
    from unittest.mock import patch

    duck = DuckAPI()
    duck.register_api_function("crm_table", db.table)
    duck.register_api_function("orders", lambda limit=None: pd.DataFrame({"customer": [1, 3, None]}))
    with patch.object(db, "_where_clauses", wraps=db._where_clauses) as spy:
        rows = duck.sql("SELECT c.name FROM orders o JOIN crm_table(table_name='customers') c "
                        "ON c.id = o.customer ORDER BY 1").fetchall()
    assert rows == [("acme",), ("initech",)]
    [condition] = spy.call_args.args[1]
    assert (condition.column, condition.op, sorted(condition.value)) == ("id", "in", [1, 3])  # 1.0/3.0 → ints
    clause = db._where_clauses(db._reflect("customers"), [condition])[0]
    assert "IN" in str(clause)

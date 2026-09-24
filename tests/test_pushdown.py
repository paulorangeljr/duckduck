"""Operator-aware push-down: LIKE/ILIKE/comparisons, safe LIMIT, qualifiers, per-connector translation."""

from typing import List, Optional
from unittest.mock import MagicMock

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.pushdown import Condition, conditions_to_sql, map_conditions, parse_like, require_like

HOSTS = [
    {"hostname": "web-prod-1", "ip": "10.0.0.1", "severity": 9},
    {"hostname": "web-dev-1", "ip": "10.0.0.2", "severity": 3},
    {"hostname": "db-prod-1", "ip": "10.0.0.3", "severity": 7},
    {"hostname": "WEB-PROD-2", "ip": "10.0.0.4", "severity": 5},
]


# ---------------------------------------------------------------------------
# pattern translation + parameter mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern, kind, text", [
    ("%web%", "contains", "web"), ("web%", "startswith", "web"),
    ("%prod", "endswith", "prod"), ("web", "equals", "web"),
])
def test_parse_like_translatable(pattern, kind, text):
    assert parse_like(pattern) == (parse_like(pattern).__class__(kind, text))


@pytest.mark.parametrize("pattern", ["%", "%%", "web_1", "%a%b%", "a\\%b", 5, None])
def test_parse_like_untranslatable(pattern):
    assert parse_like(pattern) is None


def test_require_like_explains_itself():
    with pytest.raises(ValueError, match="hostname_ilike='web_1'"):
        require_like("web_1", "hostname_ilike")


def test_map_conditions():
    conds = [
        Condition("hostname", "like", "web%"),
        Condition("owner", "ilike", "%bob%"),
        Condition("title", "like", "%a_b%"),      # '_' wildcard: not translatable
        Condition("severity", "gte", 5),
        Condition("status", "eq", "open"),
    ]
    params = {"hostname_ilike", "owner_like", "title_ilike", "severity_gte", "status"}
    kwargs, consumed = map_conditions(params, conds)
    # LIKE may go to a case-insensitive param (superset); ILIKE never to a case-sensitive one
    assert kwargs == {"hostname_ilike": "web%", "severity_gte": 5, "status": "open"}
    assert [c.column for c in consumed] == ["hostname", "severity", "status"]


def test_map_conditions_prefers_exact_like_param():
    kwargs, _ = map_conditions({"hostname_like", "hostname_ilike"}, [Condition("hostname", "like", "web%")])
    assert kwargs == {"hostname_like": "web%"}


def test_where_param_takes_everything():
    conds = [Condition("a", "eq", 1), Condition("b", "like", "%x_y%")]
    kwargs, consumed = map_conditions({"where"}, conds)
    assert kwargs == {"where": conds} and consumed == conds


def test_conditions_to_sql_quotes_and_skips_unknown_columns():
    sql = conditions_to_sql(
        [Condition("name", "eq", "O'Brien"), Condition("ghost", "eq", 1), Condition("n", "gte", 5),
         Condition("host", "like", "%a_b%")],
        ["Name", "N", "Host"],
    )
    assert sql == """"Name" = 'O''Brien' AND "N" >= 5 AND "Host" LIKE '%a_b%'"""


# ---------------------------------------------------------------------------
# DuckAPI.sql()
# ---------------------------------------------------------------------------


class Spy:
    def __init__(self):
        self.calls = []

    def assets(self, hostname: Optional[str] = None, hostname_ilike: Optional[str] = None,
               severity_gte: Optional[int] = None, limit: Optional[int] = None):
        """Case-insensitive contains server (returns a superset for LIKE)."""
        self.calls.append(dict(hostname=hostname, hostname_ilike=hostname_ilike, severity_gte=severity_gte, limit=limit))
        rows = HOSTS
        if hostname:
            rows = [r for r in rows if r["hostname"] == hostname]
        if hostname_ilike:
            like = require_like(hostname_ilike)
            needle = like.text.lower()
            test = {"contains": lambda h: needle in h, "startswith": lambda h: h.startswith(needle),
                    "endswith": lambda h: h.endswith(needle), "equals": lambda h: h == needle}[like.kind]
            rows = [r for r in rows if test(r["hostname"].lower())]
        if severity_gte is not None:
            rows = [r for r in rows if r["severity"] >= severity_gte]
        return rows[:limit] if limit else rows


@pytest.fixture
def spy_duck():
    spy = Spy()
    duck = DuckAPI()
    duck.register_api_function("assets", spy.assets)
    yield spy, duck
    duck.close()


def test_like_is_pushed_as_a_pattern_and_duckdb_keeps_it_exact(spy_duck):
    spy, duck = spy_duck
    df = duck.sql("SELECT hostname FROM assets WHERE hostname LIKE 'web%' ORDER BY 1").df()
    assert spy.calls[-1]["hostname_ilike"] == "web%"
    assert spy.calls[-1]["hostname"] is None  # the old bug: pattern sent as equality
    # the server matched case-insensitively (WEB-PROD-2 too); DuckDB's LIKE is exact
    assert df["hostname"].tolist() == ["web-dev-1", "web-prod-1"]


def test_ilike_reaches_the_case_insensitive_param(spy_duck):
    spy, duck = spy_duck
    df = duck.sql("SELECT hostname FROM assets WHERE hostname ILIKE '%prod%' ORDER BY 1").df()
    assert spy.calls[-1]["hostname_ilike"] == "%prod%"
    assert df["hostname"].tolist() == ["WEB-PROD-2", "db-prod-1", "web-prod-1"]


def test_untranslatable_like_stays_in_duckdb(spy_duck):
    spy, duck = spy_duck
    df = duck.sql("SELECT hostname FROM assets WHERE hostname LIKE 'web_prod%' LIMIT 1").df()
    assert spy.calls[-1] == dict(hostname=None, hostname_ilike=None, severity_gte=None, limit=None)
    # '_' matches any one character ('-' here). Pushed as a literal
    # "starts with web_prod", the API would have returned nothing.
    assert df["hostname"].tolist() == ["web-prod-1"]


def test_comparison_goes_to_suffix_param_not_equality(spy_duck):
    spy, duck = spy_duck
    df = duck.sql("SELECT hostname FROM assets WHERE severity >= 7 ORDER BY 1").df()
    assert spy.calls[-1]["severity_gte"] == 7
    assert df["hostname"].tolist() == ["db-prod-1", "web-prod-1"]


def test_limit_pushed_only_when_every_condition_was(spy_duck):
    spy, duck = spy_duck
    duck.sql("SELECT * FROM assets WHERE hostname LIKE 'web%' LIMIT 1").df()
    assert spy.calls[-1]["limit"] == 1
    # ip has no parameter: capping at the source could drop the matching row
    df = duck.sql("SELECT * FROM assets WHERE ip = '10.0.0.3' LIMIT 1").df()
    assert spy.calls[-1]["limit"] is None and df["hostname"].tolist() == ["db-prod-1"]


@pytest.mark.parametrize("query", [
    "SELECT * FROM assets ORDER BY severity DESC LIMIT 1",
    "SELECT DISTINCT hostname FROM assets LIMIT 1",
    "SELECT count(*) AS n FROM assets LIMIT 1",
    "SELECT * FROM assets WHERE hostname = 'x' OR severity > 1 LIMIT 1",
    "SELECT * FROM assets WHERE NOT hostname = 'x' LIMIT 1",
])
def test_limit_not_pushed_when_it_would_change_the_answer(spy_duck, query):
    spy, duck = spy_duck
    duck.sql(query).df()
    assert spy.calls[-1]["limit"] is None


def test_order_by_limit_returns_the_true_top_row(spy_duck):
    _, duck = spy_duck
    df = duck.sql("SELECT hostname FROM assets ORDER BY severity DESC LIMIT 1").df()
    assert df["hostname"].tolist() == ["web-prod-1"]  # severity 9 — not just the API's first row


def test_qualified_conditions_only_reach_their_own_table():
    a_calls, b_calls = [], []

    def a(hostname=None, limit=None):
        a_calls.append(hostname)
        return [{"hostname": "h1", "k": 1}, {"hostname": "h2", "k": 2}]

    def b(hostname=None, limit=None):
        b_calls.append(hostname)
        return [{"hostname": "h2", "k": 1}, {"hostname": "zzz", "k": 2}]

    duck = DuckAPI()
    duck.register_api_function("a", a)
    duck.register_api_function("b", b)
    df = duck.sql("SELECT x.k FROM a AS x JOIN b y ON x.k = y.k WHERE x.hostname = 'h1'").df()
    assert a_calls[-1] == "h1" and b_calls[-1] is None
    assert df["k"].tolist() == [1]


def test_stream_pushes_like_to_the_iter_function():
    seen = []

    def iter_assets(hostname_ilike=None):
        seen.append(hostname_ilike)
        yield pd.DataFrame(HOSTS)

    duck = DuckAPI()
    duck.register_api_function("assets", Spy().assets)
    duck.register_streaming_function("assets", iter_assets)
    chunks = list(duck.stream("SELECT hostname FROM assets WHERE hostname LIKE '%dev%'"))
    assert seen == ["%dev%"] and chunks[0]["hostname"].tolist() == ["web-dev-1"]


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------


def test_sqldatabase_runs_where_natively(tmp_path):
    sa = pytest.importorskip("sqlalchemy")
    from duckduck.database import SQLDatabase

    db = SQLDatabase(f"sqlite:///{tmp_path}/t.db")
    with db.engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE hosts ("HostName" TEXT, sev INTEGER)'))
        conn.execute(sa.text("INSERT INTO hosts VALUES ('web-prod-1', 9), ('web_prod', 3), ('db-1', 7), ('a\\b', 1)"))
    statements = []
    sa.event.listen(db.engine, "before_cursor_execute", lambda *a: statements.append(a[2]))

    duck = DuckAPI()
    duck.register_api_function("db_table", db.table)
    df = duck.sql(
        "SELECT HostName FROM db_table(table_name='hosts') WHERE HostName LIKE 'web_prod%' AND sev >= 3 LIMIT 5"
    ).df()
    assert sorted(df["HostName"]) == ["web-prod-1", "web_prod"]  # '_' matches any char, like DuckDB
    sql = statements[-1]
    assert "LIKE" in sql and ">=" in sql and "LIMIT" in sql and "web" not in sql  # bound params

    df = duck.sql("SELECT HostName FROM db_table(table_name='hosts') WHERE HostName LIKE 'a\\b'").df()
    assert df["HostName"].tolist() == ["a\\b"]  # backslash is literal, as in DuckDB


def test_lakehouse_scan_filters_inside_duckdb(tmp_path):
    from duckduck.lakehouse import LakehouseConnection

    path = tmp_path / "hosts.csv"
    pd.DataFrame(HOSTS).to_csv(path, index=False)
    lake = LakehouseConnection()
    df = lake.scan(f"read_csv('{path}')", where=[
        Condition("hostname", "like", "web%"), Condition("severity", "gt", 4), Condition("structural", "eq", "x"),
    ])
    assert df["hostname"].tolist() == ["web-prod-1"]


def test_servicenow_like_operators():
    from duckduck.servicenow import ServiceNow

    q = ServiceNow._build_query(state="2", short_description_ilike="%vpn%", number_ilike="INC00%")
    assert q == "state=2^short_descriptionLIKEvpn^numberSTARTSWITHINC00"
    assert ServiceNow._build_query(name_ilike="%-prod") == "nameENDSWITH-prod"
    with pytest.raises(ValueError, match="'\\^'"):
        ServiceNow._build_query(short_description_ilike="%x^ORactive=false%")


def test_servicenow_incidents_signature_exposes_like_params():
    import inspect

    from duckduck.servicenow import ServiceNow

    assert {"short_description_ilike", "number_ilike"} <= set(inspect.signature(ServiceNow.incidents).parameters)


def test_insightvm_hostname_like_uses_search_operators():
    from duckduck.rapid7 import InsightVM

    r7 = InsightVM.__new__(InsightVM)
    r7.default_page_size = 100
    r7._post = MagicMock(return_value={"resources": [{"id": 1}]})
    r7.assets(hostname_ilike="web%")
    body = r7._post.call_args.args[1]
    assert body["filters"] == [{"field": "host-name", "operator": "starts-with", "value": "web"}]


def test_axonius_like_is_an_escaped_case_insensitive_regex():
    from duckduck.axonius import Axonius

    assert Axonius._aql_like("f", "web.prod%", "p") == 'f == regex("^web\\\\.prod", "i")'
    assert Axonius._aql_like("f", "%prod", "p") == 'f == regex("prod$", "i")'
    assert Axonius._aql_eq("f", 'a"b') == 'f == "a\\"b"'


def test_blocker_hook_keeps_refused_conditions_out_of_where():
    from duckduck.pushdown import assign_conditions

    conds = [Condition("a", "eq", 1), Condition("b_value", "eq", 2)]
    blocker = lambda c: "nope" if c.column.endswith("_value") else None  # noqa: E731
    kwargs, consumed = map_conditions({"where"}, conds, blocker)
    assert kwargs == {"where": [conds[0]]} and consumed == [conds[0]]
    assert assign_conditions({"where"}, conds, blocker) == {conds[0]: "where"}


# ---------------------------------------------------------------------------
# Regressions: structural params in WHERE next to `where`; empty results
# ---------------------------------------------------------------------------


def test_structural_param_in_where_still_filled_when_function_takes_where():
    calls = []

    def table(table_name, where=None, limit=None):
        calls.append((table_name, where))
        return [{"name": "a", "dns_domain": "x.auql.net"}]

    duck = DuckAPI()
    duck.register_api_function("t", table)
    df = duck.sql("SELECT * FROM t WHERE table_name = 'cmdb_ci' AND dns_domain LIKE '%auql%'").df()
    table_name, where = calls[-1]
    assert table_name == "cmdb_ci"
    assert [(c.column, c.op) for c in where] == [("dns_domain", "like")]  # not table_name
    assert list(df.columns) == ["name", "dns_domain"]  # structural condition stripped from the query


def _empty_duck(rows=None):
    duck = DuckAPI()
    duck.register_api_function("t", lambda table_name, where=None, limit=None: rows if rows is not None else [])
    duck.register_api_function("u", lambda limit=None: [{"k": 1, "v": "x"}])
    return duck


def test_empty_result_takes_its_columns_from_the_query():
    df = _empty_duck().sql("SELECT name, dns_domain FROM t(table_name='x') WHERE dns_domain LIKE '%a%' ORDER BY name").df()
    assert df.empty and list(df.columns) == ["name", "dns_domain"]


def test_empty_result_supports_numeric_and_aggregate_queries():
    duck = _empty_duck()
    row = duck.sql("SELECT count(*) AS n, sum(score) AS s FROM t(table_name='x') WHERE score >= 5").df().iloc[0]
    assert row["n"] == 0 and pd.isna(row["s"])  # SQL: sum over no rows is NULL


def test_empty_select_star_without_references_uses_a_placeholder():
    df = _empty_duck().sql("SELECT * FROM t(table_name='x')").df()
    assert df.empty and list(df.columns) == [DuckAPI.EMPTY_PLACEHOLDER_COLUMN]


def test_empty_source_in_a_join_uses_only_its_own_qualified_columns():
    df = _empty_duck().sql("SELECT a.name, b.v FROM t(table_name='x') a JOIN u b ON a.k = b.k").df()
    assert df.empty and list(df.columns) == ["name", "v"]


def test_empty_frame_with_columns_is_used_as_is():
    typed = pd.DataFrame({"hostname": pd.Series(dtype="string"), "severity": pd.Series(dtype="int64")})
    duck = DuckAPI()
    duck.register_api_function("t", lambda limit=None: typed)
    df = duck.sql("SELECT * FROM t WHERE severity > 3").df()
    assert df.empty and list(df.columns) == ["hostname", "severity"]

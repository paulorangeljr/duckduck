"""KQL in the SQL tab: DuckAPI's table references read before translating, the SQL adjusted after,
native ADX when only ADX is read — and, with the real kql extension (scripts/build_kql_extension.sh),
end to end: joins across sources, arguments, time filters pushed down, syntax errors caught."""

import json
import os
from datetime import datetime, timezone

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.kql import (DEFAULT_EXTENSION, EXTENSION_ENV, KqlError, KqlTranslator, KqlUnavailable, _masked,
                          resolve_time)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


class DataExplorer:
    def __init__(self):
        self.calls = []

    def table(self, table_name: str, where=None, limit=None):
        """An ADX table."""
        self.calls.append(("table", table_name, sorted((c.column, c.op, c.value) for c in where or []), limit))
        return pd.DataFrame({"Host": ["web01", "db01", "web02"], "Bytes": [5, 8, 20],
                             "Timestamp": pd.to_datetime(["2026-09-30 11:30", "2026-09-01 00:00", "2026-09-30 11:45"])})

    def query(self, kql: str, limit=None):
        self.calls.append(("query", kql, limit))
        return pd.DataFrame({"ran": [kql]})


DataExplorer.table.__module__ = DataExplorer.query.__module__ = "duckduck.adx"


class ServiceNow:
    def __init__(self):
        self.calls = []

    def table(self, table_name: str, where=None, limit=None):
        self.calls.append((table_name, sorted((c.column, c.op, c.value) for c in where or []), limit))
        return pd.DataFrame({"cmdb_ci": ["web01", "web02"], "number": ["INC1", "INC2"]})


def _duck():
    duck, adx, sn = DuckAPI(), DataExplorer(), ServiceNow()
    for name, fn, svc in [("adx_table", adx.table, "adx"), ("adx_query", adx.query, "adx"), ("sn_table", sn.table, "sn")]:
        duck.register_api_function(name, fn)
        duck.service_of[name] = svc
        duck.service_prefix[svc] = svc
    return duck, adx, sn


def _fake(mapping):
    """A translator standing in for the extension: the marked KQL → SQL."""
    return KqlTranslator(translate=lambda kql: mapping[kql], now=lambda: NOW)


# --- without the extension ----------------------------------------------------

def test_time_is_resolved_before_translating():
    out = resolve_time("T | where ts > ago(1d) and ts < now() and x > now(-1h) and s == 'ago(2d)'", NOW)
    assert out == ("T | where ts > datetime(2026-09-29T12:00:00.000000Z) and ts < datetime(2026-09-30T12:00:00.000000Z)"
                   " and x > datetime(2026-09-30T11:00:00.000000Z) and s == 'ago(2d)'")
    assert resolve_time("T | where ts > ago(90m) | where ts > ago(time(2h))", NOW).count("datetime(") == 2
    assert resolve_time("T | where ts > ago(x)", NOW) == "T | where ts > ago(x)"  # an expression: left alone


def test_strings_and_comments_are_never_read_as_names():
    kql = "T | where a == 'adx.X' // adx.Y\n| where b == \"sn.z\""
    assert "adx" not in _masked(kql) and "sn" not in _masked(kql)


def test_addresses_calls_and_arguments_become_placeholders_and_come_back():
    duck, adx, sn = _duck()
    translator = _fake({
        "['__duckref_0'] | join kind=inner (['__duckref_1'] | project Host = cmdb_ci) on Host":
            "SELECT L.*, R.* FROM __duckref_0 AS L JOIN (SELECT cmdb_ci AS Host FROM __duckref_1) AS R ON L.Host = R.Host",
        "['__duckref_0'] | take 3": "SELECT * FROM __duckref_0 LIMIT 3",
        "sn_table | where arg.table_name == 'problem'": "SELECT * FROM sn_table WHERE json_extract(arg, '$.table_name') = 'problem'",
        "['__duckref_0'] | where Host =~ 'WEB01'": "SELECT * FROM __duckref_0 WHERE UPPER(Host) = UPPER('WEB01')",
    })
    t = translator.to_sql("adx.ProxyLogs | join kind=inner (sn.incident | project Host = cmdb_ci) on Host", duck)
    assert t.route == "translated" and "FROM adx.ProxyLogs AS L" in t.sql and "FROM sn.incident)" in t.sql
    assert duck.sql(t.sql).df()["Host"].tolist() == ["web01", "web02"]
    assert translator.to_sql("sn_table('incident') | take 3", duck).sql == "SELECT * FROM sn_table(table_name='incident') LIMIT 3"
    assert translator.to_sql("sn_table(table_name=\"change\") | take 3", duck).sql.startswith("SELECT * FROM sn_table(table_name='change')")
    assert translator.to_sql("['sn.incident'] | take 3", duck).sql == "SELECT * FROM sn.incident LIMIT 3"
    t = translator.to_sql("sn_table | where arg.table_name == 'problem'", duck)
    assert t.sql == "SELECT * FROM sn_table WHERE arg.table_name = 'problem'"
    duck.sql(t.sql).df()
    assert sn.calls[-1] == ("problem", [], None)
    # =~ becomes an exact ILIKE, which reaches the source
    t = translator.to_sql("adx.ProxyLogs | where Host =~ 'WEB01'", duck, native=False)
    assert t.sql == "SELECT * FROM adx.ProxyLogs WHERE Host ILIKE 'WEB01'"
    duck.sql(t.sql).df()
    assert adx.calls[-1] == ("table", "ProxyLogs", [("host", "ilike", "WEB01")], None)


def test_has_reaches_the_source_as_the_like_it_implies():
    duck, adx, _ = _duck()
    regex = "regexp_matches(CAST(Url AS VARCHAR), '(?i)(?:^|[^\\p{L}\\p{N}])github(?:[^\\p{L}\\p{N}]|$)')"
    translator = _fake({"['__duckref_0'] | where Url has 'github'": f"SELECT * FROM __duckref_0 WHERE {regex}",
                        "['__duckref_0'] | where not(Url has 'github')": f"SELECT * FROM __duckref_0 WHERE NOT ({regex})"})
    t = translator.to_sql("adx.ProxyLogs | where Url has 'github'", duck, native=False)
    assert t.sql == f"SELECT * FROM adx.ProxyLogs WHERE (Url ILIKE '%github%' AND {regex})"
    assert duck._extract_pushdown(duck.resolve_addresses(t.sql)).conditions[0].op == "ilike"
    t = translator.to_sql("adx.ProxyLogs | where not(Url has 'github')", duck, native=False)
    assert duck._extract_pushdown(duck.resolve_addresses(t.sql)).conditions == []  # under NOT: stays with DuckDB


def test_a_table_read_twice_gets_an_alias_per_read():
    duck, adx, _ = _duck()
    translator = _fake({"['__duckref_0'] | where Host == 'web01' | join kind=inner (['__duckref_1'] | where Host == 'web02') on Bytes":
                        "SELECT * FROM (SELECT * FROM __duckref_0 WHERE Host = 'web01') AS L JOIN "
                        "(SELECT * FROM __duckref_1 WHERE Host = 'web02') AS R ON L.Bytes < R.Bytes"})
    t = translator.to_sql("adx.ProxyLogs | where Host == 'web01' | join kind=inner (adx.ProxyLogs | where Host == 'web02') on Bytes",
                          duck, native=False)
    assert "adx.ProxyLogs AS _k1" in t.sql and "adx.ProxyLogs AS _k2" in t.sql
    duck.sql(t.sql).df()
    assert sorted(c[2] for c in adx.calls) == [[("host", "eq", "web01")], [("host", "eq", "web02")]]


def test_only_adx_tables_run_on_adx_as_kql():
    duck, adx, _ = _duck()
    translator = KqlTranslator(extension="/nowhere/kql.duckdb_extension", now=lambda: NOW)  # no extension at all
    t = translator.to_sql("adx.ProxyLogs | where Timestamp > ago(1h) | summarize count() by Host", duck)
    assert t.route == "native" and t.service == "adx"
    assert duck.sql(t.sql).df()["ran"].tolist() == [
        "['ProxyLogs'] | where Timestamp > datetime(2026-09-30T11:00:00.000000Z) | summarize count() by Host"]
    # another source in it → it has to be translated, and there's no translator
    with pytest.raises(KqlUnavailable, match="Releases"):
        translator.to_sql("adx.ProxyLogs | join (sn.incident) on $left.Host == $right.cmdb_ci", duck)
    assert translator.status()["available"] is False


def test_management_commands_are_refused_but_show_tables():
    duck, _, _ = _duck()
    translator = _fake({})
    assert translator.to_sql(".show tables", duck).sql == "SHOW TABLES"
    with pytest.raises(KqlError, match="queries only"):
        translator.to_sql(".drop table T", duck)
    with pytest.raises(KqlError, match="no KQL"):
        translator.to_sql("  ", duck)


def test_the_console_and_the_api_take_kql(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.commands import serve

    (tmp_path / "sn_fake.py").write_text(
        "import pandas as pd\n"
        "def table(table_name: str, limit=None):\n"
        "    return pd.DataFrame({'t': [table_name], 'n': [1]})\n"
        "TABLES = {'table': table}\n")
    (tmp_path / "duckduck.json").write_text(json.dumps({
        "services": {"sn": {"connector": "python", "module": "sn_fake.py"}},
        "kql": {"extension": "missing/kql.duckdb_extension"}}))
    app = serve(config_path=str(tmp_path / "duckduck.json"), run=False)
    client = TestClient(app)
    status = client.get("/api/sql/kql").json()
    assert status["available"] is False and str(tmp_path / "missing") in status["reason"] and status["native"] == []
    got = client.post("/api/sql", json={"sql": "sn.incident | take 1", "language": "kql"}).json()
    assert "build_kql_extension" in got["error"] and got["language"] == "kql"
    assert client.post("/api/sql", json={"sql": "SELECT 1", "language": "cobol"}).json()["error"].startswith("unknown language")
    assert client.post("/api/sql/translate", json={"kql": ".drop table x"}).status_code == 400


def test_the_page():
    from duckduck.semantic.webpage import PAGE

    assert 'data-lang="kql"' in PAGE and "function inLang(" in PAGE and "/api/sql/kql" in PAGE
    assert "language: LANG" in PAGE and "Translated to SQL" in PAGE and "/api/sql/translate" in PAGE
    # beta, with its limits; the ⓘ's examples in the editor's language
    assert 'class="betapill">Beta' in PAGE and "w.code = inLang(w.sql)" in PAGE and "esc(w.code)" in PAGE and "kqlPushdown(" in PAGE


# --- with the real extension (skipped when it isn't built) ----------------------

def _extension():
    for path in (os.environ.get(EXTENSION_ENV), os.path.expanduser(DEFAULT_EXTENSION)):
        if path and os.path.isfile(path):
            return path
    return None


real = pytest.mark.skipif(_extension() is None, reason="the kql extension isn't built (scripts/build_kql_extension.sh)")


@real
def test_a_join_across_adx_and_servicenow_pushes_each_side_its_filters():
    duck, adx, sn = _duck()
    translator = KqlTranslator(extension=_extension(), now=lambda: NOW)
    t = translator.to_sql("adx.ProxyLogs\n| where Timestamp > ago(1h) and Host startswith 'web'\n"
                          "| join kind=inner (sn.incident | project Host = cmdb_ci, number) on Host\n| project Host, number", duck)
    assert t.route == "translated"
    out = duck.sql(t.sql).df()
    assert out.sort_values("Host").values.tolist() == [["web01", "INC1"], ["web02", "INC2"]]
    assert adx.calls[-1] == ("table", "ProxyLogs", [("host", "ilike", "web%"), ("timestamp", "gt", "2026-09-30 11:00:00")], None)
    assert sn.calls[-1] == ("incident", [], None)


@real
def test_table_functions_three_ways():
    duck, _, sn = _duck()
    translator = KqlTranslator(extension=_extension(), now=lambda: NOW)
    for kql in ("sn.problem | take 2", "sn_table('problem') | take 2", "sn_table(table_name='problem') | take 2",
                "sn_table | where arg.table_name == 'problem' | take 2"):
        duck.sql(translator.to_sql(kql, duck).sql).df()
        assert sn.calls[-1] == ("problem", [], 2), kql


@real
def test_a_typo_is_an_error_not_another_query():
    duck, _, _ = _duck()
    translator = KqlTranslator(extension=_extension(), now=lambda: NOW)
    if not translator.checks_syntax:
        pytest.skip("this extension build has no kql_syntax_errors — rebuild with scripts/build_kql_extension.sh")
    with pytest.raises(KqlError, match="projct"):
        translator.to_sql("sn.incident | where number == 'x' | projct a", duck)
    with pytest.raises(KqlError, match="line 2"):
        translator.to_sql("sn.incident\n| where number ==", duck)

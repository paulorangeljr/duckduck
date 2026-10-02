"""ORDER BY … LIMIT n read as a top N at the source: every bundled table either sorts there (@sortable) or says
why it can't; pages-mode APIs stop once they hold the top N, query engines get ORDER BY + LIMIT together."""

import logging
import sqlite3

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.common.kinds import CATALOG, RAW_QUERY, kind_of
from duckduck.common.pushdown import sortable_of
from duckduck.connectors.registry import SERVICE_REGISTRY

#: tables whose source can't sort for a top N — and why. A new table goes in here or gets @sortable.
NO_SORT = {
    **{("sharepoint", t): "Graph's $orderby works on indexed columns only and pages by a nextLink cursor"
       for t in ("sites", "lists", "list_columns", "list_items", "drives", "drive_items", "search_files",
                 "file_versions")},
    **{("insightvm", t): "which properties the API's sort takes isn't documented per endpoint (an unknown one is a 400)"
       for t in ("vulnerabilities", "asset_vulnerabilities", "sites", "scan_engines", "scans", "report_templates",
                 "reports", "tags", "asset_groups", "users", "policies", "policy_rules", "remediation_projects")},
    ("axonius", "devices"): "the v2 entity API documents no sort",
    ("axonius", "users"): "the v2 entity API documents no sort",
    ("nvd", "cves"): "the CVE API has no sort parameter",
    ("restcountries", "countries"): "no sort parameter (sorted by name here)",
    ("airflow", "dags"): "the API sorts DAGs by text fields only (collation)",
    ("airflow", "task_instances"): "Airflow 2's task instance list has no order_by",
}


def _tables():
    for service, spec in SERVICE_REGISTRY.items():
        cls = getattr(spec.factory, "__self__", None)
        for table, method in spec.tables.items():
            fn = getattr(cls, method, None)
            if fn is not None and kind_of(fn) not in (CATALOG, RAW_QUERY):
                yield service, table, fn


@pytest.mark.parametrize("service,table,fn", [pytest.param(*t, id=f"{t[0]}.{t[1]}") for t in _tables()])
def test_every_table_sorts_at_the_source_or_says_why_not(service, table, fn):
    sorts = sortable_of(fn)
    if sorts is None:
        assert (service, table) in NO_SORT, (
            f"{service}.{table}: declare @sortable (the API's sort, dates/numbers — or exact=True for a query "
            f"engine) or add it to NO_SORT with the reason")
        return
    assert (service, table) not in NO_SORT, f"{service}.{table} sorts: take it out of NO_SORT"
    import inspect

    assert "order_by" in inspect.signature(fn).parameters, f"{service}.{table}: @sortable needs an order_by parameter"


def _rows(n=30, missing=()):
    return pd.DataFrame({"id": range(n), "name": [f"n{i:02d}" for i in range(n)],
                         "score": [None if i in missing else (i * 7) % n for i in range(n)]})


def test_a_sql_database_gets_order_by_and_limit_together(tmp_path, caplog):
    sa = pytest.importorskip("sqlalchemy")
    from duckduck.connectors.databases.sql import SQLDatabase

    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as conn:
        _rows(missing={3, 9}).to_sql("t", conn, index=False)
    db = SQLDatabase(f"sqlite:///{path}")
    duck = DuckAPI()
    duck.register_api_function("db_table", db.table)
    with caplog.at_level(logging.INFO, logger="duckduck"):
        got = duck.sql("SELECT id FROM db_table WHERE table_name = 't' ORDER BY score DESC LIMIT 3").fetchall()
    assert got == duckdb_answer(_rows(missing={3, 9}), "score DESC", 3)
    sent = [r.getMessage() for r in caplog.records if r.getMessage().startswith("sqlite:")]
    assert sent and "ORDER BY CASE WHEN" in sent[0] and "LIMIT" in sent[0]  # NULLs last, then the top 3
    assert any("✓ LIMIT 3 → limit (sorted at the source" in r.getMessage() for r in caplog.records)
    # text: the database's collation isn't DuckDB's — neither the order nor the limit is sent
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="duckduck"):
        got = duck.sql("SELECT name FROM db_table WHERE table_name = 't' ORDER BY name DESC LIMIT 2").fetchall()
    assert [r[0] for r in got] == ["n29", "n28"]
    sent = [r.getMessage() for r in caplog.records if r.getMessage().startswith("sqlite:")]
    assert sent and "ORDER BY" not in sent[0] and "LIMIT" not in sent[0]


def duckdb_answer(df, order, n):
    import duckdb

    return duckdb.sql(f"SELECT id FROM df ORDER BY {order} LIMIT {n}").fetchall()


def test_local_files_sort_inside_the_scan(tmp_path):
    df = _rows(missing={2})  # the scan is DuckDB itself: text sorts the same, so any column goes
    df.to_parquet(tmp_path / "t.parquet")
    duck = DuckAPI()
    duck.auto_register({"local": {"connector": "files", "path": str(tmp_path), "table_prefix": ""}})
    for order in ("score DESC", "score", "score DESC, id", "name DESC"):
        got = duck.sql(f"SELECT id FROM t ORDER BY {order} LIMIT 4 OFFSET 1").fetchall()
        import duckdb

        assert got == duckdb.sql(f"SELECT id FROM df ORDER BY {order} LIMIT 4 OFFSET 1").fetchall(), order


class _SNResponse:
    def __init__(self, rows, total):
        self.status_code, self._rows, self.headers = 200, rows, {"X-Total-Count": str(total)}

    def raise_for_status(self):
        pass

    def json(self):
        return {"result": self._rows}


class _SNSession:
    """A ServiceNow table that honours ORDERBY / ORDERBYDESC — empty dates as ServiceNow keeps them ("")."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        rows = list(self.rows)
        query = params.get("sysparm_query") or ""
        for part in query.split("^"):
            if part.startswith("ORDERBY"):
                desc = part.startswith("ORDERBYDESC")
                field = part[len("ORDERBYDESC" if desc else "ORDERBY"):]
                full = sorted((r for r in rows if r[field]), key=lambda r: r[field], reverse=desc)
                rows = full + [r for r in rows if not r[field]]  # MariaDB-like: empties last either way
        offset, size = int(params["sysparm_offset"]), int(params["sysparm_limit"])
        return _SNResponse(rows[offset:offset + size], len(rows))


def test_servicenow_reads_pages_in_order_and_stops_at_the_top():
    from duckduck.connectors.api.servicenow import ServiceNow

    rows = [{"number": f"INC{i:03d}", "opened_at": "" if i % 5 == 0 else f"2026-09-{1 + (i * 7) % 28:02d} 10:00:00"}
            for i in range(40)]
    sn = ServiceNow("x", "u", "p", default_page_size=5)
    sn.session = _SNSession(rows)
    duck = DuckAPI()
    duck.register_api_function("sn_incidents", sn.incidents)
    duck.register_streaming_function("sn_incidents", sn.iter_incidents)
    for order in ("opened_at DESC", "opened_at"):
        got = duck.sql(f"SELECT number FROM sn_incidents ORDER BY {order} LIMIT 3").fetchall()
        df = pd.DataFrame(rows).replace("", None)
        import duckdb

        assert got == duckdb.sql(f"SELECT number FROM df ORDER BY {order} LIMIT 3").fetchall(), order
    assert "ORDERBYopened_at" in sn.session.calls[-1]["sysparm_query"]
    assert len(sn.session.calls) < 2 * 8  # 8 pages each: fewer were read


def test_insightvm_assets_sort_by_risk_score():
    from duckduck.connectors.api.insightvm import InsightVM

    vm = InsightVM("h", "u", "p")
    assets = [{"id": i, "riskScore": float((i * 13) % 50)} for i in range(50)]
    seen = []

    def get(path, params=None):
        seen.append(dict(params))
        ordered = sorted(assets, key=lambda a: a["riskScore"], reverse=params.get("sort", "").endswith("DESC"))
        page, size = int(params["page"]), int(params["size"])
        return {"resources": ordered[page * size:(page + 1) * size],
                "page": {"totalResources": len(assets), "totalPages": -(-len(assets) // size)}}

    vm._get = get
    vm.default_page_size = 10
    duck = DuckAPI()
    duck.register_api_function("vm_assets", vm.assets)
    duck.register_streaming_function("vm_assets", vm.iter_assets)
    got = duck.sql("SELECT id FROM vm_assets ORDER BY riskScore DESC LIMIT 3").fetchall()
    assert [r[0] for r in got][0] in {i for i, a in enumerate(assets) if a["riskScore"] == 49.0}
    assert seen[0]["sort"] == "riskScore,DESC" and len(seen) < 5

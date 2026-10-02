"""The SQL tab's catalog: which connectors started (and why not), and the tables behind catalogs, with a preview."""

import warnings

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.common.kinds import catalog


class Warehouse:
    """A connector whose catalog lists the tables behind its table function."""

    def __init__(self, schemas=None, fail=False):
        self.schemas = schemas or {"sales": ["orders", "O'Brien stats"], "hr": ["people"]}
        self.fail, self.catalog_reads = fail, 0

    @catalog(lists="table")
    def tables(self, limit=None):
        """Every table, by schema."""
        self.catalog_reads += 1
        if self.fail:
            raise ConnectionError("catalog endpoint down")
        return pd.DataFrame([{"schema": s, "name": t} for s, ts in self.schemas.items() for t in ts])

    def table(self, schema: str, name: str, limit=None):
        """One table."""
        return pd.DataFrame({"schema": [schema], "name": [name], "rows": [1]})


def _duck(**kw):
    w = Warehouse(**kw)
    duck = DuckAPI()
    duck.register_api_function("wh_tables", w.tables)
    duck.register_api_function("wh_table", w.table)
    duck.service_of.update({"wh_tables": "wh", "wh_table": "wh"})
    return duck, w


def test_the_tables_behind_a_catalog():
    duck, _ = _duck()
    tables, notes = duck.nested_tables()
    assert notes == []
    assert [(t["catalog"], t["label"], t["service"]) for t in tables] == [
        ("wh_tables", "sales.orders", "wh"), ("wh_tables", "sales.O'Brien stats", "wh"), ("wh_tables", "hr.people", "wh")]
    quoted = tables[1]["usage"]
    assert quoted == "SELECT * FROM wh_table(schema='sales', name='O''Brien stats') LIMIT 100"
    assert duck.sql(quoted).df()["name"].tolist() == ["O'Brien stats"]  # the preview runs as written
    assert duck.nested_tables("nope") == ([], [])


def test_a_catalog_that_fails_is_a_note_not_an_error():
    duck, _ = _duck(fail=True)
    tables, notes = duck.nested_tables()
    assert tables == [] and "catalog endpoint down" in notes[0]


def test_the_console_caches_the_catalog_reads():
    pytest.importorskip("pydantic")
    from duckduck.semantic.admin import SQLConsole

    duck, w = _duck()
    console = SQLConsole(duck)
    assert len(console.nested()["tables"]) == 3 and w.catalog_reads == 1
    console.nested()
    assert w.catalog_reads == 1  # cached
    console.nested(refresh=True)
    assert w.catalog_reads == 2


def test_which_connectors_started_and_why_not():
    pytest.importorskip("pydantic")
    from duckduck.semantic.admin import SQLConsole

    duck = DuckAPI()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        duck.auto_register({"nvd": {"connector": "nvd"}, "world": {"connector": "restcountries"}}, on_error="warn")
    configured = {"nvd": {"connector": "nvd"}, "world": {"connector": "restcountries"}}
    status = {s["name"]: s for s in SQLConsole(duck).connections(configured)["services"]}
    assert status["nvd"]["started"] and status["nvd"]["tables"] == 1 and status["nvd"]["error"] is None
    assert not status["world"]["started"] and "authentication" in status["world"]["error"]
    assert status["world"]["connector"] == "restcountries"


def test_the_web_app(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.server import create_app
    from test_answer_shapes import _search

    search = _search()
    duck, _ = _duck()
    search.duck.functions.update(duck.functions)
    search.duck.service_of.update(duck.service_of)
    client = TestClient(create_app(lambda: search, store=None, console=SQLConsole(search.duck)))
    nested = client.get("/api/tables/nested").json()
    assert [t["label"] for t in nested["tables"]] == ["sales.orders", "sales.O'Brien stats", "hr.people"]
    rows = client.post("/api/sql", json={"sql": nested["tables"][0]["usage"]}).json()["rows"]
    assert rows == [["sales", "orders", 1]]
    conn = client.get("/api/connections").json()
    assert {s["name"] for s in conn["services"]} >= {"wh"} and conn["tables"] >= 2
    off = TestClient(create_app(lambda: search, store=None))
    assert off.get("/api/tables/nested").status_code == 403 and off.get("/api/connections").status_code == 403


def _two_warehouses():
    duck, first = _duck()
    second = Warehouse(schemas={"ops": ["tickets"]})
    duck.register_api_function("dw_tables", second.tables)
    duck.register_api_function("dw_table", second.table)
    duck.service_of.update({"dw_tables": "dw", "dw_table": "dw"})
    return duck, first, second


def test_one_connectors_catalog_is_expanded_without_reading_the_others():
    pytest.importorskip("pydantic")
    from duckduck.semantic.admin import SQLConsole

    duck, first, second = _two_warehouses()
    tables, _ = duck.nested_tables(service="dw")
    assert [t["label"] for t in tables] == ["ops.tickets"] and first.catalog_reads == 0
    console = SQLConsole(duck)
    assert console.nested(service="dw")["service"] == "dw" and second.catalog_reads == 2
    console.nested(service="dw")
    assert second.catalog_reads == 2 and first.catalog_reads == 0  # cached per connector
    assert len(console.nested()["tables"]) == 4 and first.catalog_reads == 1
    listed = {t["name"]: t["expandable"] for t in console.tables()}
    assert listed["wh_tables"] and listed["dw_tables"] and not listed["wh_table"]


def test_the_web_app_expands_one_connector():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.server import create_app
    from test_answer_shapes import _search

    search = _search()
    duck, first, _ = _two_warehouses()
    search.duck.functions.update(duck.functions)
    search.duck.service_of.update(duck.service_of)
    client = TestClient(create_app(lambda: search, store=None, console=SQLConsole(search.duck)))
    got = client.get("/api/tables/nested", params={"service": "dw"}).json()
    assert [t["label"] for t in got["tables"]] == ["ops.tickets"] and first.catalog_reads == 0


def test_the_page_has_a_toggle_per_connector():
    from duckduck.semantic.webpage import PAGE

    assert "data-expand=" in PAGE and "loadNested(b.dataset.expand" in PAGE and 'id="expanded"' not in PAGE

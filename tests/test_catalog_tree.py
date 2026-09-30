"""The SQL tab's tables as one tree per connector: saved tables inside their database,
the catalog read once (↻ says what's new), and Save as table's connector / database / name."""

import json

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.kinds import catalog
from duckduck.views import suggested_place

pytest.importorskip("fastapi")


class Lake:
    def __init__(self):
        self.reads = 0
        self.databases = ["sec", "fin"]

    @catalog(lists="table")
    def tables(self, limit=None):
        self.reads += 1
        return pd.DataFrame([{"database": d, "table_name": f"t{i}"} for d in self.databases for i in range(2)])

    def table(self, database: str, table_name: str, where=None, limit=None):
        return pd.DataFrame({"database": [database], "table_name": [table_name], "n": [1]})


def _duck():
    duck, lake = DuckAPI(), Lake()
    for name, fn in [("s3_tables", lake.tables), ("s3_table", lake.table)]:
        duck.register_api_function(name, fn)
        duck.service_of[name] = "s3"
    duck.service_prefix["s3"] = "s3"
    return duck, lake


def test_where_a_new_saved_table_goes():
    duck, _ = _duck()
    bound = {"table": "s3_table", "args": {"database": "sec", "table_name": "t0"}}
    assert suggested_place(duck, bound) == {"connector": "s3", "database": "sec", "name": "t0"}
    # a query: the connector and database it reads from, named so it never takes the table's own address
    q = "SELECT n FROM s3.fin.t1 WHERE n > 0"
    assert suggested_place(duck, duck.view_from_sql(q), q) == {"connector": "s3", "database": "fin", "name": "t1_view"}
    q = "SELECT * FROM s3_tables"
    assert suggested_place(duck, {"sql": q}, q) == {"connector": "s3", "database": None, "name": "tables_view"}
    assert suggested_place(duck, {"sql": "SELECT 1"}, "SELECT 1") == {"connector": None, "database": None, "name": "saved_query_view"}
    # taken → numbered
    duck.register_view("s3.fin.t1_view", {"sql": "SELECT 1 AS x"})
    assert suggested_place(duck, duck.view_from_sql("SELECT n FROM s3.fin.t1"), "SELECT n FROM s3.fin.t1")["name"] == "t1_view_2"


def test_a_saved_query_named_by_address_is_listed_under_its_connector_and_a_new_database_is_just_a_name():
    from duckduck.semantic.admin import SQLConsole

    duck, _ = _duck()
    duck.register_view("s3.my_reports.big", {"sql": "SELECT n FROM s3.fin.t1"})
    duck.register_view("plain_one", {"sql": "SELECT 1 AS x"})
    rows = {t["name"]: t for t in SQLConsole(duck).tables()}
    assert rows["s3_my_reports_big"]["service"] == "s3" and rows["s3_my_reports_big"]["saved_name"] == "s3.my_reports.big"
    assert rows["plain_one"]["service"] == "saved tables"
    assert duck.sql("SELECT * FROM s3.my_reports.big").df()["n"].tolist() == [1]


def test_the_catalog_is_read_once_and_a_refresh_says_what_is_new(tmp_path):
    from duckduck.semantic.admin import SQLConsole

    duck, lake = _duck()
    console = SQLConsole(duck)
    first = console.nested(service="s3")
    assert len(first["tables"]) == 4 and first["new"] == [] and lake.reads == 1
    console.nested(service="s3")  # expanding again: nothing read
    assert lake.reads == 1
    lake.databases = ["sec", "ops"]  # fin's two tables gone, ops's two new
    again = console.nested(service="s3", refresh=True)
    assert lake.reads == 2 and again["gone"] == 2 and len(again["new"]) == 2
    assert all('"ops"' in k for k in again["new"])
    # saving one doesn't make it "new" or "gone" on the next read (ids are the call, not the address)
    duck.register_view("s3.ops.t0", {"table": "s3_table", "args": {"database": "ops", "table_name": "t0"}})
    after = console.nested(service="s3", refresh=True)
    assert after["new"] == [] and after["gone"] == 0


def test_the_api_gives_each_table_a_stable_id_and_the_place(tmp_path):
    from fastapi.testclient import TestClient

    from duckduck.semantic.commands import serve

    (tmp_path / "lake.py").write_text(
        "import pandas as pd\n"
        "from duckduck.kinds import catalog\n"
        "class Lake:\n"
        "    @catalog(lists='table')\n"
        "    def tables(self, limit=None):\n"
        "        return pd.DataFrame([{'database': 'sec', 'table_name': 'proxy'}])\n"
        "    def table(self, database: str, table_name: str, limit=None):\n"
        "        return pd.DataFrame({'n': [1]})\n"
        "lake = Lake()\n"
        "TABLES = {'tables': lake.tables, 'table': lake.table}\n")
    (tmp_path / "duckduck.json").write_text(json.dumps({"services": {"s3": {"connector": "python", "module": "lake.py"}}}))
    client = TestClient(serve(config_path=str(tmp_path / "duckduck.json"), run=False, allow_config_edit=True))
    item = client.get("/api/tables/nested", params={"service": "s3"}).json()["tables"][0]
    assert item["id"] == 's3_tables|s3_table|[["database", "sec"], ["table_name", "proxy"]]'
    check = client.post("/api/views/check", json={"sql": "SELECT n FROM s3.sec.proxy WHERE n > 0"}).json()
    assert check["kind"] == "query" and check["place"] == {"connector": "s3", "database": "sec", "name": "proxy_view"}
    assert check["name"] == "s3.sec.proxy_view"
    made = client.post("/api/views", json={"name": "s3.anything_i_like.proxy_view", "sql": "SELECT n FROM s3.sec.proxy"})
    assert made.status_code == 200, made.text
    assert client.post("/api/sql", json={"sql": "SELECT n FROM s3.anything_i_like.proxy_view"}).json()["rows"] == [[1]]


def test_the_page():
    from duckduck.semantic.webpage import PAGE

    assert "const treeOf = " in PAGE and "const dbSection = " in PAGE and "const placeOf = " in PAGE
    assert 'id="vconn"' in PAGE and 'id="vdb"' in PAGE and 'id="vtbl"' in PAGE and "function composeName(" in PAGE
    assert "NEW_TABLES" in PAGE and "reuse" in PAGE and "SHOW_SAVED" not in PAGE


def _app(tmp_path, n):
    from fastapi.testclient import TestClient

    from duckduck.semantic.commands import serve

    (tmp_path / "lake.py").write_text(
        "import pandas as pd\n"
        "from duckduck.kinds import catalog\n"
        "class Lake:\n"
        "    @catalog(lists='table')\n"
        "    def tables(self, limit=None):\n"
        f"        return pd.DataFrame([{{'database': 'db' + str(i % 7), 'table_name': 't' + str(i)}} for i in range({n})])\n"
        "    def table(self, database: str, table_name: str, limit=None):\n"
        "        return pd.DataFrame({'n': [1]})\n"
        "lake = Lake()\n"
        "TABLES = {'tables': lake.tables, 'table': lake.table}\n")
    (tmp_path / "duckduck.json").write_text(json.dumps({"services": {"s3": {"connector": "python", "module": "lake.py"}}}))
    client = TestClient(serve(config_path=str(tmp_path / "duckduck.json"), run=False, allow_config_edit=True))
    nested = client.get("/api/tables/nested", params={"service": "s3"}).json()["tables"]
    return client, [{"table": x["table"], "args": x["args"]} for x in nested]


def test_register_all_saves_every_table_however_many(tmp_path):
    """It used to stop at the first 500, silently."""
    import time

    client, items = _app(tmp_path, 1389)
    started = client.post("/api/views/many", json={"items": items, "background": True})
    assert started.status_code == 202
    job = started.json()["job_id"]
    for _ in range(200):
        view = client.get(f"/api/jobs/{job}").json()
        if view["state"] == "done":
            break
        time.sleep(0.05)
    assert view["state"] == "done" and len(view["result"]["created"]) == 1389 and view["result"]["skipped"] == []
    assert any("Saving" in (e or {}).get("text", "") for e in view["events"])
    assert len(json.loads((tmp_path / "duckduck.json").read_text())["views"]) == 1389


def test_a_cancelled_register_all_saves_nothing(tmp_path):
    from duckduck import progress

    client, items = _app(tmp_path, 60)
    real = progress.checkpoint
    calls = {"n": 0}

    def cancel_on_second():  # the job is cancelled while it's saving
        calls["n"] += 1
        if calls["n"] == 2:
            raise progress.Cancelled()
        real()

    progress.checkpoint = cancel_on_second
    try:
        import time

        job = client.post("/api/views/many", json={"items": items, "background": True}).json()["job_id"]
        for _ in range(100):
            view = client.get(f"/api/jobs/{job}").json()
            if view["state"] not in ("running", "pausing"):
                break
            time.sleep(0.05)
    finally:
        progress.checkpoint = real
    assert "views" not in json.loads((tmp_path / "duckduck.json").read_text())
    assert client.get("/api/views").json()["views"] == []


def test_the_page_shows_register_all_and_the_default_database():
    from duckduck.semantic.webpage import PAGE

    assert 'id="regstatus"' in PAGE and "function pollRegJob(" in PAGE and "background: true}" in PAGE
    assert 'const DEFAULT_DB = "default"' in PAGE and "CLOSED_DBS" in PAGE  # a click closes even a lone one
    # every connector has a default: its tools (listed first) and its tables with no database; the catalog's header on top
    assert "tool: !t.saved && TOOL_KINDS.has(t.kind)" in PAGE and "part.catalogs.map(t => nestedBlock(t, svc))" in PAGE

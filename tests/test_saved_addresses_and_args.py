"""Saved tables named by address (connector.database.table, like the connector's own tables), and WHERE arg.x = …
— a table function's argument said explicitly, never a column."""

import json

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.common.kinds import catalog
from duckduck.views import function_key, suggested_name


class Lake:
    def __init__(self):
        self.calls = []

    @catalog(lists="table")
    def tables(self, limit=None):
        return pd.DataFrame([{"database": "accountable_cyber", "table_name": "sharepoint_lists"}])

    def table(self, database: str, table_name: str, where=None, limit=None):
        """A table of the lake, by database and name."""
        self.calls.append({"database": database, "table_name": table_name, "where": where, "limit": limit})
        return pd.DataFrame({"title": ["a", "b"], "table_name": ["x", "y"]})


def _duck():
    duck, lake = DuckAPI(), Lake()
    for name, fn in [("s3_data_tables", lake.tables), ("s3_data_table", lake.table)]:
        duck.register_api_function(name, fn)
        duck.service_of[name] = "s3_data"
    duck.service_prefix["s3_data"] = "s3_data"
    return duck, lake


def test_a_saved_table_is_named_by_its_address_by_default():
    duck, lake = _duck()
    definition = {"table": "s3_data_table", "args": {"database": "accountable_cyber", "table_name": "sharepoint_lists"}}
    name = suggested_name(duck, definition)
    assert name == "s3_data.accountable_cyber.sharepoint_lists"
    duck.register_view(name, {**definition, "description": "Lists"})
    assert duck.views[name]["description"] == "Lists" and function_key(name) in duck.functions
    assert duck.view_key[function_key(name)] == name
    # read by its name — the saved table (its description, the catalog's source), push-down unchanged
    out = duck.sql("SELECT title FROM s3_data.accountable_cyber.sharepoint_lists WHERE title = 'a' LIMIT 1").df()
    assert out["title"].tolist() == ["a"] and lake.calls[-1]["limit"] == 1
    assert duck.address_of(function_key(name)) == name
    # the next one of the same table gets a plain name, not a clash
    assert suggested_name(duck, definition) != name


def test_names_by_address_are_checked():
    duck, _ = _duck()
    with pytest.raises(ValueError, match="isn't a table address"):
        duck.register_view("s3_data.bad name.x", {"sql": "SELECT 1"})
    duck.register_view("s3_data.a.b", {"sql": "SELECT 1 AS n"})
    with pytest.raises(ValueError, match="same table as the saved table"):
        duck.register_view("s3_data_a_b", {"sql": "SELECT 2 AS n"})
    duck.unregister_view("S3_DATA.a.b")  # the connector part in any case
    assert "s3_data_a_b" not in duck.functions and not duck.views


def test_the_sql_tab_lists_a_saved_table_by_its_name():
    pytest.importorskip("pydantic")
    from duckduck.semantic.admin import SQLConsole

    duck, _ = _duck()
    duck.register_view("s3_data.accountable_cyber.sharepoint_lists",
                       {"table": "s3_data_table", "args": {"database": "accountable_cyber", "table_name": "sharepoint_lists"}})
    row = {t["name"]: t for t in SQLConsole(duck).tables()}["s3_data_accountable_cyber_sharepoint_lists"]
    assert row["saved_name"] == row["address"] == "s3_data.accountable_cyber.sharepoint_lists"
    assert row["usage"] == "SELECT * FROM s3_data.accountable_cyber.sharepoint_lists LIMIT 100"
    fn = {t["name"]: t for t in SQLConsole(duck).tables()}["s3_data_table"]
    assert [p["name"] for p in fn["params"] if p["required"]] == ["database", "table_name"]
    assert fn["doc"].startswith("A table of the lake")


def test_arg_says_it_is_an_argument():
    duck, lake = _duck()
    out = duck.sql("SELECT title FROM s3_data_table WHERE arg.database = 'db' AND arg.table_name = 't' "
                   "AND title = 'a' LIMIT 5").df()
    assert out["title"].tolist() == ["a"]
    call = lake.calls[-1]
    assert (call["database"], call["table_name"], call["limit"]) == ("db", "t", 5)
    assert [c.column for c in call["where"]] == ["title"]


def test_next_to_arg_a_bare_name_is_a_column():
    duck, lake = _duck()
    out = duck.sql("SELECT title FROM s3_data_table WHERE arg.database = 'db' AND arg.table_name = 't' "
                   "AND table_name = 'y'").df()
    assert out["title"].tolist() == ["b"]  # the column table_name, filtered — not the argument
    assert lake.calls[-1]["table_name"] == "t" and [c.column for c in lake.calls[-1]["where"]] == ["table_name"]


def test_an_arg_nothing_takes_is_an_error_not_ignored():
    duck, _ = _duck()
    with pytest.raises(ValueError, match="no table in this query takes an argument 'nope'.*database, table_name"):
        duck.sql("SELECT * FROM s3_data_table WHERE arg.nope = 'x'")
    with pytest.raises(ValueError, match="given with ="):
        duck.sql("SELECT * FROM s3_data_table WHERE arg.database LIKE 'x%' AND arg.table_name = 't'")


def test_a_saved_table_from_arg_is_bound():
    duck, _ = _duck()
    assert duck.view_from_sql("SELECT * FROM s3_data_table WHERE arg.database = 'a' AND arg.table_name = 'b'") == {
        "table": "s3_data_table", "args": {"database": "a", "table_name": "b"}}


def test_the_web_app_saves_by_address_and_renames(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.commands import serve

    (tmp_path / "lake.py").write_text(
        "import pandas as pd\n"
        "from duckduck.common.kinds import catalog\n"
        "class Lake:\n"
        "    @catalog(lists='table')\n"
        "    def tables(self, limit=None):\n"
        "        return pd.DataFrame({'database': ['sec'], 'table_name': ['proxy']})\n"
        "    def table(self, database: str, table_name: str, limit=None):\n"
        "        return pd.DataFrame({'t': [database + '.' + table_name]})\n"
        "lake = Lake()\n"
        "TABLES = {'tables': lake.tables, 'table': lake.table}\n")
    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps({"services": {"s3": {"connector": "python", "module": "lake.py"}},
                                "views": {"old_name": {"table": "s3_table", "args": {"database": "sec", "table_name": "proxy"}}}}))
    client = TestClient(serve(config_path=str(path), run=False, allow_config_edit=True))
    # rename a plain one to its address: the same table underneath, so the old one makes way
    moved = client.post("/api/views", json={"name": "s3.sec.proxy", "previous": "old_name",
                                            "sql": "SELECT * FROM s3_table WHERE arg.database = 'sec' AND arg.table_name = 'proxy'"})
    assert moved.status_code == 200, moved.text
    assert set(json.loads(path.read_text())["views"]) == {"s3.sec.proxy"}
    assert client.post("/api/sql", json={"sql": "SELECT t FROM s3.sec.proxy"}).json()["rows"] == [["sec.proxy"]]
    views = client.get("/api/views").json()["views"]
    assert [(v["name"], v["key"]) for v in views] == [("s3.sec.proxy", "s3_sec_proxy")]
    assert client.delete("/api/views/s3.sec.proxy").json() == {"removed": "s3.sec.proxy"}


def test_the_page():
    from duckduck.semantic.webpage import PAGE

    assert 'id="fninfo"' in PAGE and "data-fninfo=" in PAGE and "function showFunctionInfo(" in PAGE
    assert "arg.${p.name}" in PAGE and "ADDRESS_OK" in PAGE
    # on every row, not only when it has params
    assert "${t.params ? `<button type=\"button\" class=\"infobtn\"" not in PAGE and "With its optional arguments" in PAGE


def test_every_table_has_what_the_info_button_shows():
    """ⓘ on every row, not only on table functions: SharePoint's list_items takes only optional arguments."""
    pytest.importorskip("pydantic")
    from typing import Optional

    from duckduck.semantic.admin import SQLConsole

    class Site:
        def list_items(self, site_id: Optional[str] = None, list_id: Optional[str] = None,
                       site_name: Optional[str] = None, list_name: Optional[str] = None,
                       column_names: str = "display", limit: Optional[int] = None):
            """Items of a SharePoint List with expanded fields.

            site_id / site_name: the site — one of the two."""
            self.args = {"site_name": site_name, "list_name": list_name, "limit": limit}
            return pd.DataFrame([{"Title": "a"}])

        def sites(self, limit: Optional[int] = None):
            return pd.DataFrame([{"id": "1"}])

    duck, site = DuckAPI(), Site()
    duck.register_api_function("sp_list_items", site.list_items)
    duck.register_api_function("sp_sites", site.sites)
    rows = {t["name"]: t for t in SQLConsole(duck).tables()}
    items = rows["sp_list_items"]
    assert items["kind"] == "table" and items["doc"].startswith("Items of a SharePoint List")
    assert [p["name"] for p in items["params"] if not p["required"]] == ["site_id", "list_id", "site_name", "list_name", "column_names"]
    assert {p["name"]: p["type"] for p in items["params"]}["site_name"] == "Optional[str]"
    assert rows["sp_sites"]["params"] == [] and rows["sp_sites"]["doc"] == ""
    # the optional arguments are arguments with arg. too
    duck.sql("SELECT * FROM sp_list_items WHERE arg.site_name = 'S' AND arg.list_name = 'Tasks' LIMIT 5").df()
    assert site.args == {"site_name": "S", "list_name": "Tasks", "limit": 5}


def test_a_required_argument_qualified_by_its_table_or_alias_is_an_argument_not_a_column():
    """SQL clients qualify everything: t.table_name = 'x' fills the argument like table_name = 'x', and a bare
    FROM keeps the function's name for columns qualified by it."""
    import pandas as pd

    from duckduck import DuckAPI

    duck, seen = DuckAPI(), []

    def sn_table(table_name: str, where=None, limit=None):
        seen.append(table_name)
        return pd.DataFrame({"number": ["A", "B"], "state": [1, 2]})

    duck.register_api_function("sn_table", sn_table)
    for sql in ["SELECT t.number FROM sn_table t WHERE t.table_name = 'incident' AND t.state = 2",
                'SELECT "table".number FROM sn_table AS "table" WHERE "table".table_name = \'incident\' AND state = 2',
                "SELECT sn_table.number FROM sn_table WHERE sn_table.table_name = 'incident' AND sn_table.state = 2"]:
        assert duck.sql(sql).fetchall() == [("B",)], sql
    assert seen == ["incident"] * 3
    duck.close()

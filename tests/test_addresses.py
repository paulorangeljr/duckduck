"""Tables by address — connector.table / connector.database.table — and, in the SQL tab, the tables behind a
catalog marked when saved, skipped by "Register all" (one write)."""

import json

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.kinds import catalog


class Glue:
    def __init__(self):
        self.calls = []

    @catalog(lists="table")
    def tables(self, limit=None):
        return pd.DataFrame([{"database": "accountable_cyber", "table_name": "sharepoint_lists"},
                             {"database": "sec", "table_name": "proxy logs"}])

    def table(self, database: str, table_name: str, where=None, limit=None):
        self.calls.append({"database": database, "table_name": table_name, "where": where, "limit": limit})
        return pd.DataFrame({"db": [database], "t": [table_name], "n": [1]})


class Database:
    @catalog(lists="table")
    def tables(self, limit=None):
        return pd.DataFrame([{"table_name": "dbo.Customers"}])

    def table(self, table_name: str, where=None, limit=None):
        return pd.DataFrame({"t": [table_name]})


def _duck():
    duck, glue = DuckAPI(), Glue()
    db = Database()
    for name, fn, service in [("s3_data_tables", glue.tables, "s3_data"), ("s3_data_table", glue.table, "s3_data"),
                              ("sql_tables", db.tables, "sql"), ("sql_table", db.table, "sql"),
                              ("nvd_cves", lambda limit=None: pd.DataFrame({"id": ["CVE-1"]}), "nvd")]:
        duck.register_api_function(name, fn)
        duck.service_of[name] = service
    duck.service_prefix.update({"s3_data": "s3_data", "sql": "sql", "nvd": "nvd"})
    return duck, glue


def test_connector_database_table_is_the_table_function_with_its_arguments():
    duck, glue = _duck()
    out = duck.sql("SELECT t FROM s3_data.accountable_cyber.sharepoint_lists WHERE n = 1 LIMIT 5").df()
    assert out["t"].tolist() == ["sharepoint_lists"]
    call = glue.calls[-1]
    assert (call["database"], call["table_name"], call["limit"]) == ("accountable_cyber", "sharepoint_lists", 5)
    assert [c.column for c in call["where"]] == ["n"]  # push-down as on the call itself


def test_the_last_argument_takes_the_rest_and_quotes_keep_a_part_whole():
    duck, glue = _duck()
    assert duck.sql("SELECT t FROM sql.dbo.Customers").df()["t"].tolist() == ["dbo.Customers"]
    assert duck.sql('SELECT t FROM s3_data.sec."proxy logs"').df()["t"].tolist() == ["proxy logs"]


def test_connector_table_and_the_alias_it_gets():
    duck, _ = _duck()
    assert duck.sql("SELECT cves.id FROM nvd.cves").df()["id"].tolist() == ["CVE-1"]
    assert duck.sql("SELECT c.id FROM nvd.cves AS c JOIN sql.dbo.Customers d ON true").df()["id"].tolist() == ["CVE-1"]


def test_strings_comments_and_other_catalogs_are_left_alone():
    duck, _ = _duck()
    out = duck.sql("SELECT 's3_data.x.y' AS s FROM nvd.cves -- FROM s3_data.a.b\n").df()
    assert out["s"].tolist() == ["s3_data.x.y"]
    assert len(duck.sql("SELECT * FROM information_schema.tables").df()) >= 0
    assert duck.resolve_addresses("SELECT * FROM somewhere.else") == "SELECT * FROM somewhere.else"


def test_what_cant_be_read_says_how_to_write_it():
    duck, _ = _duck()
    with pytest.raises(ValueError, match=r"s3_data\.<database>\.<table_name>"):
        duck.sql("SELECT * FROM s3_data.onlyone")
    with pytest.raises(ValueError, match="nvd has no table 'nope'"):
        duck.sql("SELECT * FROM nvd.nope")
    duck.failed_services["lake"] = {"error": "boto3 missing", "prefix": "lake", "connector": "glue"}
    with pytest.raises(ValueError, match="didn't start .boto3 missing"):
        duck.sql("SELECT * FROM lake.a.b")


def test_every_table_has_its_address():
    duck, _ = _duck()
    assert duck.address_of("nvd_cves") == "nvd.cves"
    assert duck.address_of("s3_data_table", {"database": "sec", "table_name": "proxy logs"}) == 's3_data.sec."proxy logs"'
    assert duck.address_of("sql_table", {"table_name": "dbo.Customers"}) == "sql.dbo.Customers"
    assert [t["address"] for t in duck.nested_tables()[0]] == [
        "s3_data.accountable_cyber.sharepoint_lists", 's3_data.sec."proxy logs"', "sql.dbo.Customers"]


def test_a_saved_table_from_an_address_is_bound():
    duck, glue = _duck()
    definition = duck.view_from_sql("SELECT * FROM s3_data.accountable_cyber.sharepoint_lists")
    assert definition == {"table": "s3_data_table", "args": {"database": "accountable_cyber", "table_name": "sharepoint_lists"}}
    duck.register_view("lists", definition)
    duck.sql("SELECT * FROM lists LIMIT 2").df()
    assert glue.calls[-1]["limit"] == 2


def test_the_sql_tab_lists_addresses():
    pytest.importorskip("pydantic")
    from duckduck.semantic.admin import SQLConsole

    duck, _ = _duck()
    tables = {t["name"]: t for t in SQLConsole(duck).tables()}
    assert tables["nvd_cves"]["address"] == "nvd.cves" and tables["nvd_cves"]["usage"] == "SELECT * FROM nvd.cves LIMIT 100"
    assert tables["s3_data_table"]["usage"] == "SELECT * FROM s3_data.<database>.<table_name> LIMIT 100"
    # the console is its own DuckAPI: addresses read the same there
    assert SQLConsole(duck).run("SELECT count(*) FROM nvd.cves")["rows"] == [[1]]


def test_register_all_skips_the_saved_ones_and_writes_once(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.commands import serve

    (tmp_path / "src.py").write_text(
        "import pandas as pd\n"
        "from duckduck.kinds import catalog\n"
        "class Db:\n"
        "    @catalog(lists='table')\n"
        "    def tables(self, limit=None):\n"
        "        return pd.DataFrame({'table_name': ['a', 'b', 'c']})\n"
        "    def table(self, table_name: str, limit=None):\n"
        "        return pd.DataFrame({'t': [table_name]})\n"
        "db = Db()\n"
        "TABLES = {'tables': db.tables, 'table': db.table}\n")
    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps({"services": {"wh": {"connector": "python", "module": "src.py"}},
                                "views": {"mine": {"table": "wh_table", "args": {"table_name": "a"}}}}))
    client = TestClient(serve(config_path=str(path), run=False, allow_config_edit=True))
    nested = client.get("/api/tables/nested", params={"service": "wh"}).json()["tables"]
    assert [(n["address"], n["saved_as"]) for n in nested] == [("wh.a", "mine"), ("wh.b", None), ("wh.c", None)]
    # one argument: no database to group by, the name is that argument
    assert [(n["database"], n["name"]) for n in nested] == [(None, "a"), (None, "b"), (None, "c")]
    items = [{"table": n["table"], "args": n["args"]} for n in nested]
    got = client.post("/api/views/many", json={"items": items}).json()
    assert [v["name"] for v in got["created"]] == ["wh.b", "wh.c"]
    assert got["skipped"] == [{"table": "wh_table", "args": {"table_name": "a"}, "why": "already saved as mine"}]
    assert set(json.loads(path.read_text())["views"]) == {"mine", "wh.b", "wh.c"}
    assert {n["saved_as"] for n in client.get("/api/tables/nested", params={"service": "wh"}).json()["tables"]} == {"mine", "wh.b", "wh.c"}
    assert client.post("/api/sql", json={"sql": "SELECT t FROM wh.c"}).json()["rows"] == [["c"]]
    again = client.post("/api/views/many", json={"items": items}).json()
    assert again["created"] == [] and len(again["skipped"]) == 3


def test_the_page():
    from duckduck.semantic.webpage import PAGE

    assert "data-regall=" in PAGE and "function registerAll(" in PAGE and "JUST_SAVED" in PAGE
    assert 'class="editbtn"' in PAGE


def test_the_tables_behind_a_catalog_are_grouped_by_database(tmp_path):
    """Thousands of tables behind a catalog: the SQL tab groups them by database (every argument but the last)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.admin import nested_group
    from duckduck.semantic.commands import serve

    (tmp_path / "lake.py").write_text(
        "import pandas as pd\n"
        "from duckduck.kinds import catalog\n"
        "class Lake:\n"
        "    @catalog(lists='table')\n"
        "    def tables(self, limit=None):\n"
        "        return pd.DataFrame([{'database': d, 'table_name': f't{i}'} for d in ['sec', 'fin'] for i in range(3)])\n"
        "    def table(self, database: str, table_name: str, limit=None):\n"
        "        return pd.DataFrame({'t': [table_name]})\n"
        "lake = Lake()\n"
        "TABLES = {'tables': lake.tables, 'table': lake.table}\n")
    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps({"services": {"s3": {"connector": "python", "module": "lake.py"}}}))
    client = TestClient(serve(config_path=str(path), run=False))
    nested = client.get("/api/tables/nested", params={"service": "s3"}).json()["tables"]
    assert {(n["database"], n["name"], n["address"]) for n in nested} >= {("sec", "t0", "s3.sec.t0"), ("fin", "t2", "s3.fin.t2")}
    assert nested_group(DuckAPI(), "nothing", {}) == (None, None)


def test_the_page_groups_by_database():
    from duckduck.semantic.webpage import PAGE

    assert "OPEN_DBS" in PAGE and "data-db=" in PAGE and "data-showmore=" in PAGE and "data-regdb=" in PAGE


def test_default_is_the_database_of_tables_that_have_none():
    """The SQL tab lists them under "duckdefault" (or the config's default_database): connector.duckdefault.table reads
    the same as connector.table — where the connector's tables do have databases, only for its own tables."""
    pytest.importorskip("pydantic")
    from duckduck.semantic.admin import SQLConsole

    duck, _ = _duck()
    calls = []

    def sn_table(table_name: str, limit=None):
        calls.append(table_name)
        return pd.DataFrame({"t": [table_name]})

    duck.register_api_function("sn_table", sn_table)
    duck.service_of["sn_table"] = "sn"
    duck.service_prefix["sn"] = "sn"
    assert duck.sql("SELECT count(*) FROM nvd.duckdefault.cves").fetchone() == (1,)
    duck.sql("SELECT * FROM sn.duckdefault.incident").df()
    assert calls == ["incident"]  # not table_name='default.incident'
    duck.register_view("sn.problem", {"table": "sn_table", "args": {"table_name": "problem"}})
    duck.sql("SELECT * FROM SN.DuckDefault.problem").df()
    assert calls[-1] == "problem"
    # s3_data's tables have databases: "default" is one of them (Glue has a real one); duckdefault only names its own tables
    assert duck.resolve_addresses("SELECT * FROM s3_data.default.logs") == \
        "SELECT * FROM s3_data_table(database='default', table_name='logs') AS logs"
    assert duck.resolve_addresses("SELECT * FROM s3_data.duckdefault.logs") == \
        "SELECT * FROM s3_data_table(database='duckdefault', table_name='logs') AS logs"
    # the name is the config's
    duck.default_database = "home"
    duck.sql("SELECT * FROM sn.home.incident").df()
    assert calls[-1] == "incident"
    del duck.default_database
    rows = {t["name"]: t for t in SQLConsole(duck).tables()}
    assert rows["nvd_cves"]["default_address"] == "nvd.duckdefault.cves"
    assert rows["s3_data_table"]["default_address"] is None

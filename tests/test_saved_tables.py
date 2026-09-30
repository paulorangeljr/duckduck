"""Saved tables: a query kept as a table in duckduck.json (views) — bound to a table function (push-down unchanged)
or a saved query (run each time, no file access) — registered by auto_register, created and removed from the page."""

import json
import warnings

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.views import clean_definition, suggested_name


class Tables:
    """A ServiceNow-like connector: one table function, the table named by an argument."""

    base_url = "https://example.invalid"

    def __init__(self):
        self.calls = []

    def table(self, table_name: str, where=None, limit=None):
        """Any table, by name."""
        self.calls.append({"table_name": table_name, "where": where, "limit": limit})
        return pd.DataFrame({"number": ["I1", "I2", "I3"], "priority": ["1", "2", "1"], "from": [table_name] * 3})

    def iter_table(self, table_name: str, where=None):
        self.calls.append({"table_name": table_name, "where": where, "pages": True})
        yield pd.DataFrame({"number": ["I1", "I2"], "priority": ["1", "2"], "from": [table_name] * 2})
        yield pd.DataFrame({"number": ["I3"], "priority": ["1"], "from": [table_name]})


def _duck():
    api = Tables()
    duck = DuckAPI()
    duck.register_api_function("sn_table", api.table)
    duck.register_streaming_function("sn_table", api.iter_table)
    duck.service_of["sn_table"] = "sn"
    return duck, api


@pytest.mark.parametrize("sql", ["SELECT * FROM sn_table(table_name='incident')",
                                 "select * from sn_table where table_name = 'incident';",
                                 "SELECT * FROM SN_TABLE WHERE table_name = 'incident'"])
def test_a_table_functions_arguments_become_a_bound_table(sql):
    duck, _ = _duck()
    assert duck.view_from_sql(sql) == {"table": "sn_table", "args": {"table_name": "incident"}}


@pytest.mark.parametrize("sql", ["SELECT * FROM sn_table(table_name='incident') WHERE priority = '1'",
                                 "SELECT * FROM sn_table WHERE table_name = 'incident' AND priority = '1'",
                                 "SELECT number FROM sn_table(table_name='incident')",
                                 "SELECT * FROM sn_table(table_name='incident') LIMIT 5",
                                 "SELECT * FROM sn_table"])
def test_anything_more_is_a_saved_query(sql):
    duck, _ = _duck()
    assert duck.view_from_sql(sql) == {"sql": sql.rstrip(";")}


def test_a_bound_table_is_the_function_underneath_push_down_included():
    duck, api = _duck()
    duck.register_view("sn_incident", {"table": "sn_table", "args": {"table_name": "incident"}, "description": "Incidents"})
    out = duck.sql("SELECT number FROM sn_incident WHERE priority = '1' LIMIT 1").df()
    assert out["number"].tolist() == ["I1"]
    call = api.calls[-1]
    assert call["table_name"] == "incident" and call["limit"] == 1 and [c.column for c in call["where"]] == ["priority"]
    listed = duck.list_tables().set_index("name").loc["sn_incident"]
    assert listed["kind"] == "table" and listed["description"] == "Incidents" and listed["endpoint"] == Tables.base_url
    assert duck.service_of["sn_incident"] == "sn"
    # no LIMIT to push → page by page, through the bound streaming function
    assert len(duck.sql("SELECT * FROM sn_incident WHERE priority = '1'").df()) == 2 and api.calls[-1].get("pages")
    assert suggested_name(duck, {"table": "sn_table", "args": {"table_name": "change_request"}}) == "sn_change_request"


def test_a_saved_query_runs_each_time_and_can_read_other_saved_tables():
    duck, api = _duck()
    duck.register_view("p1", {"sql": "SELECT number FROM sn_incident WHERE priority = '1'"})  # before what it reads
    duck.register_view("sn_incident", {"table": "sn_table", "args": {"table_name": "incident"}})
    assert duck.sql("SELECT * FROM p1 ORDER BY number").df()["number"].tolist() == ["I1", "I3"]
    assert duck.sql("SELECT count(*) AS n FROM p1").df()["n"][0] == 2
    assert duck.service_of["p1"] == "saved tables"
    assert duck.list_tables().set_index("name").loc["p1", "source"] == "Saved query"


def test_a_saved_query_cant_read_files_write_or_read_itself(tmp_path):
    duck, _ = _duck()
    (tmp_path / "secret.csv").write_text("a\n1\n")
    duck.register_view("peek", {"sql": f"SELECT * FROM read_csv('{tmp_path / 'secret.csv'}')"})
    with pytest.raises(Exception, match="disabled"):
        duck.sql("SELECT * FROM peek").df()
    with pytest.raises(ValueError, match="read queries"):
        clean_definition({"sql": "DROP TABLE x"})
    duck.register_view("loop", {"sql": "SELECT * FROM loop"})
    with pytest.raises(Exception, match="reads itself"):
        duck.sql("SELECT * FROM loop").df()


def test_names_are_checked():
    duck, _ = _duck()
    for bad, why in [("sn_table", "already a table"), ("select", "reserved"), ("Bad-Name", "isn't a table name")]:
        with pytest.raises(ValueError, match=why):
            duck.register_view(bad, {"sql": "SELECT 1"})
    duck.register_view("one", {"sql": "SELECT 1 AS a"})
    with pytest.raises(ValueError, match="already a saved table"):
        duck.register_view("one", {"sql": "SELECT 2 AS a"})
    duck.register_view("one", {"sql": "SELECT 2 AS a"}, replace=True)
    assert duck.sql("SELECT a FROM one").df()["a"][0] == 2
    with pytest.raises(ValueError, match="no parameter"):
        duck.register_view("x", {"table": "sn_table", "args": {"nope": 1}})
    duck.unregister_view("one")
    assert "one" not in duck.functions and "one" not in duck.views


def _project(tmp_path, views, broken=True):
    (tmp_path / "src.py").write_text(
        "import pandas as pd\n"
        "def table(table_name: str, limit=None):\n"
        "    return pd.DataFrame({'number': ['I1', 'I2'], 'from': [table_name] * 2})\n"
        "TABLES = {'table': table}\n")
    config = {"on_error": "warn",
              "services": {"sn": {"connector": "python", "module": "src.py"}},
              "views": views}
    if broken:
        config["services"]["down"] = {"connector": "restcountries"}  # no authentication → fails to start
    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps(config))
    return path


def test_auto_register_brings_them_back_and_says_why_one_couldnt(tmp_path):
    path = _project(tmp_path, {
        "open": {"sql": "SELECT number FROM sn_incident"},
        "sn_incident": {"table": "sn_table", "args": {"table_name": "incident"}},
        "countries_eu": {"table": "down_countries", "args": {"region": "Europe"}},
    })
    duck = DuckAPI()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        duck.auto_register(config_path=str(path))
    assert duck.sql("SELECT * FROM open").df()["number"].tolist() == ["I1", "I2"]
    assert "service 'down' didn't start" in duck.failed_views["countries_eu"]
    assert any("countries_eu" in str(w.message) for w in caught)
    with pytest.raises(ValueError, match="countries_eu"):
        DuckAPI().auto_register(services={"sn": {"connector": "python", "module": str(tmp_path / "src.py")}},
                                views={"countries_eu": {"table": "down_countries", "args": {}}})


def test_the_config_validates_them():
    from duckduck.semantic.admin import validate_config

    ok = validate_config({"services": {}, "views": {"a": {"sql": "SELECT 1"}, "b": {"table": "t", "args": {"x": 1}}}})
    assert ok == {"errors": [], "warnings": []}
    bad = validate_config({"services": {}, "views": {"A-1": {"sql": "DELETE FROM t"}, "c": {"table": "t", "sql": "SELECT 1"}}})
    assert len(bad["errors"]) == 3


def test_the_web_app_saves_and_removes_them(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.commands import serve

    path = _project(tmp_path, {}, broken=False)
    readonly = TestClient(serve(config_path=str(path), run=False))
    assert readonly.post("/api/views/check", json={"sql": "SELECT * FROM sn_table(table_name='incident')"}).json()["off"]
    assert readonly.post("/api/views", json={"name": "x", "sql": "SELECT 1"}).status_code == 403

    client = TestClient(serve(config_path=str(path), run=False, allow_config_edit=True))
    assert client.get("/api/meta").json()["features"]["saved_tables"] is True
    check = client.post("/api/views/check", json={"sql": "SELECT * FROM sn_table WHERE table_name = 'incident'"}).json()
    assert check == {"kind": "bound", "table": "sn_table", "args": {"table_name": "incident"}, "name": "sn_incident", "off": None,
                     "address": "sn.incident"}
    assert client.post("/api/views/check", json={"sql": "DELETE FROM x"}).json()["error"]
    made = client.post("/api/views", json={"name": "sn_incident", "sql": "SELECT * FROM sn_table(table_name='incident')",
                                            "description": "Incidents"})
    assert made.status_code == 200 and made.json()["kind"] == "bound"  # a plain call stays bound
    saved = json.loads(path.read_text())["views"]
    assert saved == {"sn_incident": {"table": "sn_table", "args": {"table_name": "incident"}, "description": "Incidents"}}
    assert (tmp_path / "duckduck.json.bak").exists()
    assert client.post("/api/sql", json={"sql": "SELECT count(*) FROM sn_incident"}).json()["rows"] == [[2]]
    tables = {t["name"]: t for t in client.get("/api/tables").json()}
    assert tables["sn_incident"]["saved"] == "bound" and tables["sn_incident"]["service"] == "sn"
    assert client.post("/api/views", json={"name": "sn_table", "sql": "SELECT 1"}).status_code == 400
    assert client.post("/api/views", json={"name": "sn_incident", "sql": "SELECT 1"}).status_code == 400  # not replace
    # the whole file saved from Config keeps it and reconnects with it
    config = client.get("/api/config").json()["config"]
    assert client.put("/api/config", json={"config": config}).status_code == 200
    assert client.post("/api/sql", json={"sql": "SELECT count(*) FROM sn_incident"}).json()["rows"] == [[2]]
    assert client.delete("/api/views/sn_incident").json() == {"removed": "sn_incident"}
    assert "views" not in json.loads(path.read_text())
    assert "sn_incident" not in {t["name"] for t in client.get("/api/tables").json()}
    assert client.delete("/api/views/sn_incident").status_code == 404


def test_the_page():
    from duckduck.semantic.webpage import PAGE

    assert 'id="sqlsave"' in PAGE and 'id="viewdlg"' in PAGE and "data-savetable=" in PAGE
    assert 'data-cfgpage="saved"' in PAGE and "function formSaved(" in PAGE
    assert 'id="expanded"' not in PAGE  # the catalog expands per connector only

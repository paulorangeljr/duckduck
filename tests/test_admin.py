"""Source icons, the SQL console (read-only, no files/network) and the duckduck.json editor."""

import json
import os
import types

import pandas as pd
import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, SemanticSearch  # noqa: E402
from duckduck.semantic.admin import (SQLConsole, config_reference, mask, read_only_reason,  # noqa: E402
                                     save_config, source_kind, unmask, validate_config)

from test_answer_shapes import CATALOG, _duck  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Icons


@pytest.mark.parametrize("module, kind", [
    ("duckduck.sharepoint", "sharepoint"), ("duckduck.database", "database"), ("duckduck.rapid7", "insightvm"),
    ("duckduck.servicenow", "servicenow"), ("duckduck.glue", "glue"), ("duckduck.adx", "adx"),
    ("duckduck.python_source.sample", "python"), ("duckduck.local_files", "files"), ("my_company.api", "api"),
])
def test_source_kind(module, kind):
    fn = types.FunctionType((lambda: None).__code__, {})
    fn.__module__ = module
    assert source_kind(fn) == kind


def test_icons_reach_the_preview_and_the_page():
    duck = _duck()
    duck.service_of.update({"alerts": "siem", "owners": "cmdb"})
    catalog = dict(CATALOG, sources=dict(CATALOG["sources"], inline={
        "relation": "(SELECT 1 AS id)", "description": "Inline.", "fields": {"id": {}}}))
    search = SemanticSearch(Catalog.model_validate(catalog), duck)
    assert search.source_icon("inline") == "duckdb" and search.source_icon("alerts") == "api"
    systems = search.preview("Which owners work in eng")["systems"]
    assert all("icon" in g and all("icon" in s for s in g["sources"]) for g in systems)


# ---------------------------------------------------------------------------
# SQL console


def _console():
    duck = DuckAPI()
    calls = []

    def alerts(severity=None, limit=None):
        calls.append({"severity": severity, "limit": limit})
        df = pd.DataFrame({"ip": ["1", "2", "3"], "severity": ["low", "high", "high"]})
        return df[df["severity"] == severity] if severity else df

    duck.register_api_function("alerts", alerts)
    duck.service_of["alerts"] = "siem"
    return duck, SQLConsole(duck, max_rows=2), calls


def test_the_console_runs_duck_sql_with_push_down_and_shows_the_log():
    duck, console, calls = _console()
    out = console.run("SELECT * FROM alerts WHERE severity = 'high'")
    assert out["columns"] == ["ip", "severity"] and out["row_count"] == 2 and not out.get("error")
    assert calls[-1]["severity"] == "high"  # pushed to the source
    assert any("severity" in line for line in out["log"])
    capped = console.run("SELECT * FROM alerts")
    assert capped["row_count"] == 3 and len(capped["rows"]) == 2 and capped["truncated"]
    assert console.run("SHOW TABLES")["row_count"] == 1
    assert console.tables()[0]["name"] == "alerts" and console.tables()[0]["service"] == "siem"


def test_the_console_cannot_touch_files_network_or_write(tmp_path):
    duck, console, _ = _console()
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n")
    for query in [f"SELECT * FROM read_csv('{secret}')", f"SELECT * FROM '{secret}'"]:
        assert "disabled by configuration" in console.run(query)["error"]
    for query in ["DROP TABLE alerts", f"COPY (SELECT 1) TO '{tmp_path}/x.csv'", "INSTALL httpfs",
                  "SET enable_external_access = true", "SELECT 1; SELECT 2",
                  "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x", "ATTACH 'x.db'", ""]:
        assert console.run(query)["error"], query
    assert not os.path.exists(tmp_path / "x.csv")
    # the user's own DuckAPI isn't locked: the console has its own connection
    assert duck.conn.execute(f"SELECT count(*) FROM read_csv('{secret}')").fetchone()[0] == 1


@pytest.mark.parametrize("query, ok", [
    ("select * from x", True), ("WITH a AS (SELECT 1) SELECT * FROM a", True), ("-- note\nSHOW TABLES", True),
    ("FROM alerts", True), ("DESCRIBE alerts", True), ("select ';' as semi", True),
    ("delete from x", False), ("update x set a = 1", False), ("pragma version", False),
])
def test_read_only_guard(query, ok):
    assert (read_only_reason(query) is None) == ok


# ---------------------------------------------------------------------------
# Config


def test_the_reference_documents_every_connector_and_option():
    from duckduck.registry import SERVICE_REGISTRY

    ref = config_reference()
    assert {c["connector"] for c in ref["connectors"]} == set(SERVICE_REGISTRY)
    sharepoint = next(c for c in ref["connectors"] if c["connector"] == "sharepoint")
    assert {"name": "client_secret", "secret": True} .items() <= next(
        o for o in sharepoint["options"] if o["name"] == "client_secret").items()
    feedback = next(o for o in ref["semantic"] if o["name"] == "feedback")
    sim = next(o for o in feedback["options"] if o["name"] == "min_similarity")
    assert sim["default"] == 0.6 and "alike" in sim["description"]
    assert any(o["name"] == "provider" for o in ref["ai_provider"])
    assert not any(o["name"] in ("base_dir", "config_file") for o in ref["semantic"])


def test_secrets_travel_masked_and_come_back_from_the_file():
    saved = {"services": {"db": {"connector": "database", "authentication": {
        "type": "local", "password": "hunter2", "username": "sa", "connection_string": "mssql://sa:x@h/db",
        "secret_id": "prod/db", "api_key_env": "X_KEY"}}},
        "ai_providers": {"claude": {"provider": "anthropic", "authentication": {"api_key": "$secret.key"}}}}
    masked = mask(saved)
    auth = masked["services"]["db"]["authentication"]
    assert auth["password"] == auth["connection_string"] == "***"
    assert auth["username"] == "sa" and auth["secret_id"] == "prod/db" and auth["api_key_env"] == "X_KEY"
    assert masked["ai_providers"]["claude"]["authentication"]["api_key"] == "$secret.key"  # a reference, not a secret
    assert unmask(masked, saved) == saved
    edited = json.loads(json.dumps(masked))
    edited["services"]["new"] = {"connector": "database", "authentication": {"password": "***"}}
    with pytest.raises(ValueError, match="services.new.authentication.password"):
        unmask(edited, saved)


def test_validation():
    example = json.load(open(os.path.join(REPO, "duckduck.example.json")))
    assert validate_config(example, os.path.join(REPO, "duckduck.example.json")) == {"errors": [], "warnings": []}
    bad = {"services": {"a": {"connector": "nope"}, "b": {"connector": "sharepoint"},
                        "c": {"connector": "sharepoint", "hostname": "x", "colour": "red",
                              "authentication": {"type": "aws"}}},
           "semantic": {"catalog_pth": "x"}, "extra": 1}
    report = validate_config(bad)
    text = " | ".join(report["errors"])
    assert "unknown connector 'nope'" in text and "authentication block is required" in text
    assert "aws needs secret_id" in text and "catalog_pth" in text
    assert any("colour" in w for w in report["warnings"]) and any("'extra'" in w for w in report["warnings"])


def test_save_keeps_a_backup_and_never_writes_an_invalid_config(tmp_path):
    path = tmp_path / "duckduck.json"
    original = {"services": {"files": {"connector": "files", "path": "data",
                                       "authentication": {"type": "local", "token": "t0p"}}}}
    path.write_text(json.dumps(original))
    edited = mask(original)
    edited["on_error"] = "warn"
    report = save_config(str(path), edited)
    assert report["saved"] == str(path)
    written = json.loads(path.read_text())
    assert written["on_error"] == "warn" and written["services"]["files"]["authentication"]["token"] == "t0p"
    assert json.loads((tmp_path / "duckduck.json.bak").read_text()) == original
    with pytest.raises(ValueError, match="unknown connector"):
        save_config(str(path), {"services": {"x": {"connector": "nope"}}})
    assert json.loads(path.read_text())["on_error"] == "warn"  # untouched


# ---------------------------------------------------------------------------
# The web app


@pytest.fixture
def served(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    (tmp_path / "catalog.json").write_text(json.dumps(CATALOG))
    (tmp_path / "tables.py").write_text(
        "import pandas as pd\n"
        "def tables():\n"
        "    alerts = pd.DataFrame({'ip': ['10.0.0.1', '10.0.0.2'], 'rule': ['brute_force', 'malware'],"
        " 'severity': ['low', 'critical']})\n"
        "    owners = pd.DataFrame({'ip': ['10.0.0.1'], 'owner': ['ana'], 'department': ['eng']})\n"
        "    return {'alerts': lambda limit=None: alerts, 'owners': lambda limit=None: owners}\n")
    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps({
        "services": {"t": {"connector": "python", "module": "tables.py", "table_prefix": ""}},
        "ai_providers": {"claude": {"provider": "anthropic", "authentication": {"type": "local", "api_key": "p4ss"}}},
        "semantic": {"catalog_path": "catalog.json"},
    }))
    return path


def _client(path, **kw):
    from fastapi.testclient import TestClient

    from duckduck.semantic import serve

    client = TestClient(serve(config_path=str(path), run=False, **kw))
    return client


def test_tabs_are_off_unless_asked(served):
    client = _client(served)
    features = client.get("/api/meta").json()["features"]
    assert features == {"sql": False, "config": True, "config_edit": False}
    assert client.post("/api/sql", json={"sql": "SELECT 1"}).status_code == 403
    assert client.put("/api/config", json={"config": {}}).status_code == 403
    config = client.get("/api/config").json()
    assert config["config"]["ai_providers"]["claude"]["authentication"]["api_key"] == "***" and not config["editable"]
    assert "p4ss" not in client.get("/api/config").text


def test_sql_tab(served):
    client = _client(served, allow_sql=True)
    out = client.post("/api/sql", json={"sql": "SELECT * FROM alerts WHERE severity = 'critical'"}).json()
    assert out["rows"] == [["10.0.0.2", "malware", "critical"]]
    assert client.get("/api/tables").json()[0]["icon"] == "python"
    assert client.post("/api/sql", json={"sql": "DROP TABLE alerts"}).json()["error"].startswith("only read")


def test_config_tab_validates_saves_and_reloads(served):
    client = _client(served, allow_sql=True, allow_config_edit=True)
    config = client.get("/api/config").json()["config"]
    assert client.post("/api/config/validate", json={"config": config}).json() == {"errors": [], "warnings": []}
    bad = json.loads(json.dumps(config))
    bad["services"]["t"]["connector"] = "nope"
    assert client.put("/api/config", json={"config": bad}).status_code == 400
    config["services"]["t"]["table_prefix"] = "x"  # tables become x_alerts, x_owners
    saved = client.put("/api/config", json={"config": config}).json()
    assert saved["reloaded"] and json.loads(served.read_text())["ai_providers"]["claude"]["authentication"]["api_key"] == "p4ss"
    names = [t["name"] for t in client.get("/api/tables").json()]
    assert "x_alerts" in names and "alerts" not in names

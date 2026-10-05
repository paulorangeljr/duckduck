"""The SQL tab's "Run in Python": the query as a script / notebook for the user's own computer — the install
line, a masked duckduck.json of only what it reads, and code that really runs with just that file."""

import json
import os
import runpy
import shutil

import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import pycode  # noqa: E402
from duckduck.semantic.admin import SQLConsole  # noqa: E402

SRC = "TABLES = {'hosts': lambda limit=None: [{'ip': '10.0.0.1', 'name': 'web'}, {'ip': '10.0.0.2', 'name': 'db'}]}\n"


@pytest.fixture
def console(tmp_path, monkeypatch):
    (tmp_path / "src.py").write_text(SRC)
    (tmp_path / "duckduck.json").write_text(json.dumps({
        "services": {
            "inv": {"connector": "python", "module": "src.py"},
            "other": {"connector": "python", "module": "src.py"},
            "db": {"connector": "database", "connection_string": f"sqlite:///{tmp_path / 'x.db'}",
                   "authentication": {"type": "local", "password": "hunter2"}},
        },
        "views": {"inv.web_hosts": {"sql": "SELECT * FROM inv.hosts WHERE name = 'web'"},
                  "unused": {"sql": "SELECT 1"}},
    }))
    monkeypatch.chdir(tmp_path)
    duck = DuckAPI()
    duck.auto_register(config_path=str(tmp_path / "duckduck.json"))
    return SQLConsole(duck)


def test_only_what_the_query_reads_goes_in_the_config(console):
    r = console.python_code("SELECT w.name FROM inv.web_hosts w JOIN inv.hosts h ON h.ip = w.ip")
    assert [s["name"] for s in r["services"]] == ["inv"]  # the saved query's own table followed
    assert r["views"] == ["inv.web_hosts"]
    assert set(r["config"]["services"]) == {"inv"} and set(r["config"]["views"]) == {"inv.web_hosts"}
    assert "other" not in r["config_text"] and "unused" not in r["config_text"]
    assert r["install"][-1] == "pip install -e ." and r["extras"] == []


def test_the_script_and_the_notebook_run_with_only_that_config(console, tmp_path, capsys):
    query = "SELECT w.name FROM inv.web_hosts w WHERE w.name LIKE '%e%'"
    r = console.python_code(query)
    there = tmp_path / "laptop"
    there.mkdir()
    shutil.copy(tmp_path / "src.py", there)
    (there / "duckduck.json").write_text(r["config_text"])
    (there / r["script_name"]).write_text(r["script"])
    os.chdir(there)
    runpy.run_path(str(there / r["script_name"]), run_name="__main__")
    assert "web" in capsys.readouterr().out
    notebook = r["notebook"]
    assert notebook["nbformat"] == 4 and notebook["cells"][0]["cell_type"] == "markdown"
    ns = {}
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            exec("".join(cell["source"]), ns)  # noqa: S102 — the generated cells, in order, as Jupyter would
    assert list(ns["df"]["name"]) == ["web"]
    assert "QUERY = r\"\"\"\n" + query + "\n\"\"\"" in r["script"]  # the query as written


def test_secrets_stay_on_the_server_and_extras_follow_the_connectors(console):
    r = console.python_code("SELECT * FROM db_query(sql='SELECT 1')")
    block = r["config"]["services"]["db"]
    assert block["authentication"]["password"] == "***" and "hunter2" not in json.dumps(r)
    assert r["extras"] == ["database"] and any("***" in n for n in r["notes"])
    extras, packages = pycode._extras({"services": {
        "ora": {"connector": "database", "connection_string": "oracle+oracledb://${username}:${password}@h:1521/",
                "authentication": {"type": "aws", "secret_id": "prod/oracle"}},
        "sql": {"connector": "database", "authentication": {"type": "azure", "drivername": "mssql+pyodbc"}},
        "lake": {"connector": "glue", "authentication": {"type": "aws"}},
    }})
    assert extras == ["database", "aws", "oracle", "azure"] and packages == ["pyodbc"]


def test_kql_runs_through_run_kql():
    script = pycode._script("hosts | take 3", "kql")
    assert "from duckduck.kql import run_kql" in script and "df = run_kql(duck, QUERY).df()" in script
    assert pycode._literal('a """ b') == repr('a """ b')  # never a broken literal


def test_the_repository_is_never_written_with_its_credentials(monkeypatch):
    class Done:
        def __init__(self, out):
            self.stdout = out

    monkeypatch.setattr(pycode.os.path, "isdir", lambda p: True)
    for remote, shown in (("https://user:ghp_secret@github.com/acme/duckduck", "https://github.com/acme/duckduck.git"),
                          ("http://local_proxy@127.0.0.1:1234/git/acme/duckduck", None),
                          ("git@github.com:acme/duckduck.git", "git@github.com:acme/duckduck.git")):
        monkeypatch.setattr(pycode.subprocess, "run", lambda *a, _r=remote, **k: Done(_r + "\n"))
        assert pycode._repository() == shown


def test_the_endpoint_and_the_page(console):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app
    from duckduck.semantic.webpage import PAGE

    app = create_app(lambda: None, store=None, console=console)
    r = TestClient(app).post("/api/sql/python", json={"sql": "SELECT * FROM inv.hosts", "language": "sql"}).json()
    assert r["services"][0]["name"] == "inv" and r["script_name"] == "run_query.py"
    assert TestClient(create_app(lambda: None, store=None)).post("/api/sql/python", json={"sql": "x"}).status_code == 403
    assert 'id="sqlpython"' in PAGE and 'id="pydlg"' in PAGE and "/api/sql/python" in PAGE

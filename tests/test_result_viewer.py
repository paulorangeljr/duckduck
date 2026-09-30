"""The SQL tab's result viewer: a query's whole result kept a while — paged, searched, sorted, as CSV — and
editing a saved table's statement (rename, replace)."""

import csv
import io
import json

import pandas as pd
import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic.admin import SQLConsole  # noqa: E402


def _console():
    duck = DuckAPI()
    duck.register_api_function("items", lambda limit=None: pd.DataFrame({
        "n": range(2500), "grp": [f"g{i % 7}" for i in range(2500)], "props": [{"a": i} for i in range(2500)]}))
    return SQLConsole(duck)


def test_the_whole_result_is_kept_and_paged_searched_sorted():
    console = _console()
    r = console.run("SELECT * FROM items")
    assert r["row_count"] == 2500 and r["truncated"] and len(r["rows"]) == 1000 and r["result_id"]
    page = console.page(r["result_id"], offset=1500, limit=10)
    assert page["total"] == page["filtered"] == 2500 and page["row_numbers"][0] == 1501 and page["rows"][0][0] == 1500
    found = console.page(r["result_id"], q="G3", sort="n", desc=True, limit=5)  # every column, any case
    assert found["filtered"] == 357 and [row[0] for row in found["rows"]] == [2495, 2488, 2481, 2474, 2467]
    assert found["rows"][0][2] == {"a": 2495} and found["row_numbers"][0] == 2496
    assert console.page(r["result_id"], limit=5000)["limit"] == 1000
    assert console.page(r["result_id"], q="%")["filtered"] == 0  # searched as text, never a pattern


def test_csv_is_every_row_as_shown():
    console = _console()
    rid = console.run("SELECT * FROM items")["result_id"]
    rows = list(csv.reader(io.StringIO(console.csv(rid, sort="n", desc=True, q="g6", columns=["props", "n"]))))
    assert rows[0] == ["props", "n"] and len(rows) == 1 + 357
    assert json.loads(rows[1][0]) == {"a": 2498} and rows[1][1] == "2498"


def test_only_the_last_few_are_kept():
    console = _console()
    first = console.run("SELECT 1 AS x")["result_id"]
    for _ in range(console.keep_results):
        console.run("SELECT 2 AS x")
    with pytest.raises(KeyError, match="run the query again"):
        console.page(first)


def test_the_web_app_serves_pages_and_csv():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app
    from test_answer_shapes import _search

    search = _search()
    console = _console()
    search.duck.functions.update(console.duck.functions)
    client = TestClient(create_app(lambda: search, store=None, console=SQLConsole(search.duck)))
    rid = client.post("/api/sql", json={"sql": "SELECT * FROM items"}).json()["result_id"]
    page = client.get(f"/api/sql/results/{rid}", params={"q": "g1", "sort": "n", "desc": 1, "limit": 3}).json()
    assert page["filtered"] == 357 and page["rows"][0][0] == 2493
    got = client.get(f"/api/sql/results/{rid}/csv", params={"columns": "grp"})
    assert got.headers["content-type"].startswith("text/csv") and got.text.splitlines()[:2] == ["grp", "g0"]
    assert len(got.text.splitlines()) == 2501
    assert client.get("/api/sql/results/nope").status_code == 404


def test_a_saved_tables_statement_is_shown_and_edited(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.commands import serve
    from test_saved_tables import _project

    path = _project(tmp_path, {"sn_incident": {"table": "sn_table", "args": {"table_name": "incident"}},
                               "reader": {"sql": "SELECT number FROM sn_incident"}}, broken=False)
    client = TestClient(serve(config_path=str(path), run=False, allow_config_edit=True))
    views = {v["name"]: v for v in client.get("/api/views").json()["views"]}
    assert views["sn_incident"]["statement"] == "SELECT * FROM sn_table\nWHERE arg.table_name = 'incident'"
    assert views["reader"]["statement"] == "SELECT number FROM sn_incident"
    # edit in place: another statement, same name
    same = client.post("/api/views", json={"name": "sn_incident", "previous": "sn_incident",
                                           "sql": "SELECT * FROM sn_table WHERE table_name = 'problem'"})
    assert same.status_code == 200 and same.json()["args"] == {"table_name": "problem"}
    assert client.post("/api/sql", json={"sql": "SELECT DISTINCT \"from\" FROM sn_incident"}).json()["rows"] == [["problem"]]
    # rename: the new name registered and in the file, the old one gone from both
    moved = client.post("/api/views", json={"name": "sn_problem", "previous": "sn_incident",
                                            "sql": "SELECT * FROM sn_table(table_name='problem')", "description": "Problems"})
    assert moved.status_code == 200
    saved = json.loads(path.read_text())["views"]
    assert set(saved) == {"reader", "sn_problem"} and saved["sn_problem"]["description"] == "Problems"
    names = {t["name"] for t in client.get("/api/tables").json()}
    assert "sn_problem" in names and "sn_incident" not in names
    assert client.post("/api/views", json={"name": "x", "previous": "nope", "sql": "SELECT 1"}).status_code == 404
    assert client.post("/api/views", json={"name": "reader", "previous": "sn_problem", "sql": "SELECT 1"}).status_code == 400


def test_the_page():
    from duckduck.semantic.webpage import PAGE

    assert 'id="viewer"' in PAGE and 'id="sqlexpand"' in PAGE and "data-expandrows" in PAGE and 'id="vwcsv"' in PAGE
    assert 'id="vstmt"' in PAGE and "data-editview=" in PAGE and "function editSavedTable(" in PAGE
    assert "data-group=" in PAGE and 'id="groupsclose"' in PAGE

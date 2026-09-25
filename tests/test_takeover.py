"""'Take over from here': an answer's rows registered as a table, queried with duck.sql."""

import pytest

pytest.importorskip("pydantic")

from duckduck.semantic import Catalog, SemanticSearch  # noqa: E402

from test_answer_shapes import CATALOG, _duck, _search  # noqa: E402


def test_an_answer_becomes_a_table_to_query_and_join():
    search = _search()
    result = search.search("What are the severities of the events?")
    taken = search.take_over(result)
    assert taken.name == "severities_events" and taken.rows == 3 and taken.columns == ["severity"]
    assert "the rows of the answer" in taken.report()
    assert sorted(search.duck.sql("SELECT * FROM severities_events").df()["severity"]) == ["critical", "high", "low"]
    hosts = search.conversation("Which hosts have critical alerts?").take_over("critical_hosts")
    joined = search.duck.sql("SELECT c.ip, o.owner FROM critical_hosts c JOIN owners o USING (ip) ORDER BY 1").df()
    assert hosts.rows == 3 and joined["owner"].tolist() == ["ana", "bob", "ana"]
    listed = search.duck.list_tables().set_index("name")
    assert listed.loc["critical_hosts", "source"].startswith("Taken over")
    assert "Which hosts have critical alerts?" in listed.loc["critical_hosts", "description"]
    assert search.duck.service_of["critical_hosts"] == "taken over"


def test_an_answer_cut_at_its_row_cap_runs_again_whole():
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck(), default_limit=2)
    result = search.search("Show me the alerts with critical severity")
    assert len(result.results) == 2  # capped
    whole = search.take_over(result, "critical_alerts")
    assert whole.rows == 3 and "without the 2-row cap" in whole.how
    shown = search.take_over(result, "critical_alerts", full=False)  # same name: replaced
    assert shown.rows == 2 and len(search.duck.sql("SELECT * FROM critical_alerts").df()) == 2


def test_names_are_checked_and_defaults_never_clash():
    search = _search()
    result = search.search("What are the severities of the events?")
    for bad, why in [("select", "reserved"), ("owners", "already a table"), ("1abc", "isn't a table name"),
                     ("a-b", "isn't a table name")]:
        with pytest.raises(ValueError, match=why):
            search.take_over(result, bad)
    first, second = search.take_over(result), search.take_over(result)
    assert (first.name, second.name) == ("severities_events", "severities_events_2")
    search.taken_over.clear()  # a reloaded search (the web app after accepting a suggestion) can still replace it
    assert search.take_over(result, "severities_events").name == "severities_events"


def test_only_answers_with_rows():
    search = _search()
    with pytest.raises(ValueError, match="direct reply"):
        search.take_over(search.search("thanks a lot"))
    with pytest.raises(ValueError, match="needs_clarification"):
        search.take_over(search.search("How many alerts by severity?"))


def test_the_web_app(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.server import create_app

    search = _search()
    client = TestClient(create_app(lambda: search, store=None, console=SQLConsole(search.duck)))
    conv = client.post("/api/ask", json={"question": "Which hosts have critical alerts?"}).json()["conversation_id"]
    taken = client.post("/api/takeover", json={"conversation_id": conv, "name": "hot"}).json()
    assert taken["name"] == "hot" and taken["rows"] == 3 and taken["sql"] == "SELECT * FROM hot LIMIT 100"
    assert client.post("/api/sql", json={"sql": "SELECT count(*) AS n FROM hot"}).json()["rows"] == [[3]]
    table = next(t for t in client.get("/api/tables").json() if t["name"] == "hot")
    assert table["icon"] == "dataset" and table["service"] == "taken over"
    assert client.post("/api/takeover", json={"conversation_id": conv, "name": "owners"}).status_code == 400
    assert client.post("/api/takeover", json={"conversation_id": "nope"}).status_code == 404
    off = TestClient(create_app(lambda: search, store=None))
    assert off.post("/api/takeover", json={"conversation_id": conv}).status_code == 403

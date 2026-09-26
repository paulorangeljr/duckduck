"""'Take over from here': an answer's rows registered as a table, queried with duck.sql."""

import json

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
    assert whole.rows == 3 and "every row of its plan (the answer showed 2)" in whole.how  # from the rows read
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


def test_the_dialog_is_told_what_it_would_get():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic import Catalog, SemanticSearch
    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.server import create_app

    from test_answer_shapes import CATALOG, _duck

    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck(), default_limit=2)
    client = TestClient(create_app(lambda: search, store=None, console=SQLConsole(search.duck)))
    conv = client.post("/api/ask", json={"question": "Show me the alerts with critical severity"}).json()
    proposal = client.post("/api/takeover/proposal", json={"conversation_id": conv["conversation_id"]}).json()
    assert proposal["rows"] == 2 and proposal["capped"] and proposal["limit"] == 2
    assert proposal["columns"] == ["ip", "rule", "severity"] and proposal["name"] == "alerts_critical_severity"
    taken = client.post("/api/takeover", json={"conversation_id": conv["conversation_id"],
                                               "name": proposal["name"], "full": True}).json()
    assert taken["rows"] == 3  # the whole subset
    again = client.post("/api/takeover/proposal", json={"conversation_id": conv["conversation_id"]}).json()
    assert again["taken"] == ["alerts_critical_severity"] and again["name"] == "alerts_critical_severity_2"


def test_taking_over_an_unrated_answer_says_it_answered():
    from duckduck.semantic.feedback import FeedbackStore
    from duckduck.semantic.takeover import TAKEOVER_REASON

    store = FeedbackStore()
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck(), feedback=store)
    result = search.search("What are the severities of the events?")
    taken = search.take_over(result, "sev")
    assert taken.feedback["verdict"] == "answered" and store.verdict_of(result.search_id) == "answered"
    assert store.query("SELECT reason FROM feedback")["reason"].tolist() == [TAKEOVER_REASON]
    again = search.take_over(result, "sev")  # already rated: nothing added
    assert again.feedback is None and store.query("SELECT count(*) AS n FROM feedback")["n"][0] == 1
    rated = search.search("Which hosts have critical alerts?")
    search.feedback(rated, "not_answered")
    assert search.take_over(rated, "hot").feedback is None  # the user's own rating stays
    assert store.verdict_of(rated.search_id) == "not_answered"


def test_the_page_saves_feedback_as_it_is_given():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.feedback import FeedbackStore
    from duckduck.semantic.server import create_app

    store = FeedbackStore()
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck(), feedback=store)
    client = TestClient(create_app(lambda: search, store=store, console=SQLConsole(search.duck)))
    answer = client.post("/api/ask", json={"question": "Which hosts have critical alerts?"}).json()
    sid = answer["result"]["search_id"]
    first = client.post("/api/feedback", json={"search_id": sid, "verdict": "not_answered"}).json()["id"]
    same = client.post("/api/feedback", json={"search_id": sid, "verdict": "partial", "categories": ["wrong_filter"],
                                              "reason": "only some", "feedback_id": first}).json()["id"]
    assert same == first and store.query("SELECT count(*) AS n FROM feedback")["n"][0] == 1  # one row, replaced
    assert store.verdict_of(sid) == "partial"
    assert client.post("/api/feedback", json={"search_id": sid, "verdict": "answered",
                                              "feedback_id": "nope"}).status_code == 404
    fresh = client.post("/api/ask", json={"question": "What are the severities of the events?"}).json()
    taken = client.post("/api/takeover", json={"conversation_id": fresh["conversation_id"], "name": "sev"}).json()
    assert taken["feedback"]["verdict"] == "answered"
    assert store.verdict_of(fresh["result"]["search_id"]) == "answered"


def test_the_history_keeps_the_sql_and_the_feedback_to_replace():
    from duckduck.semantic.feedback import FeedbackStore

    store = FeedbackStore()
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck(), feedback=store)
    listed = search.search("What are the severities of the events?")
    looked_up = search.search("What I can find for this ip 10.0.0.3")  # lookup: one SQL per table it looked in
    rows = store.searches().set_index("id")
    assert rows.loc[listed.search_id, "sql"] == listed.sql and "SELECT" in listed.sql
    across = rows.loc[looked_up.search_id, "sql"]
    assert "-- owners\n" in across and "-- alerts\n" in across
    assert rows.loc[listed.search_id, "feedback_id"] is None
    fid = search.feedback(listed, "not_answered", categories=["wrong_answer_kind"], expected={"answer_shape": "count"})
    row = store.searches().set_index("id").loc[listed.search_id]
    assert row["feedback_id"] == fid and json.loads(row["expected"]) == {"answer_shape": "count"}
    search.feedback(listed, "answered", feedback_id=fid)  # the History row's widget replaces it
    assert store.verdict_of(listed.search_id) == "answered"
    assert store.query("SELECT count(*) AS n FROM feedback")["n"][0] == 1

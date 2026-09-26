"""More columns after the answer: the same plan again, with other fields of its tables — no decision re-taken."""

import pytest

pytest.importorskip("pydantic")

from test_answer_shapes import _search  # noqa: E402


def test_the_fields_the_question_used_are_offered_first():
    search = _search()
    result = search.search("Which hosts have critical alerts?")
    options = search.column_options(result)
    assert [(o["ref"], o["asked"], o["suggested"], o["why"]) for o in options] == [
        ("alerts.ip", True, False, ""), ("alerts.severity", False, True, "filtered on"),
        ("alerts.rule", False, False, "")]


def test_added_columns_run_the_same_plan_without_asking_the_engine_again():
    search = _search()
    result = search.search("Which hosts have critical alerts?")
    calls = []
    search.interpreter.interpret = lambda *a, **k: calls.append(a)  # would fail: nothing is re-interpreted
    wider = search.with_columns(result, ["alerts.severity", "alerts.rule"])
    assert calls == [] and wider.status == "ok"
    assert list(wider.results.columns) == ["ip", "severity", "rule"]
    assert set(wider.results["severity"]) == {"critical"} and set(wider.results["ip"]) == set(result.results["ip"])
    assert "\"severity\" = 'critical'" in wider.sql and wider.added_columns == ["alerts.severity", "alerts.rule"]
    assert (wider.decisions[-1].kind, wider.decisions[-1].decided_by) == ("columns", "user")
    assert result.added_columns == [] and list(result.results.columns) == ["ip"]  # the original is untouched
    assert [o["ref"] for o in search.column_options(wider) if o["selected"]] == ["alerts.ip", "alerts.severity",
                                                                                 "alerts.rule"]
    back = search.with_columns(wider, [])  # the complete set each time: [] goes back
    assert list(back.results.columns) == ["ip"] and back.added_columns == []
    assert not any(d.kind == "columns" for d in back.decisions)


def test_only_answers_that_are_rows_take_columns():
    search = _search()
    count = search.search("How many alerts are critical?")
    assert count.status == "ok" and search.column_options(count) == []
    with pytest.raises(ValueError, match="no rows to add columns to"):
        search.with_columns(count, ["alerts.rule"])
    result = search.search("Which hosts have critical alerts?")
    with pytest.raises(ValueError, match="isn't a field of the tables"):
        search.with_columns(result, ["owners.owner"])
    with pytest.raises(ValueError, match="isn't a field of the tables"):
        search.with_columns(result, ["alerts.nope"])


def test_the_web_app():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.server import create_app

    search = _search()
    client = TestClient(create_app(lambda: search, store=None, console=SQLConsole(search.duck)))
    first = client.post("/api/ask", json={"question": "Which hosts have critical alerts?"}).json()
    conv = first["conversation_id"]
    assert [o["ref"] for o in first["result"]["column_options"] if o["suggested"]] == ["alerts.severity"]
    wider = client.post("/api/columns", json={"conversation_id": conv, "columns": ["alerts.severity"]}).json()
    assert wider["result"]["added_columns"] == ["alerts.severity"]
    assert set(wider["result"]["results"][0]) == {"ip", "severity"}
    taken = client.post("/api/takeover", json={"conversation_id": conv, "name": "wide"}).json()
    assert taken["columns"] == ["ip", "severity"]  # taking over takes what's on screen
    assert client.post("/api/columns", json={"conversation_id": conv, "columns": ["x.y"]}).status_code == 400
    assert client.post("/api/columns", json={"conversation_id": conv, "columns": "alerts.rule"}).status_code == 400
    assert client.post("/api/columns", json={"conversation_id": "nope", "columns": []}).status_code == 404

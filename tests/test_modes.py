"""Which mode read each question, what it cost — recorded, compared on the dashboard, filtered in the history."""

import json

import duckdb
import pytest

pytest.importorskip("pydantic")

from duckduck.semantic import Catalog, SemanticSearch  # noqa: E402
from duckduck.semantic.evaluation import compare_readers  # noqa: E402
from duckduck.semantic.feedback import FeedbackStore  # noqa: E402
from duckduck.semantic.llm_extraction import LLMExtractor, LLMReading  # noqa: E402

from test_answer_shapes import CATALOG, _duck  # noqa: E402
from test_readers import Decider  # noqa: E402

READINGS = {"how many departments are there?": (None, LLMReading(answer="count_values", about_kind="field",
                                                                  about="owners.department"))}


def _search(store=None):
    catalog = Catalog.model_validate(CATALOG)
    llm = Decider(READINGS, choices={"activity": "security_alert"})
    return SemanticSearch(catalog, _duck(), feedback=store, llm_reader=LLMExtractor(catalog, llm)), llm


def test_every_search_records_its_mode_and_what_it_cost():
    store = FeedbackStore()
    search, _ = _search(store)
    for reader in ("rules", "llm", "llm_decides"):
        result = search.search("how many departments are there?", reader=reader)
        store.record_feedback(result.search_id, "answered" if reader != "llm" else "not_answered")
    rows = store.searches()
    assert rows["reader"].tolist() == ["llm_decides", "llm", "rules"]  # newest first
    fly = rows.iloc[0]
    assert fly["engine"].startswith("LLMDecisionBackend") and fly["engine_calls"] >= 1 and fly["llm_calls"] >= 1
    assert rows.iloc[2]["engine"] == "LexicalDecisionEngine" and rows.iloc[2]["llm_calls"] == 0
    assert store.searches(reader="llm")["reader"].tolist() == ["llm"]
    stored = store.search(fly["id"])
    assert stored["reader"] == "llm_decides" and stored["usage"]["engine_calls"] == fly["engine_calls"]


def test_the_dashboard_compares_the_modes():
    store = FeedbackStore()
    search, _ = _search(store)
    for reader, verdict in (("rules", "answered"), ("rules", "not_answered"), ("llm_decides", "answered")):
        store.record_feedback(search.search("how many departments are there?", reader=reader).search_id, verdict)
    by = {r["reader"]: r for r in store.stats()["by_reader"].to_dict(orient="records")}
    assert by["rules"]["searches"] == 2 and by["rules"]["answer_rate"] == 0.5
    assert by["llm_decides"]["answer_rate"] == 1.0 and by["llm_decides"]["avg_llm_calls"] >= 1


def test_typing_is_counted_apart_and_never_twice():
    store = FeedbackStore()
    search, llm = _search(store)
    preview = search.preview("how many departments are there?", reader="llm")
    assert preview["usage"]["llm_calls"] == 1
    result = search.search("how many departments are there?", reader="llm")
    assert result.usage["llm_calls"] == 0 and result.usage["reused"] == 1  # the preview's reading, not a new call
    assert len(llm.calls) == 1
    by = {r["reader"]: r for r in store.stats()["by_reader"].to_dict(orient="records")}
    assert by["llm"]["previews"] == 1 and by["llm"]["typing_llm_calls"] == 1 and by["llm"]["avg_llm_calls"] == 0
    search.preview("how many departments are there?", reader="llm")  # cached: nothing called, nothing recorded
    assert store.query("SELECT count(*) AS n FROM previews")["n"][0] == 1


def test_a_file_from_before_the_modes_is_migrated(tmp_path):
    path = str(tmp_path / "feedback.duckdb")
    conn = duckdb.connect(path)
    conn.execute("""CREATE TABLE searches (id VARCHAR PRIMARY KEY, created_at TIMESTAMP, conversation_id VARCHAR,
        user_name VARCHAR, question VARCHAR, english_question VARCHAR, template VARCHAR, status VARCHAR,
        answer_shape VARCHAR, entity VARCHAR, activity VARCHAR, sources VARCHAR, decisions VARCHAR, pinned VARCHAR,
        sql VARCHAR, rows INTEGER, clarification VARCHAR, elapsed_ms DOUBLE, catalog_version VARCHAR, engine VARCHAR)""")
    conn.execute("INSERT INTO searches (id, created_at, question, status, sources) "
                 "VALUES ('old', now(), 'how many alerts?', 'ok', '[]')")
    conn.close()
    store = FeedbackStore(path)
    search, _ = _search(store)
    search.search("how many departments are there?", reader="llm")
    rows = store.searches()
    assert sorted(rows["reader"]) == ["llm", "rules"]  # an old search was read by the rules: that's all there was
    assert rows.set_index("id").loc["old", "engine_calls"] == 0


def test_the_modes_compared_on_the_same_questions():
    search, _ = _search()
    cases = [{"question": "how many departments are there?", "expected_answer_shape": "count_values"}]
    reports = compare_readers(search, cases)
    assert list(reports) == ["rules", "llm", "llm_decides", "auto"]
    assert all(r.metrics["answer_shape_accuracy"] == 1.0 for r in reports.values())
    assert reports["rules"].metrics["mean_llm_calls"] == 0 and reports["llm_decides"].metrics["mean_engine_calls"] >= 1


def test_the_web_app():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app

    store = FeedbackStore()
    search, _ = _search(store)
    client = TestClient(create_app(lambda: search, store=store))
    answer = client.post("/api/ask", json={"question": "how many departments are there?", "reader": "llm_decides"})
    result = answer.json()["result"]
    assert result["reader"] == "llm_decides" and result["usage"]["engine_calls"] >= 1
    client.post("/api/feedback", json={"search_id": result["search_id"], "verdict": "answered"})
    history = client.get("/api/searches?reader=llm_decides").json()
    assert [h["reader"] for h in history] == ["llm_decides"] and client.get("/api/searches?reader=llm").json() == []
    stats = client.get("/api/stats").json()
    assert stats["by_reader"][0]["reader"] == "llm_decides"
    json.dumps(stats)  # strict JSON: no NaN for a mode without reported cost
    evaluated = client.post("/api/evaluate", json={"readers": ["rules", "llm_decides", "nope"]}).json()
    assert set(evaluated["readers"]) == {"rules", "llm_decides"}

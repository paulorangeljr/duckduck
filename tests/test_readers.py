"""Two ways to read a question: rules (+ the engine for doubts), or an LLM's reading first (+ the engine to decide)."""

import json
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pydantic")

from duckduck.semantic import Catalog, JEVAdapter, JevClient, SemanticSearch  # noqa: E402
from duckduck.semantic.llm_extraction import (LLMExtractionWithReading, LLMExtractor,  # noqa: E402
                                              LLMReading)

from test_answer_shapes import CATALOG, Jev, _duck  # noqa: E402


class Reader:
    """Fake LLM: a canned reading (and English) per question, counting its calls."""

    def __init__(self, readings):
        self.readings, self.calls, self.systems = readings, [], []

    def generate(self, system, prompt, output_model):
        question = prompt.rsplit("Question: ", 1)[1]
        self.calls.append(question)
        self.systems.append(system)
        english, reading = self.readings.get(question, (None, None))
        return output_model(english_question=english or question,
                            **({"reading": reading} if output_model is LLMExtractionWithReading else {}))


def _search(readings, **kw):
    llm = Reader(readings)
    catalog = Catalog.model_validate(CATALOG)
    search = SemanticSearch(catalog, _duck(), llm_reader=LLMExtractor(catalog, llm), **kw)
    return search, llm


DEPARTMENTS = {"quantos departamentos existem?": (
    "how many departments are there?",
    LLMReading(answer="count_values", about_kind="field", about="owners.department"))}


def test_the_rules_reader_is_the_default_and_never_calls_the_llm():
    search, llm = _search(DEPARTMENTS)
    assert search.readers == ["rules", "llm"] and search.reader == "rules"
    search.search("How many alerts per rule?")
    assert not llm.calls


def test_the_llm_reads_the_question_and_its_reading_is_evidence():
    search, llm = _search(DEPARTMENTS)
    result = search.search("quantos departamentos existem?", reader="llm")
    assert result.status == "ok" and result.results.to_dict("records") == [{"count": 2}]
    assert "Also return reading" in llm.systems[0]  # asked for its reading, in the same call as the translation
    record = result.decisions[0]
    assert (record.kind, record.answer, record.decided_by) == ("llm_reading", "count_values", "llm")
    assert "field owners.department" in record.subject
    assert result.intent.reader == "llm" and result.intent.reading.about == "owners.department"


def test_names_the_catalog_does_not_have_are_dropped():
    readings = {"show me stuff please": (None, LLMReading(answer="teleport", about_kind="field", about="owners.nope",
                                                          group_by="alerts.rule"))}
    search, _ = _search(readings)
    reading = search.interpreter.llm_reader.extract("show me stuff please", search.clock(), reading=True).reading
    assert reading.answer is None and reading.about is None and reading.group_by == "alerts.rule"


def test_a_bare_field_name_is_resolved_when_it_is_unique():
    readings = {"what departments are there": (None, LLMReading(answer="values", about_kind="field",
                                                                about="department"))}
    search, _ = _search(readings)
    reading = search.interpreter.llm_reader.extract("what departments are there", search.clock(), reading=True).reading
    assert reading.about == "owners.department"


def test_the_preview_and_the_search_share_one_llm_call():
    search, llm = _search(DEPARTMENTS)
    preview = search.preview("quantos departamentos existem?", reader="llm")
    assert preview["reader"] == "llm" and preview["reading"]["about"] == "owners.department"
    assert preview["question"] == "quantos departamentos existem?"  # as typed: the page matches on it
    assert preview["field"] == "owners.department" and preview["entity"] is None
    search.search("quantos departamentos existem?", reader="llm")
    assert len(llm.calls) == 1


def test_where_the_rules_and_the_llm_disagree_the_engine_decides(monkeypatch):
    jev = Jev(shape={"list": 0.1, "values": 0.9})
    monkeypatch.setattr("requests.Session.post", lambda self, url, data, timeout: jev(url, data, timeout))
    readings = {"show me the owners of the alerts": (None, LLMReading(answer="values", about_kind="field",
                                                                      about="owners.department"))}
    search, _ = _search(readings, engine=JEVAdapter(JevClient(api_key="k"), cache_size=0))
    result = search.search("show me the owners of the alerts", reader="llm")
    body = jev.bodies[0]
    assert set(body["questions"]["answer_shape"]["criteria"]) == {"list", "values"}
    assert body["state"]["items"]["answer_shape"]["facts"]["llm_reading"]["answer"] == "values"
    shape = next(d for d in result.decisions if d.kind == "answer_shape")
    assert shape.answer == "values" and shape.decided_by == "engine" and "LLM read it as values" in shape.subject


def test_a_doubt_between_the_two_readings_is_asked_back(monkeypatch):
    jev = Jev(shape={"list": 0.5, "values": 0.5})
    monkeypatch.setattr("requests.Session.post", lambda self, url, data, timeout: jev(url, data, timeout))
    readings = {"show me the owners of the alerts": (None, LLMReading(answer="values", about_kind="field",
                                                                      about="owners.department"))}
    search, _ = _search(readings, engine=JEVAdapter(JevClient(api_key="k"), cache_size=0))
    result = search.search("show me the owners of the alerts", reader="llm")
    assert result.status == "needs_clarification" and result.followup.kind == "answer_shape"


def test_the_llm_can_find_a_table_to_show():
    readings = {"dump everything in the owners dataset thing": (
        None, LLMReading(answer="browse", about_kind="table", about="owners"))}
    search, _ = _search(readings)
    result = search.search("dump everything in the owners dataset thing", reader="llm")
    assert result.intent.answer_shape == "browse" and list(result.results.columns) == ["ip", "owner", "department"]


def test_without_an_llm_the_llm_reader_is_refused_clearly():
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck())
    assert search.readers == ["rules"]
    with pytest.raises(ValueError, match="needs an LLM"):
        search.search("How many alerts per rule?", reader="llm")
    with pytest.raises(ValueError, match="unknown reader"):
        search.search("How many alerts per rule?", reader="magic")


def test_a_failed_llm_falls_back_to_the_rules_and_says_so():
    class Down:
        def generate(self, *a):
            raise RuntimeError("LLM down")

    catalog = Catalog.model_validate(CATALOG)
    search = SemanticSearch(catalog, _duck(), llm_reader=LLMExtractor(catalog, Down()))
    with pytest.warns(RuntimeWarning, match="LLM extraction failed"):
        result = search.search("How many alerts per rule?", reader="llm")
    assert result.status == "ok" and result.decisions[0].kind == "llm_reading"
    assert "no reading" in result.decisions[0].subject


def test_the_web_app(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app

    search, llm = _search(DEPARTMENTS)
    client = TestClient(create_app(lambda: search, store=None))
    meta = client.get("/api/meta").json()["readers"]
    assert meta == {"available": ["rules", "llm"], "default": "rules", "llm_unavailable": None}
    preview = client.post("/api/preview", json={"question": "quantos departamentos existem?", "reader": "llm"}).json()
    assert preview["reading"]["answer"] == "count_values"
    answer = client.post("/api/ask", json={"question": "quantos departamentos existem?", "reader": "llm"}).json()
    assert answer["result"]["results"] == [{"count": 2}] and len(llm.calls) == 1
    bad = client.post("/api/ask", json={"question": "x y z", "reader": "magic"})
    assert bad.status_code == 400

    rules_only = TestClient(create_app(lambda: SemanticSearch(Catalog.model_validate(CATALOG), _duck()), store=None))
    assert rules_only.get("/api/meta").json()["readers"]["llm_unavailable"].startswith("no LLM configured")

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
        from duckduck.semantic.metering import record_llm

        record_llm(100, 20, 0.01)  # what a real client does (llm._log_call)
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
    assert search.readers == ["rules", "llm", "llm_decides", "auto"] and search.reader == "rules"
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
    assert meta == {"available": ["rules", "llm", "llm_decides", "auto"], "default": "rules", "llm_unavailable": None}
    preview = client.post("/api/preview", json={"question": "quantos departamentos existem?", "reader": "llm"}).json()
    assert preview["reading"]["answer"] == "count_values"
    answer = client.post("/api/ask", json={"question": "quantos departamentos existem?", "reader": "llm"}).json()
    assert answer["result"]["results"] == [{"count": 2}] and len(llm.calls) == 1
    bad = client.post("/api/ask", json={"question": "x y z", "reader": "magic"})
    assert bad.status_code == 400

    rules_only = TestClient(create_app(lambda: SemanticSearch(Catalog.model_validate(CATALOG), _duck()), store=None))
    assert rules_only.get("/api/meta").json()["readers"]["llm_unavailable"].startswith("no LLM configured")


def test_the_reading_never_says_not_about_the_data():
    """Whether a question is about the data is the engine's in_scope call — an LLM's "out of scope" would mislead it."""
    search, llm = _search({"what are my departments": (None, LLMReading(answer="out_of_scope", about_kind="none"))})
    result = search.search("what are my departments", reader="llm")
    assert result.results.to_dict("records") == [{"department": "eng"}, {"department": "ops"}]
    assert result.intent.reading is None  # nothing usable left
    assert "no reading" in result.decisions[0].subject
    assert "out_of_scope" not in llm.systems[0] and "small_talk" not in llm.systems[0]
    assert '"my", "our", "I have"' in llm.systems[0]
    assert search.preview("what are my departments", reader="llm")["reading"] is None


# ---------------------------------------------------------------------------
# llm_decides: the LLM reads the question and makes every decision — no Jev


class Decider(Reader):
    """Fake LLM for both roles: the reading (extraction) and the decisions (as LLMDecisionBackend asks them)."""

    def __init__(self, readings, choices=None):
        super().__init__(readings)
        self.choices, self.judgments = choices or {}, []

    def generate(self, system, prompt, output_model):
        from duckduck.semantic.llm_decisions import _Answer, _Answers, _Scored, _Scores, _YesNo

        if output_model in (LLMExtractionWithReading,) or "Question: " in prompt:
            return super().generate(system, prompt, output_model)
        from duckduck.semantic.metering import record_llm

        record_llm(200, 30, 0.01)
        body = json.loads(prompt)
        self.judgments.append(body)
        if output_model is _Answers:
            answers = []
            for key, j in body["judgments"].items():
                if j["type"] == "choice":
                    opts = list(j.get("options") or {})
                    want = self.choices.get(key)
                    answers.append(_Answer(key=key, option_probabilities=[
                        _Scored(key=o, probability=0.9 if o == want else 0.1 / max(len(opts) - 1, 1)) for o in opts]))
                else:
                    answers.append(_Answer(key=key, yes_probability=0.95))
            return _Answers(answers=answers)
        if output_model is _YesNo:
            return _YesNo(probability=0.95)
        return _Scores(scores=[])


def test_llm_decides_never_asks_jev(monkeypatch):
    jev = Jev()
    monkeypatch.setattr("requests.Session.post", lambda self, url, data, timeout: jev(url, data, timeout))
    llm = Decider({"how many departments are there?": (None, LLMReading(
        answer="count_values", about_kind="field", about="owners.department"))},
        choices={"activity": "security_alert"})
    catalog = Catalog.model_validate(CATALOG)
    search = SemanticSearch(catalog, _duck(), engine=JEVAdapter(JevClient(api_key="k"), cache_size=0),
                            llm_reader=LLMExtractor(catalog, llm))
    result = search.search("how many departments are there?", reader="llm_decides")
    assert result.status == "ok" and result.results.to_dict("records") == [{"count": 2}]
    assert not jev.bodies and llm.judgments  # decided by the LLM, never by Jev
    assert result.reader == "llm_decides" and search.engine_label_for("llm_decides").startswith("LLMDecisionBackend")
    assert result.usage["llm_calls"] == 1 + len(llm.judgments) and result.usage["engine_calls"] == len(llm.judgments)
    # the same question with the llm reader: Jev decides
    jev_result = search.search("how many departments are there?", reader="llm")
    assert jev.bodies and jev_result.usage["engine_calls"] >= 1 and jev_result.usage["llm_calls"] == 0  # cached reading


def test_usage_counts_jev_cost_and_is_per_search(monkeypatch):
    class Costly(Jev):
        def __call__(self, url, data, timeout):
            r = super().__call__(url, data, timeout)
            r.json.return_value["usage"] = {"cost": 0.0002, "input_tokens": 300}
            return r

    jev = Costly()
    monkeypatch.setattr("requests.Session.post", lambda self, url, data, timeout: jev(url, data, timeout))
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck(),
                            engine=JEVAdapter(JevClient(api_key="k"), cache_size=0))
    first = search.search("Which hosts have critical alerts?")
    assert first.usage["engine_calls"] == len(jev.bodies) >= 1
    assert first.usage["cost"] == pytest.approx(0.0002 * len(jev.bodies))
    assert first.to_dict()["usage"] == first.usage and first.to_dict()["reader"] == "rules"
    calls = len(jev.bodies)
    second = search.search("How many alerts per rule?")
    assert second.usage["engine_calls"] == len(jev.bodies) - calls  # its own, not the first search's


def test_real_llm_clients_count_themselves():
    import time

    from duckduck.semantic.llm import _log_call
    from duckduck.semantic.metering import metered

    with metered() as usage:
        _log_call("m", LLMReading, time.perf_counter() - 0.2, "prompt", 1200, 80)
    _log_call("m", LLMReading, time.perf_counter(), "prompt", 5, 5)  # outside a search: not counted anywhere
    assert (usage.llm_calls, usage.llm_tokens_in, usage.llm_tokens_out) == (1, 1200, 80)
    assert usage.llm_seconds >= 0.2

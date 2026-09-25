"""Feedback: the store, case memory, catalog suggestions — and that measuring never learns from itself."""

import json
import os

import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

from duckduck.semantic import Catalog, SemanticConfig, SemanticSearch  # noqa: E402
from duckduck.semantic.evaluation import calibrate_thresholds, evaluate  # noqa: E402
from duckduck.semantic.feedback import FeedbackStore  # noqa: E402
from duckduck.semantic.memory import CaseMemory, question_template  # noqa: E402
from duckduck.semantic.suggest import CatalogSuggester, Suggestion, apply_suggestion  # noqa: E402

from test_answer_shapes import CATALOG, _duck, _jev_search  # noqa: E402


@pytest.fixture
def store():
    return FeedbackStore()


def _search(store, **kw):
    return SemanticSearch(Catalog.model_validate(CATALOG), _duck(), feedback=store, **kw)


# ---------------------------------------------------------------------------
# The store


def test_every_search_is_recorded_without_its_rows(store):
    search = _search(store)
    result = search.search("How many alerts per rule?", user="ana")
    row = store.search(result.search_id)
    assert row["question"] == "How many alerts per rule?" and row["answer_shape"] == "count_by"
    assert row["sources"] == ["alerts"] and row["rows"] == 3 and row["user_name"] == "ana"
    assert row["template"] == "how many alerts per rule" and row["catalog_version"] == search.catalog_version
    assert {d["kind"] for d in row["decisions"]} >= {"answer_shape", "values_field"}
    assert "brute_force" not in json.dumps(row, default=str)  # no result values stored


def test_a_conversation_ties_its_rounds_together(store, monkeypatch):
    search, _ = _jev_search(monkeypatch, shape={"count": 0.45, "count_by": 0.55})
    search.feedback_store = store
    conversation = search.conversation("How many alerts by severity?")
    conversation.answer("1")
    ids = store.query("SELECT DISTINCT conversation_id FROM searches")["conversation_id"].tolist()
    assert ids == [conversation.id] and len(store.searches()) == 2


def test_feedback_is_validated(store):
    sid = _search(store).search("How many alerts per rule?").search_id
    with pytest.raises(ValueError, match="verdict"):
        store.record_feedback(sid, "meh")
    with pytest.raises(ValueError, match="unknown categories"):
        store.record_feedback(sid, "not_answered", ["bad_vibes"])
    with pytest.raises(ValueError, match="expected: unknown"):
        store.record_feedback(sid, "not_answered", expected={"sql": "DROP TABLE x"})
    with pytest.raises(ValueError, match="synonym needs"):
        store.record_feedback(sid, "not_answered", expected={"synonym": {"word": "x"}})
    with pytest.raises(KeyError):
        store.record_feedback("nope", "answered")


def test_stats(store):
    search = _search(store)
    search.feedback(search.search("How many alerts per rule?"), "answered")
    search.feedback(search.search("Which hosts have critical alerts?"), "not_answered", ["wrong_answer_kind"])
    search.search("What is the weather like?")  # not rated
    stats = store.stats()
    assert stats["overall"]["searches"] == 3 and stats["overall"]["rated"] == 2
    assert stats["overall"]["answer_rate"] == 0.5
    assert stats["by_answer_shape"].set_index("key")["answer_rate"].to_dict() == {"count_by": 1.0, "list": 0.0}
    assert stats["by_category"]["key"].tolist() == ["wrong_answer_kind"]


def test_cases_and_the_evaluation_set(store):
    search = _search(store)
    search.feedback(search.search("How many alerts per rule?"), "answered")
    search.feedback(search.search("Which hosts have critical alerts?"), "not_answered", ["wrong_answer_kind"],
                    expected={"answer_shape": "count"})
    search.feedback(search.search("Show the alerts by rule"), "not_answered")  # no correction: teaches nothing
    cases = {c["question"]: c for c in store.cases()}
    assert set(cases) == {"How many alerts per rule?", "Which hosts have critical alerts?"}
    assert cases["How many alerts per rule?"]["kind"] == "confirmed"
    assert cases["Which hosts have critical alerts?"] == {**cases["Which hosts have critical alerts?"],
                                                        "kind": "corrected", "answer_shape": "count"}
    dataset = {d["question"]: d for d in store.to_evaluation()}
    assert dataset["How many alerts per rule?"]["expected_answer_shape"] == "count_by"
    assert dataset["Which hosts have critical alerts?"] == {
        "question": "Which hosts have critical alerts?", "expected_answer_shape": "count",
        "_from": dataset["Which hosts have critical alerts?"]["_from"]}


def test_a_later_no_retires_a_confirmation(store):
    search = _search(store)
    search.feedback(search.search("How many alerts per rule?"), "answered")
    search.feedback(search.search("How many alerts per rule?"), "not_answered")
    assert store.cases() == []


def test_feedback_needs_a_store():
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck())
    result = search.search("How many alerts per rule?")
    assert result.search_id is None
    with pytest.raises(RuntimeError, match="no feedback store"):
        search.feedback(result, "answered")


def test_a_broken_store_never_costs_the_answer(store):
    search = _search(store)
    store.close()
    result = search.search("How many alerts per rule?")
    assert result.status == "ok" and result.search_id is None


# ---------------------------------------------------------------------------
# Case memory


def test_templates_ignore_the_values():
    class Lit:
        def __init__(self, value, kind):
            self.value = self.text = value
            self.kind, self.semantic_type = kind, None

    assert question_template("What can I find for 10.0.0.1?", [Lit("10.0.0.1", "ip_address")]) == \
        "what can i find for <ip_address>"


def test_similar_confirmed_questions_reach_the_engine_as_evidence(store, monkeypatch):
    first = _search(store)
    first.feedback(first.search("How many alerts per rule?"), "answered")

    search, jev = _jev_search(monkeypatch)
    search.feedback_store = store
    search.interpreter.memory = CaseMemory(store)
    result = search.search("How many alerts are there per rule?")
    assert result.intent.similar_cases[0]["question"] == "How many alerts per rule?"
    facts = jev.bodies[0]["state"]["items"]["entity"]["facts"]["similar_confirmed_questions"]
    assert facts[0]["answer_kind"] == "count_by" and facts[0]["confirmed_by_user"] is True
    assert jev.bodies[0]["state"]["items"]["source:alerts"]["facts"]["used_in_similar_confirmed_questions"] is True


def test_memory_settles_what_the_offline_engine_had_no_evidence_for(store):
    search = _search(store, memory=True)
    first = search.search("Which people own machines?")
    assert first.status == "needs_clarification"  # the lexical engine can't tell what's asked for
    search.feedback(first, "not_answered", ["wrong_tables"], expected={"entity": "user", "sources": ["owners"]})
    result = search.search("Which people own the machines?")
    assert result.intent.similar_cases[0]["kind"] == "corrected"
    assert result.intent.target_entity == "user"  # was a 0.33 guess; the corrected case settled it


def test_memory_brings_in_the_tables_a_similar_case_used(store):
    import copy

    import pandas as pd

    catalog = copy.deepcopy(CATALOG)
    catalog["sources"]["tickets"] = {"table": "tickets", "description": "Help-desk tickets.",
                                     "fields": {"ticket_id": {}, "title": {}}}
    duck = _duck()
    duck.register_api_function("tickets", lambda limit=None: pd.DataFrame({"ticket_id": [1], "title": ["x"]}))
    search = SemanticSearch(Catalog.model_validate(catalog), duck, feedback=store, memory=True)
    search.interpreter.top_k = 1  # as with many tables: retrieval only brings the best one
    plain = search.search("Which alerts?")
    assert "tickets" not in [d.subject for d in plain.decisions if d.kind == "source_relevance"]
    search.feedback(plain, "not_answered", ["wrong_tables"], expected={"sources": ["tickets"]})
    again = search.search("Which alerts?")
    assert "tickets" in [d.subject for d in again.decisions if d.kind == "source_relevance"]  # still judged


def test_offline_a_very_similar_case_is_the_default(store):
    search = _search(store, memory=True)
    search.feedback(search.search("Show 10.0.0.1"), "not_answered",
                    expected={"answer_shape": "lookup", "entity": "ip_address"})
    result = search.search("Show 10.0.0.2")
    assert result.status == "ok" and result.intent.answer_shape == "lookup"
    assert result.results["source"].tolist() == ["alerts", "owners"]


def test_measuring_never_records_nor_remembers(store):
    search = _search(store, memory=True)
    search.feedback(search.search("How many alerts per rule?"), "answered")
    before = len(store.searches())
    dataset = store.to_evaluation()
    evaluate(search, dataset, execute=False)
    calibrate_thresholds(search, dataset)
    assert len(store.searches()) == before
    assert search.feedback_store is store and search.interpreter.memory is not None  # restored


# ---------------------------------------------------------------------------
# Suggestions


def _rated(store, question, **expected):
    search = _search(store)
    search.feedback(search.search(question), "not_answered", ["other"], expected=expected or None)


def _suggest(store, **kw):
    return CatalogSuggester(store, Catalog.model_validate(CATALOG), **kw).suggest()


def test_a_synonym_correction(store):
    _rated(store, "Which alerts are urgent?", synonym={"field": "alerts.severity", "value": "critical", "word": "urgent"})
    [s] = [s for s in _suggest(store) if s.kind == "value_synonym"]
    assert (s.target, s.change) == ("alerts.severity=critical", "urgent")


def test_a_table_correction_becomes_an_example_question(store):
    _rated(store, "Who is behind 10.0.0.1?", sources=["owners"])
    [s] = [s for s in _suggest(store) if s.kind == "source_example"]
    assert (s.target, s.change) == ("owners", "Who is behind 10.0.0.1?")


def test_an_answer_kind_correction_becomes_wording(store):
    _rated(store, "Give me a rundown for 10.0.0.1", answer_shape="lookup")
    [s] = [s for s in _suggest(store) if s.kind == "answer_wording"]
    assert (s.target, s.change) == ("lookup", "give me a rundown")


def test_unknown_words_need_support_and_go_where_corrections_point(store):
    _rated(store, "Which gizmos have critical alerts?", entity="ip_address")
    assert not [s for s in _suggest(store) if s.kind == "entity_keyword"]
    _rated(store, "List the gizmos with malware alerts", entity="ip_address")
    [s] = [s for s in _suggest(store) if s.kind == "entity_keyword"]
    assert (s.target, s.change, len(s.evidence)) == ("ip_address", "gizmos", 2)
    _rated(store, "Show quux alerts")
    _rated(store, "Count quux alerts")
    [u] = [s for s in _suggest(store) if s.kind == "unknown_word"]
    assert u.change == "quux" and not u.applicable


def test_reviewed_suggestions_leave_the_list(store):
    _rated(store, "Who is behind 10.0.0.1?", sources=["owners"])
    [s] = _suggest(store)
    store.record_review(s.id, "dismissed")
    assert _suggest(store) == [] and _suggest(store, min_support=2)[0:0] == []
    assert CatalogSuggester(store, Catalog.model_validate(CATALOG)).suggest(include_reviewed=True)[0].status == "dismissed"


def test_applying_writes_the_catalog_and_keeps_a_backup(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(CATALOG))
    what = apply_suggestion(Suggestion("value_synonym", "alerts.severity=critical", "urgent", "r"), str(path), "x")
    assert "urgent" in what and (tmp_path / "catalog.yaml.bak").exists()
    catalog = Catalog.load(str(path))
    assert "urgent" in catalog.sources["alerts"].fields["severity"].values["critical"]
    apply_suggestion(Suggestion("entity_keyword", "ip_address", "gizmo", "r"), str(path), "x")
    apply_suggestion(Suggestion("source_example", "owners", "Who is behind 10.0.0.1?", "r"), str(path), "x")
    apply_suggestion(Suggestion("source_note", "owners", "People call this “cmdb”.", "r"), str(path), "x")
    catalog = Catalog.load(str(path))
    assert "gizmo" in catalog.entities["ip_address"].keywords
    assert catalog.sources["owners"].examples == ["Who is behind 10.0.0.1?"]
    assert "cmdb" in catalog.sources["owners"].notes
    with pytest.raises(ValueError, match="nothing to apply"):
        apply_suggestion(Suggestion("unknown_word", None, "quux", "r"), str(path), "x")


def test_accepted_wording_is_loaded_by_the_config(tmp_path):
    (tmp_path / "catalog.json").write_text(json.dumps(CATALOG))
    cfg_file = tmp_path / "duckduck.json"
    cfg_file.write_text(json.dumps({"services": {}, "semantic": {"catalog_path": "catalog.json",
                                                                  "answer_shapes": {"count": {"wording": ["qtd"]}}}}))
    apply_suggestion(Suggestion("answer_wording", "lookup", "give me a rundown", "r"), "unused",
                     str(tmp_path / "answer_shapes.learned.yaml"))
    search = SemanticSearch.from_config(_duck(), str(cfg_file))
    assert search.shapes.candidates("give me a rundown for 10.0.0.1")[0] == ["lookup"]
    assert search.shapes.candidates("qtd de alertas")[0] == ["count"]  # the config's own wording too


def test_feedback_config(tmp_path):
    (tmp_path / "catalog.json").write_text(json.dumps(CATALOG))
    cfg_file = tmp_path / "duckduck.json"
    cfg_file.write_text(json.dumps({"services": {}, "semantic": {
        "catalog_path": "catalog.json", "feedback": {"enabled": True, "path": "fb.duckdb", "max_cases": 2}}}))
    cfg = SemanticConfig.load(None, str(cfg_file))
    assert cfg.feedback.min_similarity == 0.6 and cfg.feedback.learned_answer_shapes == "answer_shapes.learned.yaml"
    search = SemanticSearch.from_config(_duck(), str(cfg_file))
    assert search.search("How many alerts per rule?").search_id
    assert os.path.exists(tmp_path / "fb.duckdb") and search.interpreter.memory.max_cases == 2
    search.feedback_store.close()

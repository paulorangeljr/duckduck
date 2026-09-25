"""Questions in other languages are read in English inside the LLM extraction call — values protected."""

import datetime as dt
import logging

import pytest

pytest.importorskip("pydantic")

from duckduck.semantic import Catalog, SemanticSearch  # noqa: E402
from duckduck.semantic.decisions import DecisionState  # noqa: E402
from duckduck.semantic.jev import JevClient  # noqa: E402
from duckduck.semantic.llm_extraction import LLMExtractionOutput, LLMExtractor, LLMValue  # noqa: E402

from test_answer_shapes import CATALOG, _duck  # noqa: E402

NOW = dt.datetime(2026, 9, 25, 12, 0)


class Translator:
    """Fake LLM: answers with a fixed English reading (and values), recording what it was sent."""

    def __init__(self, english, values=()):
        self.english, self.values, self.prompts, self.systems = english, list(values), [], []

    def generate(self, system, prompt, output_model):
        self.systems.append(system)
        self.prompts.append(prompt)
        return LLMExtractionOutput(values=[LLMValue(value=v) for v in self.values], english_question=self.english)


def _extractor(llm, **kw):
    return LLMExtractor(Catalog.model_validate(CATALOG), llm, **kw)


def test_values_are_masked_before_the_llm_and_restored_after():
    llm = Translator("Which tables contain the ip ⟦1⟧?")
    ex = _extractor(llm).extract("Em quais tabelas existe o ip 10.0.0.196?", NOW)
    assert "10.0.0.196" not in llm.prompts[0] and "⟦1⟧" in llm.prompts[0]
    assert ex.english_question == "Which tables contain the ip 10.0.0.196?"
    assert [lit.value for lit in ex.literals] == ["10.0.0.196"]
    assert "english_question" in llm.systems[0]


def test_the_rules_run_over_the_english_reading():
    llm = Translator("How many critical alerts per rule?")
    ex = _extractor(llm).extract("Quantos alertas críticos por regra?", NOW)
    assert [(m.field, m.value) for m in ex.enum_matches] == [("alerts.severity", "critical")]


def test_a_translation_that_loses_a_value_is_discarded(caplog):
    caplog.set_level(logging.WARNING, logger="duckduck.semantic.llm")
    lost_placeholder = _extractor(Translator("Which tables contain this IP?")).extract(
        "Em quais tabelas existe o ip 10.0.0.196?", NOW)
    assert lost_placeholder.english_question is None
    respelled = _extractor(Translator("Everything about the finance server", values=["servidor-financeiro"])).extract(
        "tudo sobre servidor-financeiro", NOW)
    assert respelled.english_question is None and [lit.value for lit in respelled.literals] == ["servidor-financeiro"]
    assert "reading the question as asked" in caplog.text


def test_english_stays_as_asked():
    ex = _extractor(Translator("Which hosts have critical alerts?")).extract("Which hosts have critical alerts?", NOW)
    assert ex.english_question is None
    assert _extractor(Translator("")).extract("Which hosts have critical alerts?", NOW).english_question is None


def test_translation_can_be_turned_off():
    llm = Translator("Which tables contain the ip ⟦1⟧?")
    ex = _extractor(llm, translate=False).extract("Em quais tabelas existe o ip 10.0.0.196?", NOW)
    assert ex.english_question is None and "10.0.0.196" in llm.prompts[0]
    assert "english_question" not in llm.systems[0]


def test_the_pipeline_reads_english_and_shows_the_question_as_asked():
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck())
    search.interpreter.extractor = _extractor(Translator("How many alerts per rule?"))
    result = search.search("Quantas alertas temos segundo a regra?")  # not in any default wording
    assert result.status == "ok" and result.intent.english_question == "How many alerts per rule?"
    assert result.intent.question == "Quantas alertas temos segundo a regra?"
    assert result.intent.answer_shape == "count_by" and "GROUP BY" in result.sql


def test_the_decision_engine_gets_both():
    state = DecisionState(query="Which tables contain 10.0.0.1?", original_query="Em quais tabelas existe 10.0.0.1?")
    sent = JevClient._state(state.model_dump())
    assert sent["user_question"] == "Which tables contain 10.0.0.1?"
    assert sent["original_question"] == "Em quais tabelas existe 10.0.0.1?"
    assert "original_question" not in JevClient._state(DecisionState(query="x").model_dump())

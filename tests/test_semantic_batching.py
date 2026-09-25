"""Batched decisions, criteria, notes as context, extra candidates, calibration."""

import json
import logging
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import (  # noqa: E402
    Ask,
    Catalog,
    DecisionState,
    JEVAdapter,
    JevClient,
    LexicalDecisionEngine,
    SemanticSearch,
    ask_all,
    calibrate_thresholds,
    load_dataset,
)
from duckduck.semantic.decisions import CRITERIA  # noqa: E402

from semantic_helpers import CATALOG_PATH, DATASET_PATH, NOW, sample_sources  # noqa: E402


def _state(**kw):
    return DecisionState(query="Which users hit github?", **kw)


MIXED = [
    Ask(key="entity", question="Which entity?", options={"user": "a person", "host": "a machine"}, state=_state()),
    Ask(key="src", question="Relevant?", subject="proxy logs", criteria=CRITERIA["source"], state=_state(prior=0.7)),
]


class FakeBackend:
    def __init__(self):
        self.requests = []

    def ask(self, state, questions):
        self.requests.append((state, questions))
        return {k: ({"user": 0.9, "host": 0.1} if q["type"] == "choice" else 0.8) for k, q in questions.items()}


def test_adapter_sends_a_mixed_batch_as_one_request():
    backend = FakeBackend()
    answers = JEVAdapter(backend).ask_batch(MIXED)
    assert len(backend.requests) == 1
    state, questions = backend.requests[0]
    assert questions["entity"]["type"] == "choice" and questions["entity"]["criteria"] == {"user": "a person", "host": "a machine"}
    assert questions["src"]["type"] == "noul" and questions["src"]["criteria"] == CRITERIA["source"]
    assert "items.src" in questions["src"]["instructions"]
    assert state["items"]["src"] == {"subject": "proxy logs", "catalog_prior_probability": 0.7}
    assert answers["entity"].choice == "user" and answers["src"].probability == pytest.approx(0.8)


def test_the_cache_answers_a_repeated_question_without_a_call():
    backend = FakeBackend()
    adapter = JEVAdapter(backend)
    adapter.ask_batch(MIXED)
    again = adapter.ask_batch(MIXED)
    assert len(backend.requests) == 1 and again["entity"].choice == "user"
    JEVAdapter(backend, cache_size=0).ask_batch(MIXED)
    assert len(backend.requests) == 2


def test_engines_without_batches_get_one_call_per_question():
    answers = ask_all(LexicalDecisionEngine(), MIXED)
    assert set(answers) == {"entity", "src"}


def test_backend_without_ask_falls_back_per_question():
    class Old:
        calls = 0

        def decide(self, state, question, options):
            Old.calls += 1
            return {options[0]: 0.9, options[1]: 0.1}

        def classify(self, state, question, options):
            Old.calls += 1
            return {options[0]: 1.0}

    JEVAdapter(Old()).ask_batch(MIXED)
    assert Old.calls == 2


# ---------------------------------------------------------------------------
# Jev client: request shape, splitting, cost
# ---------------------------------------------------------------------------


def _jev(monkeypatch, answer=None):
    bodies = []

    def fake_post(self, url, data, timeout):
        body = json.loads(data)
        bodies.append(body)
        r = MagicMock(status_code=200, ok=True)
        r.json.return_value = answer(body) if answer else {
            "answers": {k: ({"type": "choice", "probabilities": {c: 1 for c in q["criteria"]}} if q["type"] == "choice"
                            else {"type": "noul", "noul": 0.9}) for k, q in body["questions"].items()},
            "usage": {"input_tokens": 400, "cost": 0.00002},
        }
        return r

    monkeypatch.setattr("requests.Session.post", fake_post)
    return JevClient(api_key="k", url=JevClient.OPENROUTER_DECISIONS_URL, model="typesafe/jev-1.13"), bodies


def test_jev_client_ask_sends_items_and_criteria(monkeypatch):
    client, bodies = _jev(monkeypatch)
    JEVAdapter(client).ask_batch(MIXED)
    body = bodies[0]
    assert body["model"] == "typesafe/jev-1.13" and set(body["questions"]) == {"entity", "src"}
    assert body["questions"]["src"]["criteria"]["true"].startswith("This source holds records")
    assert body["state"]["items"]["src"]["subject"] == "proxy logs" and "terms" not in body["state"]


def test_jev_client_splits_a_batch_over_the_body_cap(monkeypatch):
    client, bodies = _jev(monkeypatch)
    asks = [Ask(key=f"s{i}", question="Relevant?", subject="x" * 3000, state=_state()) for i in range(80)]
    answers = JEVAdapter(client).ask_batch(asks)
    assert len(bodies) > 1 and len(answers) == 80
    assert all(len(json.dumps(b).encode()) <= 32 * 1024 for b in bodies)
    assert sorted(k for b in bodies for k in b["questions"]) == sorted(a.key for a in asks)


def test_jev_client_logs_cost(monkeypatch, caplog):
    client, _ = _jev(monkeypatch)
    caplog.set_level(logging.INFO, logger="duckduck")
    JEVAdapter(client).ask_batch(MIXED)
    assert "2 questions" in caplog.text and "400 tokens in" in caplog.text and "$0.000020" in caplog.text
    assert client.total_cost == pytest.approx(0.00002)


def test_llm_decision_backend_answers_a_mixed_batch():
    from duckduck.semantic.llm_decisions import LLMDecisionBackend, _Answer, _Answers, _Scored

    class Judge:
        def generate(self, system, prompt, output_model):
            body = json.loads(prompt)
            assert output_model is _Answers and body["judgments"]["src"]["criteria"] == CRITERIA["source"]
            return _Answers(answers=[
                _Answer(key="entity", option_probabilities=[_Scored(key="user", probability=0.7), _Scored(key="host", probability=0.3)]),
                _Answer(key="src", yes_probability=0.6),
            ])

    answers = JEVAdapter(LLMDecisionBackend(Judge())).ask_batch(MIXED)
    assert answers["entity"].choice == "user" and answers["src"].probability == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# the pipeline: two requests per question, notes as context, extra candidates
# ---------------------------------------------------------------------------


class CountingJev:
    """Fake Decisions API: 'user'/'authentication' choices, yes to everything; records requests."""

    def __init__(self):
        self.bodies = []

    def __call__(self, url, data, timeout):
        body = json.loads(data)
        self.bodies.append(body)
        answers = {}
        for key, q in body["questions"].items():
            if q["type"] == "choice":
                wanted = "user" if "entity" in q["instructions"] else "authentication"
                answers[key] = {"probabilities": {c: (0.96 if c == wanted else 0.01) for c in q["criteria"]}}
            else:
                subject = body["state"].get("items", {}).get(key, {}).get("subject", "")
                answers[key] = {"noul": 0.97 if "auth_logs" in subject or "source:" not in key else 0.1}
        r = MagicMock(status_code=200, ok=True)
        r.json.return_value = {"answers": answers}
        return r


def _jev_search(monkeypatch, catalog=CATALOG_PATH, **kw):
    fake = CountingJev()
    monkeypatch.setattr("requests.Session.post", lambda self, url, data, timeout: fake(url, data, timeout))
    duck = DuckAPI()
    sample_sources.register_sample_sources(duck, NOW)
    engine = JEVAdapter(JevClient(api_key="k"), cache_size=0)
    return SemanticSearch(catalog, duck, engine=engine, clock=lambda: NOW, **kw), fake


def test_a_question_costs_two_requests(monkeypatch):
    search, fake = _jev_search(monkeypatch)
    result = search.search("Which users generated failed authentication events?")
    assert result.status == "ok", result.clarification
    assert len(fake.bodies) == 2
    first, second = (set(b["questions"]) for b in fake.bodies)
    assert {"entity", "activity"} <= first and any(k.startswith("source:") for k in first)
    assert all(k.startswith(("field:", "join:")) for k in second)


def test_notes_reach_the_decision_engine(monkeypatch):
    data = Catalog.load(CATALOG_PATH).model_dump(by_alias=True)
    data["sources"]["auth_logs"]["notes"] = "Only covers SSO logins."
    field = next(iter(data["sources"]["auth_logs"]["fields"]))
    data["sources"]["auth_logs"]["fields"][field]["notes"] = "Lowercased."
    catalog = Catalog.model_validate(data)
    assert "Owner's notes: Only covers SSO logins." in catalog.describe_source("auth_logs")
    assert "Owner's notes: Lowercased." in catalog.describe_field(f"auth_logs.{field}")
    assert "Only covers SSO logins." in catalog.source_texts("auth_logs")
    search, fake = _jev_search(monkeypatch, catalog=catalog)
    search.search("Which users generated failed authentication events?")
    subjects = json.dumps(fake.bodies[0]["state"]["items"])
    assert "Only covers SSO logins." in subjects


def test_sources_declaring_the_decided_entity_get_a_second_look(monkeypatch):
    search, fake = _jev_search(monkeypatch)
    search.interpreter.top_k = 1  # retrieval proposes a single source
    search.search("Which users generated failed authentication events?")
    first_sources = {k for k in fake.bodies[0]["questions"] if k.startswith("source:")}
    second_sources = {k for k in fake.bodies[1]["questions"] if k.startswith("source:")}
    assert len(first_sources) == 1 and second_sources and not (first_sources & second_sources)
    declaring = {f"source:{n}" for n, s in search.catalog.sources.items() if "user" in s.entities}
    assert second_sources <= declaring


def test_a_clarification_keeps_every_decision_made_before_it(monkeypatch):
    duck = DuckAPI()
    sample_sources.register_sample_sources(duck, NOW)
    search = SemanticSearch(CATALOG_PATH, duck, clock=lambda: NOW)
    search.thresholds.source = 0.999
    result = search.search("Which users accessed github in the last 24hrs?")
    assert result.status == "needs_clarification"
    kinds = [d.kind for d in result.decisions]
    assert kinds[:2] == ["entity", "activity"] and "source_relevance" in kinds


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------


def _local_search():
    duck = DuckAPI()
    sample_sources.register_sample_sources(duck, NOW)
    return SemanticSearch(CATALOG_PATH, duck, clock=lambda: NOW)


def test_calibration_suggests_thresholds_and_restores_the_current_ones():
    search = _local_search()
    before = search.thresholds.model_dump()
    report = calibrate_thresholds(search, load_dataset(DATASET_PATH))
    assert search.thresholds.model_dump() == before
    kinds = {s.kind for s in report.suggestions}
    assert kinds == {"entity", "activity", "source"}
    for s in report.suggestions:
        assert s.cost_suggested <= s.cost_current and s.samples > 0
    assert '"thresholds": {' in report.summary()


def test_calibration_moves_a_threshold_when_it_pays_off():
    search = _local_search()
    cases = load_dataset(DATASET_PATH)
    report = calibrate_thresholds(search, cases, cost_wrong=1000.0, cost_ask=0.001)
    source = next(s for s in report.suggestions if s.kind == "source")
    assert source.accepted_wrong == 0  # a wrong answer is so expensive that none is accepted


def test_calibrate_cli(capsys):
    from duckduck.semantic.__main__ import main

    from semantic_helpers import EXAMPLES

    assert main(["--config", f"{EXAMPLES}/duckduck.local.json", "calibrate", DATASET_PATH]) == 0
    assert "calibrated on 6 labeled questions" in capsys.readouterr().out

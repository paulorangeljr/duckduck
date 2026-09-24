import time

import pytest

from duckduck.semantic import (
    DecisionEngineError,
    DecisionState,
    JEVAdapter,
    LexicalDecisionEngine,
)
from duckduck.semantic.decisions import normalize_probabilities


def test_normalize_probabilities_rescales_and_matches_case_insensitively():
    probs = normalize_probabilities({"yes": 3, "NO": 1}, ["YES", "NO"])
    assert probs == {"YES": 0.75, "NO": 0.25}


def test_normalize_probabilities_clamps_garbage_and_falls_back_to_uniform():
    assert normalize_probabilities({"a": -1, "b": "nan?", "zzz": 5}, ["a", "b"]) == {"a": 0.5, "b": 0.5}
    assert normalize_probabilities({"a": float("nan"), "b": 1}, ["a", "b"]) == {"a": 0.0, "b": 1.0}


class FakeJEV:
    def __init__(self, decide=None, classify=None, fail_times=0, delay=0.0):
        self._decide, self._classify = decide, classify
        self.fail_times, self.delay = fail_times, delay
        self.calls = []

    def _maybe_fail(self):
        if self.delay:
            time.sleep(self.delay)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("jev down")

    def decide(self, state, question, options):
        self.calls.append(("decide", state, question, options))
        self._maybe_fail()
        return self._decide

    def classify(self, state, question, options):
        self.calls.append(("classify", state, question, options))
        self._maybe_fail()
        return self._classify


def test_jev_adapter_decide_sends_state_and_normalizes():
    backend = FakeJEV(decide={"YES": 9, "NO": 1})
    jev = JEVAdapter(backend, backoff=0)
    result = jev.decide(DecisionState(query="q", terms=["a"]), "Is it relevant?", "proxy_logs: ...")
    assert result.answer is True and result.probability == pytest.approx(0.9)
    _, state, question, options = backend.calls[0]
    assert state["query"] == "q" and state["subject"] == "proxy_logs: ..."
    assert options == ["YES", "NO"]


def test_jev_adapter_classify_passes_option_descriptions():
    backend = FakeJEV(classify={"user": 0.7, "host": 0.3})
    jev = JEVAdapter(backend, backoff=0)
    result = jev.classify(DecisionState(query="q"), "Which entity?", {"user": "a person", "host": "a machine"})
    assert result.choice == "user" and result.probability == pytest.approx(0.7)
    assert backend.calls[0][1]["options"] == {"user": "a person", "host": "a machine"}


def test_jev_adapter_retries_then_succeeds():
    backend = FakeJEV(decide={"YES": 1, "NO": 0}, fail_times=2)
    result = JEVAdapter(backend, retries=2, backoff=0).decide(DecisionState(query="q"), "?", "s")
    assert result.answer is True
    assert len(backend.calls) == 3


def test_jev_adapter_gives_up_after_retries():
    backend = FakeJEV(decide={"YES": 1}, fail_times=10)
    with pytest.raises(DecisionEngineError, match="after 2 attempts"):
        JEVAdapter(backend, retries=1, backoff=0).decide(DecisionState(query="q"), "?", "s")


def test_jev_adapter_times_out():
    backend = FakeJEV(decide={"YES": 1}, delay=0.5)
    with pytest.raises(DecisionEngineError):
        JEVAdapter(backend, retries=0, timeout=0.05, backoff=0).decide(DecisionState(query="q"), "?", "s")


def test_lexical_decide_uses_prior_when_given():
    engine = LexicalDecisionEngine()
    assert engine.decide(DecisionState(query="q", prior=0.93), "?", "whatever").probability == 0.93


def test_lexical_decide_is_term_coverage():
    engine = LexicalDecisionEngine()
    full = engine.decide(DecisionState(query="q", terms=["user", "access"]), "?", "users access the web")
    none = engine.decide(DecisionState(query="q", terms=["user", "access"]), "?", "dns lookups")
    assert full.probability == pytest.approx(0.95) and full.answer
    assert none.probability == pytest.approx(0.05) and not none.answer


def test_lexical_classify_one_clear_hit_is_confident_and_no_hit_is_uniform():
    engine = LexicalDecisionEngine()
    options = {"user": "a person", "host": "a machine", "domain": "a website"}
    hit = engine.classify(DecisionState(query="q", terms=["machin"]), "?", options)
    assert hit.choice == "host" and hit.probability > 0.8
    miss = engine.classify(DecisionState(query="q", terms=["banana"]), "?", options)
    assert miss.probability == pytest.approx(1 / 3)

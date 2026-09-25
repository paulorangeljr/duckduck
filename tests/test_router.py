"""🧭 Auto: the mode for a question picked from how similar questions went in each mode — by the decision engine."""

import pytest

pytest.importorskip("pydantic")

from duckduck.semantic.decisions import Classification  # noqa: E402
from duckduck.semantic.evaluation import evaluate  # noqa: E402
from duckduck.semantic.feedback import FeedbackStore  # noqa: E402
from duckduck.semantic.router import ModeRouter  # noqa: E402

from test_modes import _search  # noqa: E402

Q = "how many departments are there?"


def _history(store, search, runs):
    """``runs``: (reader, verdict or None) — each a search of ``Q``, rated as given."""
    for reader, verdict in runs:
        result = search.search(Q, reader=reader)
        if verdict:
            store.record_feedback(result.search_id, verdict)


class Engine:
    """Fake decision engine for the route question: fixed probabilities, the state it was asked with kept."""

    def __init__(self, probabilities=None, error=None):
        self.probabilities, self.error, self.states = probabilities, error, []

    def classify(self, state, question, options):
        self.states.append(state)
        if self.error:
            raise self.error
        return Classification(question=question, probabilities=self.probabilities)


def test_a_run_is_a_success_unless_rated_negatively():
    store = FeedbackStore()
    search, _ = _search(store)
    _history(store, search, [("rules", "not_answered"), ("rules", "partial"), ("llm", "answered"), ("llm", None)])
    runs = store.runs()
    assert sorted((r["reader"], r["success"], r["outcome"]) for r in runs) == [
        ("llm", 1.0, "answered"), ("llm", 1.0, "no complaint"),
        ("rules", 0.0, "not answered"), ("rules", 0.5, "partly answered")]


def test_a_conversation_is_one_run_its_outcome_the_last_rounds():
    store = FeedbackStore()
    search, _ = _search(store)
    first = search.search(Q, reader="llm", conversation_id="c1")
    store.record_feedback(first.search_id, "not_answered")
    last = search.search(Q, reader="rules", conversation_id="c1")
    store.record_feedback(last.search_id, "answered")
    (run,) = store.runs()
    assert (run["reader"], run["rounds"], run["success"]) == ("llm", 2, 1.0)  # the mode that started it


def test_evidence_is_weighted_and_smoothed():
    similar = [{"reader": "rules", "similarity": 1.0, "success": 0.0, "elapsed_ms": 10, "engine_calls": 1,
                "llm_calls": 0, "asked_back": False, "outcome": "not answered", "question": "q"},
               {"reader": "llm", "similarity": 0.5, "success": 1.0, "elapsed_ms": 30, "engine_calls": 1,
                "llm_calls": 1, "asked_back": True, "outcome": "answered", "question": "q"}]
    ev = ModeRouter.evidence(similar, ["rules", "llm", "llm_decides"])
    assert ev["rules"]["success_rate"] == round(1 / 3, 3)
    assert ev["llm"]["success_rate"] == 0.6 and ev["llm"]["asked_back_rate"] == 1.0
    assert ev["llm_decides"]["similar_runs"] == 0 and ev["llm_decides"]["success_rate"] == 0.5  # no evidence


def test_auto_follows_the_history():
    store = FeedbackStore()
    search, _ = _search(store)
    _history(store, search, [("rules", "not_answered"), ("rules", "not_answered"), ("llm", "answered"),
                             ("llm", None)])
    result = search.search(Q, reader="auto")
    assert (result.reader, result.requested_reader, result.status) == ("llm", "auto", "ok")
    record = result.decisions[0]
    assert record.kind == "route" and record.answer == "llm" and "llm: 2/2 similar ok" in record.subject
    assert result.route["reader"] == "llm" and result.route["similar"]
    assert store.searches().iloc[0]["requested_reader"] == "auto"
    (auto,) = store.stats()["auto"].to_dict(orient="records")
    assert (auto["reader"], auto["searches"], auto["rated"]) == ("llm", 1, 0)


def test_with_no_similar_history_the_fallback_reads_it():
    store = FeedbackStore()
    search, _ = _search(store)
    _history(store, search, [("llm", "answered")])
    result = search.search("show me the owners table", reader="auto")
    assert result.reader == "rules" and result.decisions[0].subject.startswith("no similar questions yet")


def test_the_engine_decides_with_the_history_and_a_doubt_falls_back():
    store = FeedbackStore()
    search, _ = _search(store)
    _history(store, search, [("llm", "answered")])
    router = ModeRouter(store, threshold=0.5)
    engine = Engine({"rules": 0.3, "llm": 0.4, "llm_decides": 0.3})
    route = router.route(engine, Q, Q, ["rules", "llm", "llm_decides"])
    assert route.reader == "rules" and "not sure" in route.record.subject
    facts = engine.states[0].facts
    assert facts["history_by_mode"]["llm"]["similar_runs"] == 1 and "success" in facts["note"]
    sure = router.route(Engine({"rules": 0.1, "llm": 0.1, "llm_decides": 0.8}), Q, Q, ["rules", "llm", "llm_decides"])
    assert sure.reader == "llm_decides" and sure.record.passed


def test_a_routing_failure_never_costs_the_answer():
    route = ModeRouter(fallback="llm").route(Engine(error=RuntimeError("down")), Q, Q, ["rules", "llm"])
    assert route.reader == "llm" and "routing failed" in route.record.subject
    only = ModeRouter().route(Engine(error=RuntimeError("never asked")), Q, Q, ["rules"])
    assert only.reader == "rules" and only.record.decided_by == "deterministic"


def test_a_conversation_keeps_the_mode_auto_picked():
    store = FeedbackStore()
    search, _ = _search(store)
    _history(store, search, [("rules", "not_answered"), ("llm", "answered")])
    conversation = search.conversation(Q, reader="auto")
    assert conversation.reader == "llm" and conversation.requested_reader == "auto"


def test_evaluating_doesnt_read_the_history_it_measures():
    store = FeedbackStore()
    search, _ = _search(store)
    _history(store, search, [("rules", "not_answered"), ("llm", "answered")])
    seen = []
    runs = store.runs
    store.runs = lambda: seen.append(1) or runs()
    search.router._version = None
    evaluate(search, [{"question": Q, "expected_answer_shape": "count_values"}], reader="auto")
    assert not seen and search.router.store is store


def test_the_web_app():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app

    store = FeedbackStore()
    search, _ = _search(store)
    _history(store, search, [("rules", "not_answered"), ("llm", "answered")])
    client = TestClient(create_app(lambda: search, store=store))
    assert "auto" in client.get("/api/meta").json()["readers"]["available"]
    preview = client.post("/api/preview", json={"question": Q, "reader": "auto"}).json()
    assert preview["route"]["reader"] == "llm" and "similar ok" in preview["route"]["why"]
    result = client.post("/api/ask", json={"question": Q, "reader": "auto"}).json()["result"]
    assert (result["reader"], result["requested_reader"]) == ("llm", "auto") and result["route"]["evidence"]
    assert client.get("/api/stats").json()["auto"][0]["reader"] == "llm"


def test_the_chip_is_the_mode_that_answers():
    # the preview picks a mode; the history then changes; the search still uses the preview's pick
    store = FeedbackStore()
    search, _ = _search(store)
    _history(store, search, [("rules", "not_answered"), ("llm", "answered")])
    assert search.preview(Q, reader="auto")["route"]["reader"] == "llm"
    _history(store, search, [("llm", "not_answered")] * 3 + [("rules", "answered")] * 3)
    result = search.search(Q, reader="auto")
    assert result.reader == "llm" and result.decisions[0].kind == "route"
    assert search.preview(Q, reader="auto")["route"]["reader"] == "llm"  # the cached preview agrees
    search.route_ttl = 0  # the pick expired: preview and search read the history again, and agree
    assert search.preview(Q, reader="auto")["route"]["reader"] == "rules"
    search.route_ttl = 300
    assert search.search(Q, reader="auto").reader == "rules"


def test_a_tie_goes_to_the_cheaper_mode():
    router = ModeRouter(threshold=0.3, tie_margin=0.05)
    modes = ["rules", "llm", "llm_decides"]
    route = router.route(Engine({"rules": 0.33, "llm": 0.35, "llm_decides": 0.32}), Q, Q, modes)
    assert route.reader == "rules" and "the cheaper" in route.record.subject and route.record.probability == 0.33
    route = router.route(Engine({"rules": 0.1, "llm": 0.44, "llm_decides": 0.46}), Q, Q, modes)
    assert route.reader == "llm"  # Dive and Fly tied: Dive is cheaper
    route = router.route(Engine({"rules": 0.1, "llm": 0.3, "llm_decides": 0.6}), Q, Q, modes)
    assert route.reader == "llm_decides"  # clearly better: no tie
    evidence = {"rules": {"success_rate": 0.5}, "llm": {"success_rate": 0.75}, "llm_decides": {"success_rate": 0.77}}
    assert router.cheapest_of_tied({m: e["success_rate"] for m, e in evidence.items()}, modes)[0] == "llm"

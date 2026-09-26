"""Hypotheses: before asking the user, every reading is planned in parallel and the engine judges the plans."""

import json
import threading

import pytest

pytest.importorskip("pydantic")

from duckduck.semantic import Catalog, JEVAdapter, JevClient, SemanticSearch  # noqa: E402
from duckduck.semantic.hypotheses import HypothesisJudge, describe  # noqa: E402

from test_answer_shapes import CATALOG, Jev, _duck  # noqa: E402

QUESTION = "How many alerts by severity?"  # a count, or a count per severity? the wording allows both


class Judge(Jev):
    """Unsure about the answer kind (as a real engine can be); then judges whole plans: ``prefer`` in the text wins."""

    def __init__(self, prefer, sure=0.85, yes=0.9):
        super().__init__(shape={"count": 0.5, "count_by": 0.5})
        self.prefer, self.sure, self.yes, self.threads = prefer, sure, yes, set()

    def __call__(self, url, data, timeout):
        self.threads.add(threading.get_ident())
        response = super().__call__(url, data, timeout)
        body = json.loads(data)
        answers = response.json.return_value["answers"]
        for key, q in body["questions"].items():
            if key == "reading":
                picks = {o: (self.sure if self.prefer in text else 0.0) for o, text in q["criteria"].items()}
                rest = (1 - self.sure) / max(1, len(picks) - 1)
                answers[key] = {"probabilities": {o: (p or rest) for o, p in picks.items()}}
            elif key.startswith("check:"):
                answers[key] = {"noul": self.yes}
        return response


def _search(monkeypatch, judge, **kw):
    monkeypatch.setattr("requests.Session.post", lambda self, url, data, timeout: judge(url, data, timeout))
    return SemanticSearch(Catalog.model_validate(CATALOG), _duck(),
                          engine=JEVAdapter(JevClient(api_key="k"), cache_size=0), **kw)


def test_without_hypotheses_the_doubt_is_asked_back(monkeypatch):
    search = _search(monkeypatch, Judge("count for each"), hypotheses=False)
    result = search.search(QUESTION)
    assert result.status == "needs_clarification" and result.followup.kind == "answer_shape"


def test_the_engine_judges_the_planned_readings_and_the_best_one_runs(monkeypatch):
    judge = Judge("A count for each value of")
    search = _search(monkeypatch, judge)
    result = search.search(QUESTION)
    assert result.status == "ok", result.report()
    assert result.intent.answer_shape == "count_by" and set(result.results.columns) == {"severity", "count"}
    reading = result.decisions[0]
    assert reading.kind == "reading" and reading.decided_by == "engine" and reading.probability == 0.85
    assert [a for a, _ in reading.alternatives] and "planned in parallel" in reading.subject
    shape = next(d for d in result.decisions if d.kind == "answer_shape")
    assert (shape.answer, shape.decided_by) == ("count_by", "hypothesis")  # the engine's pick, not the user's
    assert len(result.hypotheses) == 2 and all(h["sql"] for h in result.hypotheses)
    assert result.hypotheses[0]["description"].startswith("A count for each value of alerts.severity")
    assert result.pinned == {}  # nothing the user said


def test_readings_are_planned_in_parallel_and_judged_in_one_batch(monkeypatch):
    judge = Judge("A count for each value of")
    search = _search(monkeypatch, judge)
    judge.bodies.clear()
    search.search(QUESTION)
    judged = [b for b in judge.bodies if "reading" in b["questions"]]
    assert len(judged) == 1 and {k for k in judged[0]["questions"] if k.startswith("check:")} == {"check:h1", "check:h2"}


def test_the_readings_really_run_at_the_same_time():
    from types import SimpleNamespace

    from duckduck.semantic.intent import ClarificationOption

    barrier = threading.Barrier(2, timeout=5)  # both readings must be planning at once to get past it

    def run(pins):
        barrier.wait()
        return SimpleNamespace(status="planned", pins=pins, sql="SELECT 1")

    first = SimpleNamespace(followup=SimpleNamespace(options=[
        ClarificationOption(value="a", label="A", pins={"answer_shape": "count"}),
        ClarificationOption(value="b", label="B", pins={"answer_shape": "count_by"})]))
    readings = HypothesisJudge().readings(run, {}, first)
    assert [h.pins for h in readings] == [{"answer_shape": "count"}, {"answer_shape": "count_by"}]


def test_no_sure_reading_asks_the_user_as_before(monkeypatch):
    search = _search(monkeypatch, Judge("A count for each value of", sure=0.55))
    result = search.search(QUESTION)
    assert result.status == "needs_clarification" and result.followup.kind == "answer_shape"
    assert result.decisions[-1].kind == "reading" and not result.decisions[-1].passed
    assert len(result.hypotheses) == 2  # what was considered, for the page and the audit
    no = _search(monkeypatch, Judge("A count for each value of", yes=0.2)).search(QUESTION)
    assert no.status == "needs_clarification"  # preferred, but the engine doesn't think it answers the question


def test_the_offline_engine_never_judges_plans():
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck())
    assert not search.hypotheses.active(search.engine)
    assert HypothesisJudge(enabled=False).active(object()) is False


def test_a_plan_in_plain_words():
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck())
    planned = search.plan("Which hosts have critical alerts?")
    text = describe(planned, search.catalog)
    assert text.startswith("List ") and "from alerts" in text and "where alerts.severity eq 'critical'" in text

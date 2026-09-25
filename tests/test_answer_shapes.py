"""Which kind of answer — and so which SQL: list, count, distinct values, count of values, count per group."""

import json
from unittest.mock import MagicMock

import pandas as pd
import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, JEVAdapter, JevClient, SemanticSearch  # noqa: E402
from duckduck.semantic.extraction import RuleBasedExtractor  # noqa: E402
from duckduck.semantic.interpreter import SemanticInterpreter  # noqa: E402
from duckduck.semantic.plan import LogicalQueryPlan  # noqa: E402

ALERTS = pd.DataFrame({
    "ip": ["10.0.0.1", "10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.3", "10.0.0.3"],
    "rule": ["brute_force", "brute_force", "malware", "brute_force", "malware", "exfil"],
    "severity": ["low", "critical", "critical", "high", "critical", "low"],
})
OWNERS = pd.DataFrame({"ip": ["10.0.0.1", "10.0.0.2", "10.0.0.3"], "owner": ["ana", "bob", "ana"],
                       "department": ["eng", "eng", "ops"]})

CATALOG = {
    "sources": {
        "alerts": {"table": "alerts", "description": "Security alerts raised about IP addresses.",
                   "entities": ["ip_address", "event"], "activities": ["security_alert"],
                   "fields": {"ip": {"semantic_type": "ip_address", "description": "IP address the alert is about"},
                              "rule": {"description": "Detection rule",
                                       "values": {"brute_force": ["brute force"], "malware": [], "exfil": []}},
                              "severity": {"description": "Alert severity",
                                           "values": {"low": [], "high": [], "critical": []}}}},
        "owners": {"table": "owners", "description": "Who owns each IP address.", "entities": ["ip_address", "user"],
                   "fields": {"ip": {"semantic_type": "ip_address"}, "owner": {"semantic_type": "user"},
                              "department": {"description": "Owner's department"}}},
    },
    "entities": {"ip_address": {"description": "An IP address", "keywords": ["ip", "ips", "host", "hosts"]},
                 "user": {"description": "A person", "keywords": ["owner", "owners", "users"]},
                 "event": {"description": "Individual records", "keywords": ["alerts", "records"], "row_level": True}},
    "activities": {"security_alert": {"description": "An alert", "keywords": ["alert", "alerts"]}},
    "relationships": [{"from": "alerts.ip", "to": "owners.ip", "type": "same_entity", "confidence": 0.95}],
}


def _duck():
    duck = DuckAPI()
    duck.register_api_function("alerts", lambda limit=None: ALERTS)
    duck.register_api_function("owners", lambda limit=None: OWNERS)
    return duck


def _search(**kw):
    return SemanticSearch(Catalog.model_validate(CATALOG), _duck(), **kw)


@pytest.mark.parametrize("question, candidates", [
    ("Which hosts have critical alerts?", ["list"]),
    ("How many hosts have critical alerts?", ["count"]),
    ("How many alerts by severity?", ["count", "count_by"]),        # "by": maybe a breakdown
    ("How many alerts per rule?", ["count_by"]),
    ("Quantos alertas por cada regra?", ["count_by"]),
    ("How many different severities are there?", ["count_values"]),
    ("Show me the different severities", ["values"]),
    ("ME mostre as diferente severities?", ["values"]),
    ("What types of rule exist?", ["values"]),
    ("Quais os tipos de regra?", ["values"]),
    ("Show alerts for each rule", ["list", "count_by"]),            # a list, or a count per rule?
])
def test_what_the_wording_allows(question, candidates):
    assert SemanticInterpreter._shape_candidates(question)[0] == candidates


def _answer(search, question):
    result = search.search(question)
    assert result.status == "ok", result.report()
    return result


def test_distinct_values_of_the_field_the_question_names():
    result = _answer(_search(), "ME mostre as diferente severities?")
    assert "SELECT DISTINCT \"alerts\".\"severity\"" in result.sql
    assert result.results["severity"].tolist() == ["critical", "high", "low"]
    shape = {d.kind: d for d in result.decisions}
    assert shape["answer_shape"].answer == "values" and shape["values_field"].answer == "alerts.severity"


def test_how_many_different_values():
    assert _answer(_search(), "How many different severities are there?").results["count"].tolist() == [3]


def test_count_per_group_of_records():
    result = _answer(_search(), "How many alerts per rule?")
    assert "GROUP BY" in result.sql
    assert result.results.to_dict("records") == [{"rule": "brute_force", "count": 3}, {"rule": "malware", "count": 2},
                                                 {"rule": "exfil", "count": 1}]


def test_count_per_group_of_distinct_things():
    result = _answer(_search(), "How many hosts per rule have critical alerts?")
    # critical: .1 brute_force, .2 malware, .3 malware → malware has 2 distinct hosts
    assert result.results.to_dict("records") == [{"rule": "malware", "count": 2}, {"rule": "brute_force", "count": 1}]


def test_shape_words_are_never_filter_values():
    extraction = RuleBasedExtractor(Catalog.model_validate(CATALOG)).extract(
        "How many hosts per rule have different severities?", pd.Timestamp("2026-09-25", tz="UTC").to_pydatetime())
    assert not [lit.value for lit in extraction.literals if lit.kind == "term"]


def test_a_plain_list_is_unchanged():
    result = _answer(_search(), "Which hosts have critical alerts?")
    assert result.results["ip"].tolist() == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert not [d for d in result.decisions if d.kind in ("answer_shape", "values_field")]


def test_group_by_needs_an_aggregate():
    with pytest.raises(ValueError, match="group_by needs an aggregate"):
        LogicalQueryPlan(select=["alerts.rule"], sources=["alerts"], group_by=["alerts.rule"])


# ---------------------------------------------------------------------------
# Ambiguity: the engine decides, a doubt is asked back


class Jev:
    """Fake Decisions API: answers the shape and field questions as told; confident elsewhere."""

    def __init__(self, shape=None, field=None):
        self.shape, self.field, self.bodies = shape or {}, field or {}, []

    def __call__(self, url, data, timeout):
        body = json.loads(data)
        self.bodies.append(body)
        answers = {}
        for key, q in body["questions"].items():
            if q["type"] == "noul":
                answers[key] = {"noul": 0.95}
                continue
            opts = list(q["criteria"])
            if key == "answer_shape":
                probs = {o: self.shape.get(o, 0.0) for o in opts}
            elif "field" in q["instructions"].lower() and self.field:
                probs = {o: self.field.get(o, 0.01) for o in opts}
            else:
                wanted = {"event", "security_alert"}
                probs = {o: (0.9 if o in wanted else 0.01) for o in opts}
            answers[key] = {"probabilities": probs}
        r = MagicMock(status_code=200, ok=True)
        r.json.return_value = {"answers": answers}
        return r


def _jev_search(monkeypatch, **fake):
    jev = Jev(**fake)
    monkeypatch.setattr("requests.Session.post", lambda self, url, data, timeout: jev(url, data, timeout))
    return _search(engine=JEVAdapter(JevClient(api_key="k"), cache_size=0)), jev


def test_ambiguous_wording_is_decided_by_the_engine_in_the_same_batch(monkeypatch):
    search, jev = _jev_search(monkeypatch, shape={"count": 0.1, "count_by": 0.9})
    result = _answer(search, "How many alerts by severity?")
    assert "answer_shape" in jev.bodies[0]["questions"]  # with entity/activity/sources: one request
    assert set(jev.bodies[0]["questions"]["answer_shape"]["criteria"]) == {"count", "count_by"}
    assert result.results.to_dict("records") == [{"severity": "critical", "count": 3}, {"severity": "low", "count": 2},
                                                 {"severity": "high", "count": 1}]


def test_a_doubt_about_the_shape_is_asked_back(monkeypatch):
    search, _ = _jev_search(monkeypatch, shape={"count": 0.45, "count_by": 0.55})
    result = search.search("How many alerts by severity?")
    assert result.status == "needs_clarification" and result.followup.kind == "answer_shape"
    assert [o.value for o in result.followup.options] == ["count_by", "count"]
    assert "a count for each value of something" in result.clarification_question
    again = search.search("How many alerts by severity?", pinned=result.followup.options[1].pins)
    assert again.status == "ok" and again.results["count"].tolist() == [6]


def test_a_field_the_question_does_not_name_is_picked_by_the_engine(monkeypatch):
    search, _ = _jev_search(monkeypatch, field={"alerts.rule": 0.95})
    result = _answer(search, "Show me the different detections")
    assert result.results["rule"].tolist() == ["brute_force", "exfil", "malware"]


def test_a_doubt_about_the_field_is_asked_back_and_none_means_a_plain_list(monkeypatch):
    search, _ = _jev_search(monkeypatch, field={"alerts.rule": 0.5, "alerts.severity": 0.45})
    conversation = search.conversation("Show me the different detections")
    followup = conversation.result.followup
    assert followup.kind == "values_field" and followup.question == "The different values of what?"
    assert [o.value for o in followup.options][:2] == ["alerts.rule", "alerts.severity"]
    assert conversation.answer("2").results["severity"].tolist() == ["critical", "high", "low"]

    conversation = search.conversation("Show me the different detections")
    assert conversation.answer("none of these").status == "ok"  # answered as a list of alerts
    assert len(conversation.result.results) == len(ALERTS)

"""Choosing the systems/tables a question may use — the preview while typing, and the restriction it sets."""

import json

import pytest

pytest.importorskip("pydantic")

from duckduck.semantic import Catalog, SemanticSearch  # noqa: E402
from duckduck.semantic import scope  # noqa: E402

from test_answer_shapes import CATALOG, _duck, _jev_search  # noqa: E402


def _search(**kw):
    duck = _duck()
    duck.service_of.update({"alerts": "siem", "owners": "cmdb"})  # two systems, as auto_register records them
    return SemanticSearch(Catalog.model_validate(CATALOG), duck, **kw)


# ---------------------------------------------------------------------------
# The restriction


def test_only_the_chosen_tables_are_used_joins_included():
    import os

    from duckduck.semantic import connect

    config = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "examples", "semantic", "duckduck.local.json")
    search = SemanticSearch.from_config(connect(config), config)
    question = "Which machines communicated with 203.0.113.9?"
    free = search.search(question)
    assert free.status == "ok" and set(free.query_plan.sources) == {"firewall_logs", "asset_inventory"}
    narrowed = search.search(question, only_sources=["firewall_logs"])  # hostnames live in asset_inventory
    assert narrowed.status != "ok" or "asset_inventory" not in narrowed.query_plan.sources
    assert narrowed.only_sources == ["firewall_logs"] and narrowed.decisions[0].kind == "scope"
    offered = json.dumps([o.model_dump() for o in narrowed.followup.options]) if narrowed.followup else ""
    assert "asset_inventory" not in offered


def test_the_restriction_holds_for_lookup_locate_and_catalog_answers():
    search = _search()
    assert search.search("Which tables contain 10.0.0.1?", only_sources=["owners"]).summary["source"].tolist() == ["owners"]
    assert search.search("Tell me about 10.0.0.1", only_sources=["alerts"]).results["source"].unique().tolist() == ["alerts"]
    assert search.search("What data do you have?", only_sources=["owners"]).results["source"].tolist() == ["owners"]


def test_the_restriction_ends_with_the_search():
    search = _search()
    search.search("How many alerts per rule?", only_sources=["alerts"])
    assert scope.current() is None and search.planner._is_allowed("owners")


def test_bad_choices_fail_clearly():
    search = _search()
    with pytest.raises(ValueError, match="unknown table"):
        search.search("Which hosts have critical alerts?", only_sources=["nope"])
    with pytest.raises(ValueError, match="at least one"):
        search.search("Which hosts have critical alerts?", only_sources=[])


def test_it_never_widens_allowed_sources():
    search = _search(allowed_sources=["alerts"])
    result = search.search("Which tables contain 10.0.0.1?", only_sources=["alerts", "owners"])
    assert result.summary["source"].tolist() == ["alerts"]


def test_a_conversation_keeps_the_choice_and_the_entity_every_round(monkeypatch):
    search, _ = _jev_search(monkeypatch, shape={"count": 0.45, "count_by": 0.55})
    conversation = search.conversation("How many alerts by severity?", only_sources=["alerts"],
                                       pinned={"entity": "event"})
    assert conversation.result.only_sources == ["alerts"] and conversation.pinned == {"entity": "event"}
    second = conversation.answer("1")
    assert second.only_sources == ["alerts"] and second.status == "ok"
    assert [d.decided_by for d in second.decisions if d.kind == "entity"] == ["user"]  # never asked


# ---------------------------------------------------------------------------
# The preview


def test_preview_groups_every_table_by_system_and_marks_the_relevant_ones():
    preview = _search().preview("Which owners work in eng")
    systems = {g["system"]: g for g in preview["systems"]}
    assert set(systems) == {"siem", "cmdb"}
    cmdb = systems["cmdb"]["sources"][0]
    assert cmdb["source"] == "owners" and cmdb["relevant"] and cmdb["probability"] >= 0.8
    assert preview["systems"][0]["system"] == "cmdb"  # relevant systems first
    assert preview["entity"]["choice"] == "user" and preview["entity"]["sure"]
    assert preview["answer_shape"] == {"choice": "list", "probability": 1.0, "sure": True}


def test_preview_suggests_the_joins_the_answer_needs():
    import os

    from duckduck.semantic import connect

    config = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "examples", "semantic", "duckduck.local.json")
    search = SemanticSearch.from_config(connect(config), config)
    preview = search.preview("Which machines communicated with 203.0.113.9?")  # hostnames live in asset_inventory
    assert preview["joins"] == [{"left": "firewall_logs.src_ip", "right": "asset_inventory.ip_address",
                                 "type": "same_entity", "confidence": 0.95, "for": "host"}]
    tables = {s["source"]: s for g in preview["systems"] for s in g["sources"]}
    assert tables["asset_inventory"]["relevant"] and tables["asset_inventory"]["joined"]
    assert not tables["firewall_logs"]["joined"]
    assert search.preview("Which users accessed github in the last 24hrs?")["joins"] == []

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app

    client = TestClient(create_app(lambda: search, store=None))
    body = {"question": "Which machines communicated with 203.0.113.9?"}
    joins = lambda r: [(j["left"], j["right"]) for j in r.json()["result"]["query_plan"]["joins"]]  # noqa: E731
    assert joins(client.post("/api/ask", json=body)) == [("firewall_logs.src_ip", "asset_inventory.ip_address")]
    unticked = client.post("/api/ask", json={**body, "blocked_joins": [["firewall_logs.src_ip",
                                                                        "asset_inventory.ip_address"]]})
    assert joins(unticked) == [("firewall_logs.dst_ip", "asset_inventory.ip_address")]  # routed around


def test_preview_is_one_batch_records_nothing_and_is_cached(monkeypatch):
    from duckduck.semantic.feedback import FeedbackStore

    search, jev = _jev_search(monkeypatch)
    search.feedback_store = store = FeedbackStore()
    first = search.preview("Which hosts have critical alerts")
    assert len(jev.bodies) == 1 and store.searches().empty
    assert search.preview("which  hosts have critical alerts") is first and len(jev.bodies) == 1  # same text: cached


def test_preview_of_a_catalog_question_asks_nothing(monkeypatch):
    search, jev = _jev_search(monkeypatch)
    preview = search.preview("Quais entidades existem no seu catalogo")
    assert preview["answer_shape"]["choice"] == "catalog" and preview["catalog_topic"] == "entities"
    assert not jev.bodies


# ---------------------------------------------------------------------------
# The web app


def test_the_api(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app

    search = _search()
    client = TestClient(create_app(lambda: search, store=None))
    preview = client.post("/api/preview", json={"question": "Which owners work in eng"}).json()
    assert {g["system"] for g in preview["systems"]} == {"siem", "cmdb"}
    assert client.post("/api/preview", json={"question": "ab"}).json()["systems"] == []
    answer = client.post("/api/ask", json={"question": "Which tables contain 10.0.0.1?", "only_sources": ["owners"],
                                           "entity": "ip_address"}).json()
    assert answer["result"]["only_sources"] == ["owners"]
    assert answer["result"]["summary"] == [{"source": "owners", "found": True, "rows": 1, "matched_on": "ip",
                                            "description": "Who owns each IP address."}]
    assert client.post("/api/ask", json={"question": "x y z", "only_sources": ["nope"]}).status_code == 400
    assert client.post("/api/ask", json={"question": "x y z", "entity": "nope"}).status_code == 400
    assert client.post("/api/ask", json={"question": "x y z", "blocked_joins": [["alerts.nope", "owners.ip"]]}
                       ).status_code == 400

    def broken(question, reader=None):
        raise RuntimeError("Jev is down")

    search.preview = broken
    assert client.post("/api/preview", json={"question": "Which owners work in eng"}).json()["error"] == "Jev is down"


# ---------------------------------------------------------------------------
# "Which systems are connected?" — about me, or about the data


def test_a_question_that_may_be_about_me_asks_which_it_is():
    search = _search()
    first = search.conversation("Quais sistemas estão conectados?")
    followup = first.result.followup
    assert followup.kind == "answer_shape" and "about me" in followup.context
    assert [o.value for o in followup.options] == ["catalog", "list"]
    systems = first.answer("1")
    assert systems.intent.catalog_topic == "systems"
    assert systems.results.to_dict("records") == [
        {"system": "cmdb", "kind": "test_answer_shapes", "tables": 1, "described": 1, "examples": "owners"},
        {"system": "siem", "kind": "test_answer_shapes", "tables": 1, "described": 1, "examples": "alerts"}]


def test_with_a_value_it_is_about_the_data():
    result = _search().search("Which systems are connected to 10.0.0.1?")
    assert result.intent.answer_shape == "locate"


def test_a_sure_engine_answers_about_me_directly(monkeypatch):
    from test_answer_shapes import _jev_search

    search, jev = _jev_search(monkeypatch, shape={"catalog": 0.9, "list": 0.1})
    search.duck.service_of.update({"alerts": "siem", "owners": "cmdb"})
    result = search.search("Which systems are connected?")
    assert result.status == "ok" and result.intent.catalog_topic == "systems"
    assert set(result.results["system"]) == {"siem", "cmdb"}


def test_systems_respect_allowed_sources():
    result = _search(allowed_sources=["alerts"]).search("Which systems are connected?",
                                                        pinned={"answer_shape": "catalog"})
    assert result.results["system"].tolist() == ["siem"]

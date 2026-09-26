"""lookup ("everything about X"), locate ("which tables have X"), the trivial-list rule, and configurable wording."""

import json

import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, SemanticConfig, SemanticSearch  # noqa: E402
from duckduck.semantic.shapes import AnswerShapes, load_answer_shapes  # noqa: E402

from test_answer_shapes import CATALOG, _duck  # noqa: E402


def _search(**kw):
    return SemanticSearch(Catalog.model_validate(CATALOG), _duck(), **kw)


def _ok(question, **kw):
    result = _search(**kw).search(question)
    assert result.status == "ok", result.report()
    return result


def test_lookup_brings_every_table_holding_the_value():
    result = _ok("What I can find for this ip 10.0.0.3")
    shape = next(d for d in result.decisions if d.kind == "answer_shape")
    assert shape.answer == "lookup" and shape.decided_by == "deterministic"
    assert result.summary.to_dict("records") == [
        {"source": "alerts", "found": True, "rows": 3, "matched_on": "ip",
         "description": "Security alerts raised about IP addresses."},
        {"source": "owners", "found": True, "rows": 1, "matched_on": "ip", "description": "Who owns each IP address."},
    ]
    sections = {s.source: s for s in result.sections}
    assert sections["alerts"].results["rule"].tolist() == ["brute_force", "malware", "exfil"]
    assert sections["owners"].results.to_dict("records") == [{"ip": "10.0.0.3", "owner": "ana", "department": "ops"}]
    payload = json.loads(result.to_json())
    assert payload["sections"][1]["results"] == [{"ip": "10.0.0.3", "owner": "ana", "department": "ops"}]
    assert "── owners (1 rows, matched on ip)" in result.report()
    # the answer itself: every row found, one set of columns, where it came from first
    rows = result.results
    assert list(rows.columns) == ["source", "ip", "rule", "severity", "owner", "department"]
    assert rows["source"].tolist() == ["alerts", "alerts", "alerts", "owners"]
    assert payload["results"][3] == {"source": "owners", "ip": "10.0.0.3", "rule": None, "severity": None,
                                     "owner": "ana", "department": "ops"}
    assert payload["summary"][0]["source"] == "alerts"


def test_lookup_applies_the_question_s_other_values_where_they_fit():
    result = _ok("Tell me about 10.0.0.3 critical alerts")
    sections = {s.source: s for s in result.sections}
    assert sections["alerts"].results["severity"].tolist() == ["critical"]  # alerts has severity
    assert sections["owners"].rows == 1  # owners doesn't: unaffected


def test_each_value_is_looked_up_on_its_own():
    result = _ok("Tell me about 10.0.0.1 and 10.0.0.2")
    assert result.summary.set_index("source")["matched_on"].to_dict() == {
        "alerts": "ip = 10.0.0.1, ip = 10.0.0.2", "owners": "ip = 10.0.0.1, ip = 10.0.0.2"}
    assert result.summary.set_index("source")["rows"].to_dict() == {"alerts": 3, "owners": 2}


def test_a_value_found_nowhere():
    result = _ok("Everything about 192.168.9.9")
    assert result.summary["found"].tolist() == [False, False] and result.results.empty
    assert all(s.results.empty for s in result.sections)


def test_locate_counts_without_reading_rows():
    result = _ok("Em quais tabelas existe o ip 10.0.0.2?")
    assert [d.answer for d in result.decisions if d.kind == "answer_shape"] == ["locate"]
    assert result.results[["source", "found", "rows"]].to_dict("records") == [
        {"source": "alerts", "found": True, "rows": 1}, {"source": "owners", "found": True, "rows": 1}]
    assert all(s.results is None for s in result.sections) and "COUNT(*)" in result.sql


def test_locate_without_a_value_answers_from_the_catalog():
    duck = _duck()
    calls = []
    duck.fetch = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("no data read"))
    search = SemanticSearch(Catalog.model_validate(CATALOG), duck)
    by_entity = search.search("Which tables have hosts?")
    assert by_entity.results.to_dict("records") == [
        {"source": "alerts", "fields": "ip", "description": "Security alerts raised about IP addresses."},
        {"source": "owners", "fields": "ip", "description": "Who owns each IP address."}]
    assert search.search("Which tables have owners?").results["source"].tolist() == ["owners"]
    assert search.search("Which tables exist?").results["source"].tolist() == ["alerts", "owners"]
    assert not calls


def test_a_list_that_would_only_repeat_the_value_becomes_a_lookup():
    result = _ok("Which hosts are 10.0.0.196?")
    shape = [d for d in result.decisions if d.kind == "answer_shape"][-1]  # after the engine read it as a list
    assert shape.answer == "lookup" and "only repeat '10.0.0.196'" in shape.subject
    # different fields of the same type are a real list: "which IPs connected to 10.0.0.5" (src vs dst)


def test_the_user_can_insist_on_the_list():
    result = _search().search("Which hosts are 10.0.0.2?", pinned={"answer_shape": "list"})
    assert result.status == "ok" and result.results["ip"].tolist() == ["10.0.0.2"] and not result.sections


def test_lookup_without_a_value_is_a_list():
    result = _ok("Tell me about the critical alerts")
    assert not result.sections and len(result.results) == 3


# ---------------------------------------------------------------------------
# Wording: defaults + your own


@pytest.mark.parametrize("question, shape", [
    ("What can I find for 10.0.0.1", "lookup"),
    ("tudo sobre 10.0.0.1", "lookup"),
    ("Investigate 10.0.0.1", "lookup"),
    ("Which tables contain 10.0.0.1?", "locate"),
    ("Where does 10.0.0.1 appear?", "locate"),
    ("Onde aparece o ip 10.0.0.1?", "locate"),
    ("Em que bases tem o ip 10.0.0.1?", "locate"),
])
def test_default_wording(question, shape):
    assert AnswerShapes().candidates(question)[0] == [shape]


def test_your_wording_extends_the_defaults():
    shapes = AnswerShapes({"lookup": {"wording": ["o que rola com", "re:\\bficha d[oa]\\b"]},
                           "count_by": {"maybe_wording": ["segundo"]}})
    assert shapes.candidates("o que rola com 10.0.0.1")[0] == ["lookup"]
    assert shapes.candidates("Ficha do ip 10.0.0.1")[0] == ["lookup"]
    assert shapes.candidates("tudo sobre 10.0.0.1")[0] == ["lookup"]  # defaults kept
    assert shapes.candidates("quantos alertas segundo a regra")[0] == ["count", "count_by"]


def test_replace_and_description():
    shapes = AnswerShapes({"lookup": {"wording": ["dossier"], "replace": True, "description": "A full dossier"}})
    assert shapes.candidates("tudo sobre 10.0.0.1")[0] == ["list"]
    assert shapes.candidates("dossier on 10.0.0.1")[0] == ["lookup"]
    assert shapes.descriptions["lookup"] == "A full dossier"


@pytest.mark.parametrize("bad, message", [
    ({"summary": {"wording": ["x"]}}, "unknown shape 'summary'"),
    ({"lookup": {"wording": ["re:(unclosed"]}}, "bad regex"),
    ({"lookup": {"maybe_wording": ["x"]}}, "only for count_by"),
    ({"lookup": {"words": ["x"]}}, "unknown key"),
])
def test_bad_wording_fails_early(bad, message):
    with pytest.raises(ValueError, match=message):
        AnswerShapes(bad)


def test_your_wording_is_not_taken_as_a_value():
    search = _search(answer_shapes={"lookup": {"wording": ["qual é a ficha de"]}})
    result = search.search("qual é a ficha de 10.0.0.1")
    assert result.status == "ok" and result.intent.answer_shape == "lookup"
    assert [lit.value for lit in result.intent.literals] == ["10.0.0.1"]


def test_config_inline_or_file(tmp_path):
    (tmp_path / "shapes.yaml").write_text("lookup:\n  wording: [o que rola com]\n")
    cfg_file = tmp_path / "duckduck.json"
    cfg_file.write_text(json.dumps({"services": {}, "semantic": {"answer_shapes": "shapes.yaml"}}))
    cfg = SemanticConfig.load(DuckAPI(), str(cfg_file))
    assert AnswerShapes(load_answer_shapes(cfg.answer_shapes, cfg.base_dir)).candidates("o que rola com x")[0] == ["lookup"]

    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(CATALOG))
    cfg_file.write_text(json.dumps({"services": {}, "semantic": {
        "catalog_path": "catalog.json", "answer_shapes": "shapes.yaml"}}))
    search = SemanticSearch.from_config(_duck(), str(cfg_file))
    assert search.search("o que rola com 10.0.0.1").intent.answer_shape == "lookup"

    cfg_file.write_text(json.dumps({"services": {}, "semantic": {"answer_shapes": {"nope": {"wording": ["x"]}}}}))
    with pytest.raises(ValueError, match="unknown shape"):
        SemanticConfig.load(DuckAPI(), str(cfg_file))



# ---------------------------------------------------------------------------
# "Bring me information for this ip ..." — the information, without a detour


def test_information_for_a_value_is_a_lookup_with_no_table_question():
    result = _ok("Bring me information for this ip 10.0.0.2")
    assert result.intent.answer_shape == "lookup"
    assert not [d for d in result.decisions if d.kind == "source_relevance" and d.decided_by == "user"]
    assert result.to_dict()["results"] == [
        {"source": "alerts", "ip": "10.0.0.2", "rule": "malware", "severity": "critical", "owner": None,
         "department": None},
        {"source": "owners", "ip": "10.0.0.2", "rule": None, "severity": None, "owner": "bob", "department": "eng"},
    ]


@pytest.mark.parametrize("question", ["Get me data on 10.0.0.1", "details for 10.0.0.1",
                                      "show me everything related to 10.0.0.1", "info on 10.0.0.1"])
def test_more_lookup_wording(question):
    assert AnswerShapes().candidates(question)[0] == ["lookup"]


def test_a_value_with_no_wording_lets_the_engine_choose_list_or_lookup(monkeypatch):
    from test_answer_shapes import _jev_search

    search, jev = _jev_search(monkeypatch, shape={"list": 0.1, "lookup": 0.9})
    result = search.search("10.0.0.2?")
    assert set(jev.bodies[0]["questions"]["answer_shape"]["criteria"]) == {"list", "lookup"}
    assert "default" not in json.dumps(jev.bodies[0]["state"])  # the lexical fallback hint never reaches Jev
    assert result.status == "ok" and result.intent.answer_shape == "lookup"


def test_offline_the_usual_reading_is_a_list():
    result = _ok("Which IPs have critical alerts from 10.0.0.2?")
    shape = next(d for d in result.decisions if d.kind == "answer_shape")
    assert shape.answer == "list" and shape.probability == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# "What kind of information do you have?" — about the catalog, not the data


@pytest.mark.parametrize("question", [
    "What kind of information do you have access to?", "Que tipo de informações você tem acesso",
    "What data do you have?", "Which tables are available?", "What can I ask?", "Quais dados você tem?",
    "O que você sabe?",
])
def test_catalog_wording(question):
    assert AnswerShapes().candidates(question)[0] == ["catalog"]


@pytest.mark.parametrize("question, shape", [
    ("Which tables contain 10.0.0.1?", "locate"), ("What types of rule exist?", "values"),
    ("What kinds of alerts are critical?", "values"),
])
def test_neighbours_keep_their_shape(question, shape):
    assert AnswerShapes().candidates(question)[0] == [shape]


def test_a_catalog_question_asks_nothing_back_and_reads_no_data():
    duck = _duck()
    duck.fetch = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no data read"))
    search = SemanticSearch(Catalog.model_validate(CATALOG), duck)
    result = search.search("Que tipo de informações você tem acesso")
    assert result.status == "ok"
    # what it's about is the engine's call (offline: the wording's reading), and nothing else is decided
    assert [(d.kind, d.answer, d.decided_by) for d in result.decisions] == [("subject", "assistant", "engine")]
    assert result.results.to_dict("records") == [
        {"source": "alerts", "description": "Security alerts raised about IP addresses.",
         "about": "ip address, security alert", "example_question": ""},
        {"source": "owners", "description": "Who owns each IP address.", "about": "ip address, user",
         "example_question": ""},
    ]


def test_the_catalog_answer_respects_allowed_sources():
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck(), allowed_sources=["alerts"])
    assert search.search("What data do you have?").results["source"].tolist() == ["alerts"]


@pytest.mark.parametrize("question, topic", [
    ("Quais entidades existem no seu catalogo", "entities"), ("Which entities exist in your catalog?", "entities"),
    ("Quais atividades existem?", "activities"), ("What columns does alerts have?", "fields"),
    ("Quais campos tem a tabela owners?", "fields"), ("How are the tables related?", "relationships"),
    ("Como as tabelas se relacionam?", "relationships"), ("What kind of information do you have?", "tables"),
    ("Show me your catalog", "tables"),
])
def test_catalog_topics(question, topic):
    result = _search().search(question)
    assert result.status == "ok" and result.intent.answer_shape == "catalog" and result.intent.catalog_topic == topic


def test_catalog_entities_say_where_each_one_is():
    rows = {r["entity"]: r for r in _ok("Quais entidades existem no seu catalogo").results.to_dict("records")}
    assert rows["ip_address"]["tables"] == "alerts, owners" and rows["ip_address"]["fields"] == "alerts.ip, owners.ip"
    assert rows["user"]["fields"] == "owners.owner" and rows["event"]["records_themselves"]


def test_catalog_fields_of_the_table_named():
    rows = _ok("What columns does alerts have?").results
    assert rows["source"].unique().tolist() == ["alerts"] and rows["field"].tolist() == ["ip", "rule", "severity"]
    assert rows.set_index("field")["known_values"]["severity"] == "low, high, critical"
    assert set(_ok("Quais campos existem?").results["source"]) == {"alerts", "owners"}  # no table named: all


def test_catalog_relationships_and_allowed_sources():
    assert _ok("How are the tables related?").results.to_dict("records") == [
        {"from": "alerts.ip", "to": "owners.ip", "type": "same_entity", "confidence": 0.95}]
    limited = SemanticSearch(Catalog.model_validate(CATALOG), _duck(), allowed_sources=["alerts"])
    assert limited.search("How are the tables related?").results.empty
    assert limited.search("Which entities exist?").results.set_index("entity")["tables"]["user"] == ""

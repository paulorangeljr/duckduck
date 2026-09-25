"""Asking the user back: follow-ups, pinned decisions, the conversation loop."""

import json

import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import SemanticSearch  # noqa: E402

from semantic_helpers import CATALOG_PATH, EXAMPLES, NOW, sample_sources  # noqa: E402

QUESTION = "Which users accessed github in the last 24hrs?"


def _search(**thresholds):
    duck = DuckAPI()
    sample_sources.register_sample_sources(duck, NOW)
    search = SemanticSearch(CATALOG_PATH, duck, clock=lambda: NOW)
    for name, value in thresholds.items():
        setattr(search.thresholds, name, value)
    return search


def test_no_relevant_source_asks_which_data_should_answer():
    result = _search(source=0.999).search(QUESTION)
    fu = result.followup
    assert result.status == "needs_clarification" and fu.kind == "source"
    assert fu.question == "Which of these should answer your question?"
    assert fu.context == f"None of the data I have looked clearly right for “{QUESTION}”."
    assert fu.options[0].value == "proxy_logs"
    assert fu.options[0].label == "HTTP and HTTPS web proxy traffic, one row per request (proxy_logs)"
    assert fu.options[0].pins == {"source:proxy_logs": True}
    assert result.clarification_question.splitlines()[:3] == [fu.question, fu.context, "  1) " + fu.options[0].label]


def test_picking_a_source_by_number_answers_the_question():
    conversation = _search(source=0.999).conversation(QUESTION)
    result = conversation.answer("1")
    assert result.status == "ok" and sorted(result.results["username"]) == ["alice", "bob"]
    pinned = next(d for d in result.decisions if d.subject == "proxy_logs" and d.kind == "source_relevance")
    assert pinned.decided_by == "user" and pinned.probability == 1.0
    assert conversation.pinned == {"source:proxy_logs": True} and conversation.done and conversation.rounds == 1


def test_entity_by_name_and_by_free_text():
    conversation = _search(entity=0.99).conversation(QUESTION)
    assert conversation.result.followup.kind == "entity"
    assert conversation.answer("user").status == "ok"

    conversation = _search(entity=0.99).conversation(QUESTION)
    conversation.answer("banana")  # not an option, and nothing like one
    assert conversation.history[-1]["understood"] is None and not conversation.done
    assert conversation.answer("a person, like an employee account").status == "ok"
    assert conversation.history[-1]["understood"] == "user"


def test_each_round_settles_one_doubt_until_answered():
    conversation = _search(field=0.999).conversation(QUESTION)
    asked = []
    while not conversation.done:
        asked.append(conversation.result.followup.question)
        conversation.answer("yes")
    assert conversation.result.status == "ok" and conversation.rounds == len(asked) == 2
    assert asked[0] == "Should I look for “github” in the requested domain?"
    assert asked[1] == "Should the answer list the authenticated user who made the request?"
    assert all(v is True for v in conversation.pinned.values())


def test_no_to_the_only_field_asks_what_the_value_is_instead():
    conversation = _search(field=0.999).conversation(QUESTION)
    result = conversation.answer("no")  # "github" isn't the requested domain
    fu = result.followup
    assert result.status == "needs_clarification" and fu.kind == "value_type"
    assert fu.question == "What is “github”?"
    assert fu.context.startswith("You said “github” isn't the requested domain")
    assert "domain" not in [o.value for o in fu.options] and fu.options
    assert not conversation.done


def test_stateless_pins_give_the_same_answer():
    """A web app can keep the pins itself: search(question, pinned=option.pins)."""
    search = _search(source=0.999)
    option = search.search(QUESTION).followup.options[0]
    again = search.search(QUESTION, pinned=option.pins)
    assert again.status == "ok" and again.pinned == option.pins


def test_the_result_carries_the_followup_everywhere():
    result = _search(source=0.999).search(QUESTION)
    data = json.loads(result.to_json())
    assert data["followup"]["kind"] == "source" and data["followup"]["options"][0]["pins"] == {"source:proxy_logs": True}
    assert "Which of these should answer your question?" in result.report() and "(proxy_logs)" in result.report()


def test_refusing_a_field_finds_another_way():
    """Not the account on the proxy request? Then the owner of the machine that made it."""
    result = _search().search(QUESTION, pinned={"field:proxy_logs.username": False})
    assert result.status == "ok"
    assert result.query_plan.select == ["asset_inventory.owner"]
    assert [(j.left, j.right) for j in result.query_plan.joins] == [("proxy_logs.source_ip", "asset_inventory.ip_address")]


def test_refusing_joins_tries_the_next_one_then_offers_what_can_be_answered():
    conversation = _search(relationship=0.999).conversation(
        "Which users have machines that communicated with 203.0.113.9?")
    asked = []
    for _ in range(8):
        if conversation.done:
            break
        fu = conversation.result.followup
        asked.append((fu.kind, fu.question))
        conversation.answer("1" if fu.kind in ("source", "entity") else "no")
    joins = [q for k, q in asked if k == "join"]
    assert len(joins) == 2 and joins[0] != joins[1]  # each alternative says which fields it matches
    assert "initiating IP" in joins[0] and "target IP" in joins[1]
    assert asked[-1][0] == "entity" and conversation.result.status == "ok"


def test_refusing_the_reading_of_a_term_offers_to_answer_without_it():
    search = _search(field=0.999)
    conversation = search.conversation("Which users generated failed authentication events?")
    conversation.answer("no")  # "failed" isn't outcome = 'failure'
    fu = conversation.result.followup
    assert fu.kind == "ignore_term" and fu.question == "Should I answer without “failed”, then?"
    conversation.answer("yes")
    ignored = [d for d in conversation.result.decisions if d.kind == "value_filter"]
    assert ignored and ignored[0].answer == "ignored" and ignored[0].decided_by == "user"
    no = search.search("Which users generated failed authentication events?",
                       pinned={"field:auth_logs.outcome": False, "term:failed": False})
    assert no.followup.kind == "unresolvable"


def test_none_of_these_pages_through_sources_then_stops():
    search = _search(source=0.999)
    search.interpreter.top_k = 2
    conversation = search.conversation(QUESTION)
    pages = []
    while not conversation.done:
        pages.append([o.value for o in conversation.result.followup.options])
        conversation.answer("none")
    assert all(p[-1] == "none" for p in pages) and len(pages) >= 2
    shown = [v for p in pages for v in p if v != "none"]
    assert len(shown) == len(set(shown)) == 5  # every source offered once
    assert conversation.result.followup.kind == "unresolvable"


def test_none_of_these_for_the_entity_rules_those_out():
    conversation = _search(entity=0.99).conversation(QUESTION)
    shown = [o.value for o in conversation.result.followup.options if o.value != "none"]
    conversation.answer("none")
    entity = next(d for d in conversation.result.decisions if d.kind == "entity")
    assert entity.answer not in shown  # only what's left: here 'event', i.e. the records themselves


def test_an_unreachable_entity_offers_the_ones_that_can_be_answered():
    import copy

    import pandas as pd

    from duckduck.semantic import Catalog

    from test_semantic_batching import LOCAL_CATALOG

    catalog = copy.deepcopy(LOCAL_CATALOG)
    catalog["relationships"] = []  # nothing connects alerts to owners
    duck = DuckAPI()
    duck.register_api_function("alerts", lambda: pd.DataFrame({"ip": ["10.0.0.1"], "rule": ["brute_force"],
                                                               "severity": ["critical"]}))
    duck.register_api_function("owners", lambda: pd.DataFrame({"ip": ["10.0.0.1"], "owner": ["ana"]}))
    search = SemanticSearch(Catalog.model_validate(catalog), duck)
    result = search.search("Which owners have critical alerts?",
                           pinned={"source:alerts": True, "source:owners": False, "entity": "user"})
    fu = result.followup
    assert fu.kind == "entity" and fu.context.startswith("I can't find a person starting from the security alerts")
    assert [o.value for o in fu.options] == ["ip_address", "none"]
    again = search.search("Which owners have critical alerts?", pinned={**result.pinned, **fu.options[0].pins})
    assert again.status == "ok" and list(again.results.iloc[:, 0]) == ["10.0.0.1"]


def test_max_rounds():
    conversation = _search(field=0.999).conversation(QUESTION, max_rounds=1)
    conversation.answer("yes")
    result = conversation.answer("yes")
    assert "Still unsure after 1 answers" in result.clarification and conversation.done


def test_transcript():
    conversation = _search(source=0.999).conversation(QUESTION)
    conversation.answer("zzz")
    conversation.answer("1")
    assert conversation.transcript.splitlines() == [
        QUESTION,
        "  Q: Which of these should answer your question?",
        "  A: zzz  (not understood)",
        "  Q: Which of these should answer your question?",
        "  A: 1",
    ]


def test_cli_interactive(monkeypatch, capsys, tmp_path):
    from duckduck.semantic import __main__ as cli

    data = json.load(open(f"{EXAMPLES}/duckduck.local.json"))
    data["semantic"]["thresholds"] = {"source": 0.999}
    data["services"]["samples"]["module"] = f"{EXAMPLES}/sample_sources.py"
    data["semantic"]["catalog_path"] = CATALOG_PATH
    cfg = tmp_path / "duckduck.json"
    cfg.write_text(json.dumps(data))
    replies = iter(["1"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    code = cli.main(["--config", str(cfg), "ask", "-i", "Which users accessed github in the last 24hrs?"])
    out = capsys.readouterr().out
    assert "Which of these should answer your question?" in out and "[ok]" in out and code == 0


# ---------------------------------------------------------------------------
# plain-language texts: context, details, overrides
# ---------------------------------------------------------------------------


def test_yes_no_questions_explain_themselves():
    fu = _search(field=0.999).search("Which users generated failed authentication events?").followup
    assert fu.question == "By “failed”, do you mean records whose result of the sign-in attempt is “failure”?"
    assert fu.context == ("In the identity provider sign-in logs, the result of the sign-in attempt "
                          "(auth_logs.outcome) has a value “failure” that looks like what you wrote.")
    assert [(o.label, o.detail) for o in fu.options] == [("yes", "only those records"),
                                                         ("no", "“failed” means something else")]
    assert fu.render().splitlines()[2:] == ["  1) yes — only those records", "  2) no — “failed” means something else"]


def test_join_questions_in_plain_words():
    conversation = _search(relationship=0.999).conversation(
        "Which users have machines that communicated with 203.0.113.9?")
    while conversation.result.followup.kind == "source":
        conversation.answer("1")
    fu = conversation.result.followup
    assert fu.kind == "join" and fu.question == (
        "Should I match the initiating IP in the perimeter and internal firewall connection logs "
        "with the current IP of the asset in the corporate asset inventory (CMDB)?")
    assert fu.context.startswith("That's how the two can be combined to answer")


PT = {
    "yes": "sim", "no": "não",
    "source.question": "Qual destes dados deve responder à pergunta?",
    "source.context": "Nenhum dos dados pareceu claramente certo para “{question}”.",
    "field_filter.question": "Devo procurar “{value}” em {field}?",
    "field_filter.yes": "sim, procure aí",
}


def test_texts_can_be_overridden_for_example_in_portuguese():
    duck = DuckAPI()
    sample_sources.register_sample_sources(duck, NOW)
    search = SemanticSearch(CATALOG_PATH, duck, clock=lambda: NOW, clarification_texts=PT)
    search.thresholds.source = 0.999
    fu = search.search(QUESTION).followup
    assert fu.question == "Qual destes dados deve responder à pergunta?" and QUESTION in fu.context
    search.thresholds.source = 0.8
    search.thresholds.field = 0.999
    conversation = search.conversation(QUESTION)
    fu = conversation.result.followup
    assert fu.question == "Devo procurar “github” em requested domain?"
    assert [o.label for o in fu.options] == ["sim", "não"] and fu.options[0].detail == "sim, procure aí"
    assert conversation.answer("sim").status in ("ok", "needs_clarification")
    assert conversation.history[-1]["understood"] == "yes"


def test_unknown_text_keys_and_placeholders_fail_early():
    from duckduck.semantic.clarify import ClarificationTexts

    with pytest.raises(ValueError, match="unknown key"):
        ClarificationTexts({"source.questoin": "x"})
    with pytest.raises(ValueError, match="unknown placeholder"):
        ClarificationTexts({"source.question": "Qual {fonte}?"})


def test_texts_from_the_config(tmp_path):
    from duckduck.semantic import SemanticConfig

    data = json.load(open(f"{EXAMPLES}/duckduck.local.json"))
    data["semantic"]["catalog_path"] = CATALOG_PATH
    data["semantic"]["clarification_texts"] = PT
    data["semantic"]["thresholds"] = {"source": 0.999}
    data["services"]["samples"]["module"] = f"{EXAMPLES}/sample_sources.py"
    cfg = tmp_path / "duckduck.json"
    cfg.write_text(json.dumps(data))
    duck = DuckAPI()
    duck.auto_register(config_path=str(cfg))
    assert SemanticSearch.from_config(duck, config_path=str(cfg)).search(QUESTION).followup.question == PT["source.question"]
    data["semantic"]["clarification_texts"] = {"nope": "x"}
    cfg.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unknown key"):
        SemanticConfig.load(duck, str(cfg))

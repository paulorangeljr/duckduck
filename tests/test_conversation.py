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
    assert fu.question == "Which data should answer this?"
    assert fu.options[0].value == "proxy_logs" and fu.options[0].label.startswith("proxy_logs — HTTP")
    assert fu.options[0].pins == {"source:proxy_logs": True}
    assert result.clarification_question.splitlines()[1] == "  1) " + fu.options[0].label


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
    assert asked[0] == "Should I use proxy_logs.destination_domain for filtering on 'github' (domain)?"
    assert asked[1] == "Should the answer list proxy_logs.username (as the user)?"
    assert all(v is True for v in conversation.pinned.values())


def test_no_to_a_needed_field_ends_the_conversation():
    conversation = _search(field=0.999).conversation(QUESTION)
    result = conversation.answer("no")
    assert result.status == "needs_clarification" and result.followup.kind == "unresolvable"
    assert conversation.done and "can't be answered without it" in result.clarification
    with pytest.raises(ValueError, match="nothing to answer"):
        conversation.answer("yes")


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
    assert "Which data should answer this?" in result.report() and "  1) proxy_logs" in result.report()


def test_a_problem_no_answer_can_fix_is_unresolvable():
    search = _search()
    result = search.search("Which users accessed github in the last 24hrs?", pinned={"field:proxy_logs.username": False})
    assert result.followup.kind == "unresolvable" and not result.followup.options


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
        "  Q: Which data should answer this?",
        "  A: zzz  (not understood)",
        "  Q: Which data should answer this?",
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
    assert "Which data should answer this?" in out and "[ok]" in out and code == 0

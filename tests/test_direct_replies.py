"""Small talk and questions that aren't about the data get a direct reply — never a plan, never a follow-up."""

import copy
import json

import pytest

pytest.importorskip("pydantic")

from duckduck.semantic import Catalog, JEVAdapter, JevClient, SemanticSearch  # noqa: E402
from duckduck.semantic.shapes import AnswerShapes  # noqa: E402

from test_answer_shapes import CATALOG, Jev, _duck  # noqa: E402


def _catalog():
    catalog = copy.deepcopy(CATALOG)
    catalog["sources"]["alerts"]["examples"] = ["How many alerts per rule?", "Which hosts have critical alerts?"]
    catalog["sources"]["owners"]["examples"] = ["Who owns 10.0.0.1?"]
    return catalog


def _search(**kw):
    return SemanticSearch(Catalog.model_validate(_catalog()), _duck(), **kw)


class ScopeJev(Jev):
    """The fake Decisions API, with a chosen answer to "is this about the data?"."""

    def __init__(self, in_scope):
        super().__init__()
        self.in_scope = in_scope

    def __call__(self, url, data, timeout):
        response = super().__call__(url, data, timeout)
        answers = response.json.return_value["answers"]
        if "in_scope" in answers:
            answers["in_scope"] = {"noul": self.in_scope}
        return response


def _jev(monkeypatch, in_scope):
    jev = ScopeJev(in_scope)
    monkeypatch.setattr("requests.Session.post", lambda self, url, data, timeout: jev(url, data, timeout))
    return _search(engine=JEVAdapter(JevClient(api_key="k"), cache_size=0)), jev


@pytest.mark.parametrize("question, kind", [
    ("hi", "greeting"), ("Hello!", "greeting"), ("Olá, tudo bem?", "greeting"), ("bom dia pessoal", "greeting"),
    ("thanks a lot", "thanks"), ("Valeu!", "thanks"), ("obrigado mesmo", "thanks"),
    ("bye", "goodbye"), ("tchau, até mais", "goodbye"),
    ("hi, which hosts have critical alerts?", None), ("thanks, now show the owners", None),
    ("Which hosts have critical alerts?", None), ("history of 10.0.0.1", None),
])
def test_small_talk_only_when_nothing_else_is_asked(question, kind):
    assert AnswerShapes().small_talk_kind(question) == kind


def test_a_greeting_gets_a_reply_and_examples_thanks_just_a_reply():
    search = _search()
    hello = search.search("Olá!")
    assert hello.status == "ok" and hello.intent.answer_shape == "small_talk" and hello.query_plan is None
    assert hello.reply.startswith("Hi!") and hello.suggestions[:3] == [
        "How many alerts per rule?", "Who owns 10.0.0.1?", "Which hosts have critical alerts?"]  # one per table first
    thanks = search.search("thanks a lot")
    assert thanks.reply == "You're welcome!" and thanks.suggestions == []
    assert "You're welcome!" in thanks.report() and thanks.to_dict()["reply"] == "You're welcome!"


def test_out_of_scope_says_what_can_be_asked():
    result = _search().search("How is the weather in Lisbon?")
    assert result.status == "ok" and result.intent.answer_shape == "out_of_scope" and result.results is None
    assert "ip address, security alert and user" in result.reply and result.suggestions
    record = next(d for d in result.decisions if d.kind == "in_scope")
    assert record.probability < record.threshold


def test_small_talk_never_calls_the_engine(monkeypatch):
    search, jev = _jev(monkeypatch, in_scope=0.95)
    assert search.search("good morning").intent.answer_shape == "small_talk" and not jev.bodies
    assert search.preview("good morning")["answer_shape"]["choice"] == "small_talk" and not jev.bodies


def test_the_engine_decides_what_is_out_of_scope_in_the_same_batch(monkeypatch):
    search, jev = _jev(monkeypatch, in_scope=0.05)
    result = search.search("Who won the world cup?")  # "who" is a catalog word: offline wouldn't catch it
    assert result.intent.answer_shape == "out_of_scope" and len(jev.bodies) == 1
    assert "in_scope" in jev.bodies[0]["questions"] and len(jev.bodies[0]["questions"]) > 1


def test_in_doubt_it_tries_the_data(monkeypatch):
    search, _ = _jev(monkeypatch, in_scope=0.5)
    result = search.search("Which hosts have critical alerts?")
    assert result.status == "ok" and result.intent.answer_shape == "list" and result.reply is None


def test_try_anyway_pins_it_and_never_asks_again(monkeypatch):
    search, jev = _jev(monkeypatch, in_scope=0.01)
    result = search.search("Which hosts have critical alerts?", pinned={"in_scope": True})
    assert result.status == "ok" and result.reply is None
    assert "in_scope" not in jev.bodies[0]["questions"]
    assert [d.decided_by for d in result.decisions if d.kind == "in_scope"] == ["user"]
    assert search.search("hi", pinned={"in_scope": True}).intent.answer_shape != "small_talk"


def test_the_replies_are_configurable():
    with pytest.raises(ValueError):  # unknown placeholder
        _search(clarification_texts={"reply.greeting": "Hi {nope}"})
    search = _search(clarification_texts={"reply.greeting": "Olá! Pergunte o que quiser.",
                                          "reply.examples": "Por exemplo:",
                                          "reply.out_of_scope": "Não achei nada disso nos dados."})
    assert search.search("oi").reply == "Olá! Pergunte o que quiser. Por exemplo:"
    assert search.search("How is the weather in Lisbon?").reply.startswith("Não achei nada disso nos dados.")


def test_the_web_app(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app

    search = _search()
    client = TestClient(create_app(lambda: search, store=None))
    assert client.get("/api/meta").json()["texts"]["ask_anyway"]
    reply = client.post("/api/ask", json={"question": "How is the weather in Lisbon?"}).json()["result"]
    assert reply["intent"]["answer_shape"] == "out_of_scope" and reply["reply"] and reply["suggestions"]
    anyway = client.post("/api/ask", json={"question": "How is the weather in Lisbon?", "in_scope": True}).json()
    assert anyway["result"]["intent"]["answer_shape"] != "out_of_scope"
    json.dumps(anyway)

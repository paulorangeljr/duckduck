"""Ask turned on and off: ``serve --ask/--no-ask`` and ``semantic.enabled`` (the Config tab's switch). Off, nothing
of the semantic layer is loaded, the page hides Ask and its tabs, and SQL keeps working."""

import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from duckduck.semantic import AskOff, SemanticConfig, SemanticSearch  # noqa: E402
from duckduck.semantic.commands import serve  # noqa: E402

from test_first_run import project  # noqa: E402,F401  (the fixture: connected tables, an LLM, no catalog)


def test_no_ask_starts_without_loading_the_semantic_layer(project, monkeypatch):  # noqa: F811
    loaded = []
    monkeypatch.setattr(SemanticSearch, "from_config", classmethod(lambda cls, *a, **k: loaded.append(a)))
    client = TestClient(serve(config_path=str(project / "duckduck.json"), run=False, ask=False))
    meta = client.get("/api/meta").json()
    assert loaded == []  # no catalog read, no AI provider built
    assert meta["ask_off"] == "started with --no-ask" and meta["features"]["ask"] is False
    assert meta["setup"] is None  # off isn't "not set up": the page shows no setup card
    asked = client.post("/api/ask", json={"question": "which hosts?"})
    assert asked.status_code == 403 and "--ask" in asked.json()["detail"] and "enabled" in asked.json()["detail"]
    assert client.post("/api/preview", json={"question": "which hosts?"}).json()["sources"] == []
    assert client.post("/api/sql", json={"sql": "SELECT name FROM hosts"}).json()["rows"] == [["web"]]


def test_the_config_switch_turns_ask_off_and_on_without_a_restart(project):  # noqa: F811
    data = json.loads((project / "duckduck.json").read_text())
    data["semantic"]["enabled"] = False
    (project / "duckduck.json").write_text(json.dumps(data))
    client = TestClient(serve(config_path=str(project / "duckduck.json"), run=False, allow_config_edit=True))
    assert client.get("/api/meta").json()["ask_off"] == "semantic.enabled is false in duckduck.json"

    config = client.get("/api/config").json()["config"]  # masked, as the page edits it
    config["semantic"]["enabled"] = True
    assert client.put("/api/config", json={"config": config}).status_code == 200
    meta = client.get("/api/meta").json()
    assert meta["ask_off"] is None and meta["setup"] is not None  # on: here, still waiting for its catalog
    assert json.loads((project / "duckduck.json").read_text())["ai_providers"]["claude"]["authentication"]["api_key"] == "k"


def test_the_command_line_wins_over_the_config(project):  # noqa: F811
    data = json.loads((project / "duckduck.json").read_text())
    data["semantic"]["enabled"] = False
    (project / "duckduck.json").write_text(json.dumps(data))
    client = TestClient(serve(config_path=str(project / "duckduck.json"), run=False, ask=True))
    assert client.get("/api/meta").json()["ask_off"] is None


def test_the_flag_reaches_serve(project, monkeypatch):  # noqa: F811
    from duckduck.semantic import __main__ as cli

    seen = {}
    monkeypatch.setattr(cli, "serve", lambda **kw: seen.update(kw))
    cli.main(["--config", str(project / "duckduck.json"), "serve", "--no-ask"])
    assert seen["ask"] is False
    cli.main(["--config", str(project / "duckduck.json"), "serve"])
    assert seen["ask"] is None  # follows semantic.enabled


def test_a_search_turned_off_refuses_to_answer_and_says_how_to_turn_it_on():
    from duckduck import DuckAPI

    search = SemanticSearch.turned_off(DuckAPI(), "semantic.enabled is false in duckduck.json")
    assert not search.ready
    with pytest.raises(AskOff, match="enabled"):
        search.search("which hosts?")
    assert SemanticConfig().enabled is True  # on unless the config says otherwise


def test_the_page_hides_ask_and_its_tabs_when_off():
    from duckduck.semantic.webpage import PAGE

    assert 'const ASK_TABS = ["ask", "history", "dashboard", "suggestions"]' in PAGE
    assert "META?.ask_off" in PAGE and 'semantic: ["enabled", "reader"' in PAGE

"""A catalog source whose table can't be used is left out — and the page says which one, and why."""

import warnings

import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, SemanticSearch  # noqa: E402

from test_answer_shapes import ALERTS, CATALOG, OWNERS  # noqa: E402


def _search():
    """alerts works; owners' table is misspelled in DuckAPI; countries' service (restcountries) failed to start."""
    duck = DuckAPI()
    with pytest.warns(RuntimeWarning, match="failed to initialize"):
        duck.auto_register({"world": {"connector": "restcountries"}}, on_error="warn")  # no api key → fails
    duck.register_api_function("alerts", lambda limit=None: ALERTS)
    duck.register_api_function("owner", lambda limit=None: OWNERS)
    duck.service_of.update({"alerts": "siem", "owner": "cmdb"})
    catalog = {**CATALOG, "sources": {**CATALOG["sources"], "countries": {
        "description": "Countries of the world", "table": "world_countries",
        "fields": {"name": {"description": "Country name"}}}}}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        search = SemanticSearch(Catalog.model_validate(catalog), duck)
    return search, [str(w.message) for w in caught]


def test_each_left_out_source_says_why():
    search, warned = _search()
    assert set(search.catalog.sources) == {"alerts"}
    reasons = {u["source"]: u["reason"] for u in search.unavailable()}
    assert "its service 'world' (connector restcountries) failed to start" in reasons["countries"]
    assert "missing 'authentication' block" in reasons["countries"]
    assert "did you mean 'owner'?" in reasons["owners"]
    assert search.duck.failed_services["world"]["prefix"] == "world"
    assert any("did you mean 'owner'" in w for w in warned)  # the server log says it too


def test_a_service_that_starts_later_is_no_longer_failed():
    duck = DuckAPI()
    with pytest.warns(RuntimeWarning, match="failed to initialize"):
        duck.auto_register({"world": {"connector": "restcountries"}}, on_error="warn")
    assert "world" in duck.failed_services
    duck.auto_register({"world": {"connector": "nvd"}}, on_error="warn")  # same name, starts fine
    assert duck.failed_services == {}


def test_what_is_connected_shows_the_failed_system():
    search, _ = _search()
    result = search.search("Which systems are connected?", pinned={"answer_shape": "catalog"})
    world = result.results.set_index("system").loc["world"]
    assert world["tables"] == 0 and "failed to start" in world["kind"] and "authentication" in world["problem"]


def test_the_page_is_told():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app

    search, _ = _search()
    client = TestClient(create_app(lambda: search, store=None))
    meta = client.get("/api/meta").json()
    assert {u["source"] for u in meta["unavailable_sources"]} == {"owners", "countries"}
    preview = client.post("/api/preview", json={"question": "Which hosts have critical alerts?"}).json()
    assert {u["source"] for u in preview["unavailable"]} == {"owners", "countries"}
    catalog = client.get("/api/catalog").json()
    assert {u["source"] for u in catalog["unavailable"]} == {"owners", "countries"}

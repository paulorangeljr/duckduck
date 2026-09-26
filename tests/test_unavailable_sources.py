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


def test_every_catalog_table_with_its_connection():
    search, _ = _search()
    status = {r["source"]: r for r in search.source_status()}
    assert set(status) == {"alerts", "owners", "countries"}  # the catalog as written, not just what's usable
    assert status["alerts"]["connected"] and status["alerts"]["system"] == "siem" and status["alerts"]["reason"] is None
    assert not status["countries"]["connected"] and status["countries"]["system"] == "world"
    assert "failed to start" in status["countries"]["reason"]
    assert not status["owners"]["connected"] and "did you mean 'owner'" in status["owners"]["reason"]
    assert [r["connected"] for r in search.source_status()] == [False, False, True]  # the problems first


def test_a_test_reads_one_row_now():
    search, _ = _search()
    ok = search.check_source("alerts")
    assert ok["ok"] and ok["columns"] == 3 and ok["rows"] == 1
    assert search.check_source("countries") == {"source": "countries", "ok": False, "ms": 0,
                                                "error": search.unavailable_sources["countries"]["reason"]}

    def broken(limit=None):
        raise ConnectionError("401 Unauthorized: bad key")

    search.duck.register_api_function("alerts", broken)  # registered, but the API refuses it
    bad = search.check_source("alerts")
    assert not bad["ok"] and "401 Unauthorized" in bad["error"]
    with pytest.raises(KeyError):
        search.check_source("nope")


def test_the_config_page_is_told_and_the_ask_page_is_not():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app

    search, _ = _search()
    client = TestClient(create_app(lambda: search, store=None))
    status = client.get("/api/catalog").json()["status"]
    assert {r["source"]: r["connected"] for r in status} == {"alerts": True, "owners": False, "countries": False}
    assert client.post("/api/catalog/check", json={"source": "alerts"}).json()["ok"] is True
    assert client.post("/api/catalog/check", json={"source": "nope"}).status_code == 404
    preview = client.post("/api/preview", json={"question": "Which hosts have critical alerts?"}).json()
    assert "unavailable" not in preview  # the Ask page stays about the question

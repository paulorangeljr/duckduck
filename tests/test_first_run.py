"""A first run: no semantic catalog yet (or no 'semantic' section at all) — the web app still starts, SQL works,
Ask says what's missing, and drafting the catalog from Config makes Ask work without a restart."""

import json
import time

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import CatalogUnavailable, SemanticConfig, SemanticSearch  # noqa: E402
from duckduck.semantic.__main__ import main  # noqa: E402

from test_catalog_refresh import CountingLLM  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Connected systems, an LLM, and no catalog file."""
    (tmp_path / "src.py").write_text(
        "TABLES = {'hosts': lambda limit=None: [{'ip': '10.0.0.1', 'name': 'web'}],\n"
        "          'alerts': lambda limit=None: [{'ip': '10.0.0.1', 'sev': 'high'}]}\n"
    )
    services = {"src": {"connector": "python", "module": "src.py", "table_prefix": ""}}
    (tmp_path / "services_only.json").write_text(json.dumps({"services": services}))
    (tmp_path / "duckduck.json").write_text(json.dumps({
        "services": services,
        "ai_providers": {"claude": {"provider": "anthropic", "model": "claude-opus-5",
                                    "authentication": {"type": "local", "api_key": "k"}}},
        "semantic": {"catalog_path": "catalog.yaml", "default_llm": "claude",
                     "feedback": {"path": "feedback.duckdb"}},
    }))
    llm = CountingLLM()
    monkeypatch.setattr(SemanticConfig, "build_llm", lambda self, duck, stage=None: llm)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_config_with_only_services_is_a_valid_start(project):
    cfg = SemanticConfig.load(DuckAPI(), str(project / "services_only.json"))
    assert cfg.catalog_path == "semantic_catalog.yaml" and cfg.decision_engine.ai_provider is None


def test_asking_without_a_catalog_says_how_to_get_one(project):
    duck = DuckAPI()
    duck.auto_register(config_path=str(project / "duckduck.json"))
    with pytest.raises(CatalogUnavailable) as missing:
        SemanticSearch.from_config(duck, str(project / "duckduck.json"))
    assert missing.value.path == str(project / "catalog.yaml")
    assert "no semantic catalog" in str(missing.value) and "generate-catalog" in str(missing.value)
    assert "SQL tab" in str(missing.value)
    (project / "catalog.yaml").write_text("sources: [not, a, mapping")  # broken YAML
    with pytest.raises(CatalogUnavailable, match="can't be used"):
        SemanticSearch.from_config(duck, str(project / "duckduck.json"))


def test_a_search_without_a_catalog_refuses_to_answer(project):
    duck = DuckAPI()
    search = SemanticSearch.without_catalog(duck, CatalogUnavailable("x.yaml", "there's no semantic catalog at x.yaml yet"))
    assert not search.ready and search.setup["catalog_path"] == "x.yaml"
    with pytest.raises(CatalogUnavailable):
        search.search("which hosts?")
    with pytest.raises(CatalogUnavailable):
        search.preview("which hosts?")


def test_the_cli_prints_the_way_forward_instead_of_a_traceback(project, capsys):
    assert main(["--config", str(project / "services_only.json"), "ask", "which hosts?"]) == 2
    err = capsys.readouterr().err
    assert "no semantic catalog" in err and "Traceback" not in err


@pytest.mark.parametrize("config", ["services_only.json", "duckduck.json"])
def test_the_web_app_starts_and_sql_works(project, config):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.commands import serve

    client = TestClient(serve(config_path=str(project / config), run=False))
    meta = client.get("/api/meta").json()
    assert meta["features"]["ask"] is False and "no semantic catalog" in meta["setup"]["reason"]
    assert client.post("/api/sql", json={"sql": "SELECT name FROM hosts"}).json()["rows"] == [["web"]]
    asked = client.post("/api/ask", json={"question": "which hosts?"})
    assert asked.status_code == 409 and "generate-catalog" in asked.json()["detail"]
    assert client.post("/api/preview", json={"question": "which hosts?"}).json()["sources"] == []
    catalog = client.get("/api/catalog").json()
    assert catalog["sources"] == [] and set(catalog["not_in_catalog"]) == {"hosts", "alerts"}


def test_drafting_the_catalog_from_config_makes_ask_work(project):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.commands import serve

    client = TestClient(serve(config_path=str(project / "duckduck.json"), run=False, allow_config_edit=True))
    assert client.get("/api/meta").json()["setup"] is not None
    job = client.post("/api/catalog/generate", json={}).json()
    for _ in range(300):
        if job["state"] != "running":
            break
        time.sleep(0.02)
        job = client.get(f"/api/catalog/jobs/{job['id']}").json()
    assert job["state"] == "done" and job["reloaded"], job
    assert client.get("/api/meta").json()["setup"] is None  # no restart
    assert client.post("/api/ask", json={"question": "which hosts?"}).status_code == 200

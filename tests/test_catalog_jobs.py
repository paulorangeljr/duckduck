"""Generating the semantic catalog from the web app: a background job, its log live, the app reloaded after."""

import json
import threading
import time

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from duckduck.semantic import SemanticConfig, generate_catalog  # noqa: E402
from duckduck.semantic.catalog_jobs import CatalogJobs  # noqa: E402
from duckduck.semantic.commands import serve  # noqa: E402

from test_catalog_refresh import CountingLLM  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "src.py").write_text(
        "TABLES = {'hosts': lambda limit=None: [{'ip': '10.0.0.1', 'name': 'web'}],\n"
        "          'alerts': lambda limit=None: [{'ip': '10.0.0.1', 'sev': 'high'}],\n"
        "          'events': lambda limit=None: [{'ip': '10.0.0.1', 'kind': 'login'}]}\n"
    )
    (tmp_path / "duckduck.json").write_text(json.dumps({
        "services": {"src": {"connector": "python", "module": "src.py", "table_prefix": ""}},
        "ai_providers": {"claude": {"provider": "anthropic", "model": "claude-opus-5",
                                    "authentication": {"type": "local", "api_key": "k"}}},
        "semantic": {"catalog_path": "catalog.yaml", "default_llm": "claude",
                     "feedback": {"path": "feedback.duckdb"}},
    }))
    llm = CountingLLM()
    monkeypatch.setattr(SemanticConfig, "build_llm", lambda self, duck, stage=None: llm)
    config = str(tmp_path / "duckduck.json")
    generate_catalog(config_path=config, only=["hosts", "alerts"])  # the app starts from a catalog
    llm.drafted.clear()
    return config, llm


def _finished(client, job):
    for _ in range(200):
        if job["state"] != "running":
            return job
        time.sleep(0.02)
        job = client.get(f"/api/catalog/jobs/{job['id']}").json()
    raise AssertionError("the job never finished")


def test_the_card_shows_the_catalog_and_what_is_not_in_it_yet(project):
    config, _ = project
    client = TestClient(serve(config_path=config, run=False, allow_config_edit=True))
    status = client.get("/api/catalog").json()
    assert sorted(s["name"] for s in status["sources"]) == ["alerts", "hosts"]
    assert all(s["generated_at"] and s["fields"] == 2 for s in status["sources"])
    assert status["not_in_catalog"] == ["events"] and status["off"] is None
    assert status["info"]["path"].endswith("catalog.yaml") and "claude" in status["info"]["llm"]
    assert client.get("/api/meta").json()["features"]["catalog_generation"] is True


def test_a_table_is_added_as_a_job_and_the_app_uses_it_after(project):
    config, llm = project
    client = TestClient(serve(config_path=config, run=False, allow_config_edit=True))
    started = client.post("/api/catalog/generate", json={"only": ["events"]})
    assert started.status_code == 202
    job = _finished(client, started.json())
    assert job["state"] == "done" and job["reloaded"] is True and llm.drafted == ["events"]
    assert list(job["result"]["drafted"]) == ["events"] and job["result"]["sources"] == 3
    assert any("[1/1] events" in line for line in job["log"])  # the generation's own progress lines
    assert client.get(f"/api/catalog/jobs/{job['id']}?since={job['log_size']}").json()["log"] == []
    status = client.get("/api/catalog").json()
    assert status["not_in_catalog"] == [] and status["job"]["id"] == job["id"]
    assert "events" in client.app.state.duckduck["search"].catalog.sources  # reloaded: questions use it


def test_redraft_one_table_or_everything(project):
    config, llm = project
    client = TestClient(serve(config_path=config, run=False, allow_config_edit=True))
    job = _finished(client, client.post("/api/catalog/generate", json={"force": ["alerts"]}).json())
    assert llm.drafted == ["alerts"] and job["result"]["drafted"] == {"alerts": "forced"}
    llm.drafted.clear()
    _finished(client, client.post("/api/catalog/generate", json={}).json())  # update: only the new one
    assert llm.drafted == ["events"]
    llm.drafted.clear()
    up_to_date = _finished(client, client.post("/api/catalog/generate", json={}).json())
    assert llm.drafted == [] and up_to_date["result"]["changed"] is False and up_to_date["reloaded"] is None
    assert client.post("/api/catalog/generate", json={"force": "yes"}).status_code == 400


def test_off_unless_config_editing_is_on(project):
    config, llm = project
    client = TestClient(serve(config_path=config, run=False))
    assert "--edit-config" in client.get("/api/catalog").json()["off"]
    assert client.post("/api/catalog/generate", json={}).status_code == 403 and llm.drafted == []
    assert client.get("/api/meta").json()["features"]["catalog_generation"] is False


def test_one_job_at_a_time_and_a_failure_is_reported():
    gate = threading.Event()

    def slow(force, only):
        gate.wait(5)
        raise ValueError("nothing to catalog")

    jobs = CatalogJobs(slow)
    first = jobs.start()
    with pytest.raises(RuntimeError):
        jobs.start()
    gate.set()
    for _ in range(200):
        if first.state != "running":
            break
        time.sleep(0.01)
    assert first.state == "failed" and first.error == "ValueError: nothing to catalog" and first.reloaded is None
    assert jobs.start(wait=True).state == "failed"  # the next one may start

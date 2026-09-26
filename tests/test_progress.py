"""Live progress: what a question is doing now, and pausing / cancelling it (library and web app)."""

import threading
import time

import pandas as pd
import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.progress import Cancelled, Progress, tracking  # noqa: E402
from duckduck.semantic import Catalog, SemanticSearch  # noqa: E402

from test_answer_shapes import ALERTS, CATALOG, OWNERS  # noqa: E402

QUESTION = "Which hosts have critical alerts?"


class SlowAPI:
    """``alerts`` blocks until the test lets it go — a slow HTTP call in flight."""

    def __init__(self):
        self.called, self.go = threading.Event(), threading.Event()

    def alerts(self, limit=None):
        self.called.set()
        assert self.go.wait(5)
        return ALERTS


def _search(api=None):
    duck = DuckAPI()
    duck.register_api_function("alerts", api.alerts if api else (lambda limit=None: ALERTS))
    duck.register_api_function("owners", lambda limit=None: OWNERS)
    return SemanticSearch(Catalog.model_validate(CATALOG), duck)


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_each_step_is_reported_with_what_it_has_so_far():
    progress = Progress()
    with tracking(progress):
        result = _search().search(QUESTION)
    assert result.status == "ok"
    stages = [e["stage"] for e in progress.events]
    assert stages[:3] == ["reading", "retrieval", "deciding"]
    assert stages[-3:] == ["sql", "fetching", "combining"] and "planning" in stages
    texts = " | ".join(e["text"] for e in progress.events)
    assert "Asking the offline rules" in texts and "Reading alerts (alerts)" in texts
    assert progress.partial["sql"] == result.sql and progress.partial["fetched"]["alerts"]["rows"] == len(ALERTS)
    assert [d["kind"] for d in progress.partial["decisions"]] == [d.kind for d in result.decisions]


def test_without_progress_nothing_changes():
    assert _search().search(QUESTION).status == "ok"  # no Progress in context: every report is a no-op


def test_pause_stops_at_the_next_step_and_resume_goes_on():
    api = SlowAPI()
    search, progress, out = _search(api), Progress(), {}

    def run():
        with tracking(progress):
            out["result"] = search.search(QUESTION)

    worker = threading.Thread(target=run)
    worker.start()
    assert api.called.wait(5)
    progress.pause()
    assert progress.to_dict()["state"] == "pausing"  # the API call in flight finishes first
    api.go.set()
    assert _wait(lambda: progress.state == "paused")
    seen = progress.to_dict()
    assert seen["events"][-1]["stage"] == "fetching" and "sql" in seen["partial"]  # the SQL is there to look at
    assert "result" not in out
    progress.resume()
    worker.join(5)
    assert out["result"].status == "ok" and progress.events[-1]["stage"] == "combining"


def test_cancel_stops_it_and_a_paused_search_can_be_cancelled_too():
    api = SlowAPI()
    search, progress, out = _search(api), Progress(), {}

    def run():
        with tracking(progress):
            try:
                search.search(QUESTION)
            except Cancelled:
                out["cancelled"] = True

    worker = threading.Thread(target=run)
    worker.start()
    assert api.called.wait(5)
    progress.pause()
    api.go.set()
    assert _wait(lambda: progress.state == "paused")
    progress.cancel()
    worker.join(5)
    assert out == {"cancelled": True} and progress.to_dict()["state"] == "cancelled"


def test_cancel_between_pages_drops_the_half_read_table():
    pages_read = []

    def iter_alerts():
        for i in range(10):
            pages_read.append(i)
            if i == 2:
                progress.cancel()
            yield pd.DataFrame(ALERTS)

    search = _search()
    search.duck.register_streaming_function("alerts", iter_alerts)
    progress = Progress()
    with tracking(progress), pytest.raises(Cancelled):
        search.search("Which hosts have alerts that are not critical?")
    assert pages_read == [0, 1, 2]
    left = search.duck.conn.sql("SELECT table_name FROM duckdb_tables() WHERE table_name LIKE '_sem_%'").df()
    assert left.empty


def test_the_web_app_runs_a_question_as_a_job():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app

    api = SlowAPI()
    search = _search(api)
    client = TestClient(create_app(lambda: search, store=None))
    started = client.post("/api/ask", json={"question": QUESTION, "background": True})
    assert started.status_code == 202
    job = started.json()["job_id"]
    assert api.called.wait(5)
    running = client.get(f"/api/jobs/{job}").json()
    assert running["state"] == "running" and running["events"][-1]["text"] == "Reading alerts (alerts)"
    assert "SELECT DISTINCT" in running["partial"]["sql"]
    assert client.get(f"/api/jobs/{job}?since=100").json()["events"] == []
    assert client.post(f"/api/jobs/{job}/pause").json()["state"] == "pausing"
    api.go.set()
    assert _wait(lambda: client.get(f"/api/jobs/{job}").json()["state"] == "paused")
    client.post(f"/api/jobs/{job}/resume")
    assert _wait(lambda: client.get(f"/api/jobs/{job}").json()["state"] == "done")
    done = client.get(f"/api/jobs/{job}").json()
    assert done["result"]["result"]["status"] == "ok" and len(done["result"]["result"]["results"]) == 3
    conv = done["result"]["conversation_id"]
    assert client.post("/api/columns", json={"conversation_id": conv, "columns": ["alerts.rule"]}).status_code == 200

    api2 = SlowAPI()
    search2 = _search(api2)
    client2 = TestClient(create_app(lambda: search2, store=None))
    job2 = client2.post("/api/ask", json={"question": QUESTION, "background": True}).json()["job_id"]
    assert api2.called.wait(5)
    cancelled = client2.post(f"/api/jobs/{job2}/cancel").json()
    assert cancelled["state"] == "cancelled" and "sql" in cancelled["partial"]  # at once, with what it had
    api2.go.set()
    assert client2.post(f"/api/jobs/{job2}/nope").status_code == 404
    assert client2.get("/api/jobs/unknown").status_code == 404
    bad = client2.post("/api/ask", json={"question": QUESTION, "background": True, "entity": "nope"})
    assert bad.status_code == 400  # checked before the job starts

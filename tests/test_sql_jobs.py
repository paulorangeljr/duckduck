"""The SQL tab: debug log, and a query run as a job — live steps and log, pause between pages, cancel (DuckDB too)."""

import threading
import time

import pandas as pd
import pytest

pytest.importorskip("pydantic")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from duckduck.semantic.admin import SQLConsole  # noqa: E402
from duckduck.semantic.server import create_app  # noqa: E402

from test_answer_shapes import _search  # noqa: E402


class Pages:
    """``iter_rows`` yields a page each time the test lets it (``go``) — a paginated API."""

    def __init__(self, pages=3):
        self.pages, self.go, self.served, self.closed = pages, threading.Semaphore(0), 0, False

    def rows(self, limit=None):
        return pd.DataFrame({"n": range(self.pages * 2)})

    def iter_rows(self):
        try:
            for p in range(self.pages):
                assert self.go.acquire(timeout=5)
                self.served += 1
                yield pd.DataFrame({"n": [2 * p, 2 * p + 1]})
        finally:
            self.closed = True


def _client(pages=None):
    search = _search()
    duck = search.duck
    if pages is not None:
        duck.register_api_function("rows", pages.rows)
        duck.register_streaming_function("rows", pages.iter_rows)
    return TestClient(create_app(lambda: search, store=None, console=SQLConsole(duck)))


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_debug_log_shows_what_info_doesnt():
    client = _client()
    info = client.post("/api/sql", json={"sql": "SELECT * FROM alerts"}).json()
    debug = client.post("/api/sql", json={"sql": "SELECT * FROM alerts", "debug": True}).json()
    assert info["rows"] == debug["rows"] and debug["debug"] is True
    assert not any("_api_alerts_" in line for line in info["log"])
    assert any("_api_alerts_" in line for line in debug["log"])  # the rewritten query, a DEBUG line


def test_a_query_runs_as_a_job_with_its_steps_and_log():
    client = _client()
    job = client.post("/api/sql", json={"sql": "SELECT count(*) AS n FROM alerts", "background": True})
    assert job.status_code == 202
    jid = job.json()["job_id"]
    assert _wait(lambda: client.get(f"/api/jobs/{jid}").json()["state"] == "done")
    view = client.get(f"/api/jobs/{jid}").json()
    assert view["result"]["rows"][0][0] > 0 and view["result"]["columns"] == ["n"]
    assert [e["stage"] for e in view["events"]] == ["planning", "fetching", "query"]
    assert any("alerts" in line for line in view["log"]) and view["partial"]["fetched"]["alerts"]["rows"] > 0


def test_pause_between_pages_then_continue():
    pages = Pages()
    client = _client(pages)
    jid = client.post("/api/sql", json={"sql": "SELECT sum(n) AS s FROM rows", "background": True}).json()["job_id"]
    view = lambda: client.get(f"/api/jobs/{jid}").json()  # noqa: E731
    assert _wait(lambda: any(e["stage"] == "fetching" for e in view()["events"]))
    pages.go.release()
    assert _wait(lambda: "page 1" in view()["events"][-1]["text"])
    client.post(f"/api/jobs/{jid}/pause")
    pages.go.release()  # the page in flight arrives, then it stops
    assert _wait(lambda: view()["state"] == "paused")
    time.sleep(0.1)
    assert pages.served == 2 and view()["state"] == "paused"
    client.post(f"/api/jobs/{jid}/resume")
    pages.go.release()
    assert _wait(lambda: view()["state"] == "done")
    assert view()["result"]["rows"] == [[15]] and pages.closed


def test_cancel_between_pages_stops_reading():
    pages = Pages()
    client = _client(pages)
    jid = client.post("/api/sql", json={"sql": "SELECT * FROM rows", "background": True}).json()["job_id"]
    pages.go.release()
    assert _wait(lambda: "page 1" in client.get(f"/api/jobs/{jid}").json()["events"][-1]["text"])
    assert client.post(f"/api/jobs/{jid}/cancel").json()["state"] == "cancelled"
    pages.go.release()
    assert _wait(lambda: pages.closed)
    assert pages.served == 2 and client.get(f"/api/jobs/{jid}").json()["state"] == "cancelled"
    # the console is free again
    assert client.post("/api/sql", json={"sql": "SELECT 1 AS one"}).json()["rows"] == [[1]]


def test_cancel_interrupts_duckdb_itself():
    client = _client()
    jid = client.post("/api/sql", json={"sql": "SELECT sum(i) FROM range(100000000000) t(i)",
                                        "background": True}).json()["job_id"]
    assert _wait(lambda: any(e["stage"] == "query" for e in client.get(f"/api/jobs/{jid}").json()["events"]))
    time.sleep(0.2)
    client.post(f"/api/jobs/{jid}/cancel")
    assert _wait(lambda: client.post("/api/sql", json={"sql": "SELECT 1 AS one"}).json().get("rows") == [[1]], 10)
    assert client.get(f"/api/jobs/{jid}").json()["state"] == "cancelled"


def test_the_page_runs_sql_as_a_job():
    from duckduck.semantic.webpage import PAGE

    assert 'id="sqldebug"' in PAGE and "background: true" in PAGE and 'data-sqljob="pause"' in PAGE

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


def test_paused_then_stop_and_show_runs_the_query_on_what_was_read():
    pages = Pages(pages=5)
    client = _client(pages)
    jid = client.post("/api/sql", json={"sql": "SELECT n FROM rows", "background": True}).json()["job_id"]
    view = lambda: client.get(f"/api/jobs/{jid}").json()  # noqa: E731
    pages.go.release()
    assert _wait(lambda: "page 1" in view()["events"][-1]["text"])
    client.post(f"/api/jobs/{jid}/pause")
    pages.go.release()  # the page in flight arrives, then it pauses
    assert _wait(lambda: view()["state"] == "paused")
    assert client.post(f"/api/jobs/{jid}/stop").json()["stopped"] is True
    assert _wait(lambda: view()["state"] == "done")
    r = view()["result"]
    assert r["rows"] == [[0], [1], [2], [3]] and pages.served == 2 and pages.closed  # no third page asked
    assert r["stopped"]["rows"] == {"rows": 4, "rows_scanned": 4, "pages": 2}
    assert r["types"] == ["BIGINT"]
    # a read cut short is never kept as the whole table: the next run reads every page
    for _ in range(5):
        pages.go.release()
    again = client.post("/api/sql", json={"sql": "SELECT count(*) AS c FROM rows"}).json()
    assert again["rows"] == [[10]] and again["stopped"] is None


def test_stop_ends_the_shared_pager_and_unread_sources_read_one_page():
    from duckduck.common import progress, slicing

    asked = []

    def fetch_page(n):
        asked.append(n)
        return list(range(n * 3, n * 3 + 3)), 30

    p = progress.Progress()
    with progress.tracking(p):
        got = []
        for rows in slicing.pages(fetch_page, 3):
            got += rows
            if len(asked) == 2:
                p.stop()
        assert asked == [0, 1] and got == [0, 1, 2, 3, 4, 5]
        asked.clear()
        assert sum(len(r) for r in slicing.pages(fetch_page, 3)) == 3 and asked == [0]  # already stopped: one page


def test_result_and_viewer_pages_carry_each_columns_type():
    client = _client()
    r = client.post("/api/sql", json={"sql": "SELECT 1 AS i, 'x' AS s, DATE '2026-10-01' AS d, [1, 2] AS l"}).json()
    assert r["types"] == ["INTEGER", "VARCHAR", "DATE", "INTEGER[]"]
    page = client.get(f"/api/sql/results/{r['result_id']}").json()
    assert page["types"] == r["types"]


def test_the_page_keeps_the_log_open_and_offers_stop_and_show():
    from duckduck.semantic.webpage import PAGE

    assert 'data-sqljob="stop"' in PAGE and "LOG_OPEN[v.job_id]" in PAGE and "typeTag(" in PAGE


def test_what_each_source_will_be_sent_is_shown_before_it_is_read_and_checking_never_waits():
    pages = Pages()
    client = _client(pages)
    jid = client.post("/api/sql", json={"sql": "SELECT n FROM rows WHERE n > 1", "background": True}).json()["job_id"]
    view = lambda: client.get(f"/api/jobs/{jid}").json()  # noqa: E731
    assert _wait(lambda: "pushdown" in view()["partial"])
    planned = view()["partial"]["pushdown"]
    assert pages.served == 0 and planned["calls"][0]["table"] == "rows"  # nothing read yet
    assert "n > 1" in planned["report"] and planned["warnings"]
    assert not any("kept 0 of 0" in line for line in view()["log"])  # planning ahead isn't in the run's log
    # the query holds the console (waiting on its first page): checking another one still answers at once
    started = time.time()
    checked = client.post("/api/sql/explain", json={"sql": "SELECT * FROM rows LIMIT 2"}).json()
    assert time.time() - started < 2 and checked["pushdown"]["calls"]
    for _ in range(3):
        pages.go.release()
    assert _wait(lambda: view()["state"] == "done")


def test_the_page_checks_push_down_as_you_type():
    from duckduck.semantic.webpage import PAGE

    assert 'id="sqlpd"' in PAGE and "livePushdownSoon" in PAGE and '"planned", "job:"' in PAGE

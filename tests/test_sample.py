"""Answers are a sample first; what was read is kept, so more columns or every row don't read the APIs again."""

import time

import pandas as pd
import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, SemanticSearch  # noqa: E402

from test_answer_shapes import ALERTS, CATALOG, OWNERS  # noqa: E402

MANY = [{**a, "ip": f"10.1.{i}.{j}"} for i in range(10)
        for j, a in enumerate(ALERTS.to_dict("records"))]  # 60 alerts, 30 critical, each its own host


def _search(pages=None, **kw):
    """``alerts`` counts its calls; with ``pages``, it's also read page by page (10 rows a page)."""
    calls = {"fetch": 0, "pages": 0}

    def alerts(limit=None):
        calls["fetch"] += 1
        return pd.DataFrame(MANY[:limit] if limit else MANY)

    def iter_alerts():
        for k in range(0, len(MANY), 10):
            calls["pages"] += 1
            yield pd.DataFrame(MANY[k:k + 10])

    duck = DuckAPI()
    duck.register_api_function("alerts", alerts)
    duck.register_api_function("owners", lambda limit=None: OWNERS)
    if pages:
        duck.register_streaming_function("alerts", iter_alerts)
    return SemanticSearch(Catalog.model_validate(CATALOG), duck, **kw), calls


def test_a_sample_shows_the_first_rows_and_says_there_are_more():
    search, calls = _search(pages=True)
    result = search.search("Which hosts have critical alerts?", sample=5)
    assert (result.status, len(result.results), result.sample, result.has_more) == ("ok", 5, 5, True)
    assert result.sql.rstrip().endswith("LIMIT 5") and result.plan_limit == 1000
    everything = search.search("Which hosts have critical alerts?", sample=None)
    assert (len(everything.results), everything.sample, everything.has_more) == (30, None, False)
    small = search.search("Which hosts have critical alerts?", sample=500)
    assert (len(small.results), small.has_more) == (30, False)


def test_a_sample_stops_reading_pages_early():
    search, calls = _search(pages=True)
    result = search.search("Show me the alerts with critical severity", sample=5)
    assert len(result.results) == 5 and result.has_more
    assert calls["pages"] == 2 and not result.data.complete  # 6 critical rows needed: 2 pages of 6, not all 6 pages


def test_every_row_from_the_data_already_read():
    search, calls = _search()
    result = search.search("Which hosts have critical alerts?", sample=5)
    assert result.data.complete and calls["fetch"] == 1
    everything = search.fetch_all(result)
    assert calls["fetch"] == 1 and everything.reused_data is True  # nothing read again
    assert (len(everything.results), everything.has_more, everything.sample) == (30, False, None)
    assert len(result.results) == 5  # the sampled answer is untouched


def test_every_row_reads_again_when_the_sample_stopped_early():
    search, calls = _search(pages=True)
    result = search.search("Show me the alerts with critical severity", sample=5)
    everything = search.fetch_all(result)
    assert everything.reused_data is False and len(everything.results) == 30 and calls["pages"] == 2 + 6


def test_more_columns_come_from_the_rows_already_read():
    search, calls = _search(pages=True)
    result = search.search("Which hosts have critical alerts?", sample=5)
    assert all(o["kept"] for o in search.column_options(result))  # every catalog column of the rows read
    pages = calls["pages"]
    wider = search.with_columns(result, ["alerts.rule"])
    assert calls["pages"] == pages and wider.reused_data is True
    assert list(wider.results.columns) == ["ip", "rule"] and len(wider.results) == 5 and wider.sample == 5
    assert wider.has_more and wider.data is result.data
    everything = search.fetch_all(wider)
    assert list(everything.results.columns) == ["ip", "rule"] and len(everything.results) > 5


def test_released_data_is_read_again():
    search, calls = _search(keep_answers=0)
    result = search.search("Which hosts have critical alerts?", sample=5)
    assert result.data.released and not any(o["kept"] for o in search.column_options(result))
    wider = search.with_columns(result, ["alerts.rule"])
    assert wider.reused_data is False and calls["fetch"] == 2


def test_counts_are_never_sampled():
    search, _ = _search()
    result = search.search("How many alerts by rule?", sample=1)
    if result.status == "needs_clarification":
        result = search.search("How many alerts by rule?", sample=1, pinned={"answer_shape": "count_by"})
    assert result.sample is None and len(result.results) == 3 and not result.has_more


def test_a_sample_is_a_number_of_rows():
    search, _ = _search()
    for bad in (0, -1, 1.5, "10", True, 10**9):
        with pytest.raises(ValueError, match="sample"):
            search.search("Which hosts have critical alerts?", sample=bad)
    with pytest.raises(ValueError, match="sample"):
        SemanticSearch(Catalog.model_validate(CATALOG), DuckAPI(), sample=0)


def test_the_web_app():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.server import create_app

    search, calls = _search(pages=True)
    client = TestClient(create_app(lambda: search, store=None))
    first = client.post("/api/ask", json={"question": "Show me the alerts with critical severity", "sample": 5}).json()
    assert first["result"]["sample"] == 5 and first["result"]["has_more"] and not first["result"]["data_complete"]
    assert len(first["result"]["results"]) == 5
    job = client.post("/api/all", json={"conversation_id": first["conversation_id"], "background": True})
    assert job.status_code == 202
    job_id = job.json()["job_id"]
    end = time.time() + 5
    while client.get(f"/api/jobs/{job_id}").json()["state"] != "done" and time.time() < end:
        time.sleep(0.02)
    done = client.get(f"/api/jobs/{job_id}").json()
    assert len(done["result"]["result"]["results"]) == 30 and not done["result"]["result"]["has_more"]
    assert any(e["stage"] == "fetching" for e in done["events"])
    again = client.post("/api/columns", json={"conversation_id": first["conversation_id"],
                                              "columns": ["alerts.severity"]}).json()
    assert again["result"]["reused_data"] is True and len(again["result"]["results"]) == 30
    assert client.post("/api/ask", json={"question": "hi", "sample": 0}).status_code == 400
    everything = client.post("/api/ask", json={"question": "Which hosts have critical alerts?", "sample": None}).json()
    assert everything["result"]["sample"] is None and len(everything["result"]["results"]) == 30


def test_a_distinct_sample_stops_reading_too():
    """"Which hosts…" is DISTINCT and sorted: a sample still stops once it has enough different hosts."""
    search, calls = _search(pages=True)
    result = search.search("Which hosts have critical alerts?", sample=5)
    assert len(result.results) == 5 and result.has_more and calls["pages"] == 2 and not result.data.complete
    everything = search.fetch_all(result)  # the full answer, sorted over every row
    assert len(everything.results) == 30 and list(everything.results["ip"]) == sorted(everything.results["ip"])


def test_a_sample_of_a_big_api_is_one_request():
    """The NVD case: "Give me a sample of vulnerabilities" must not page through every CVE (6 s apart)."""
    from test_flags_and_negation import NOW, SilentLLM
    from test_public_apis import LOG4SHELL, FakeNVD

    from duckduck import NVD
    from duckduck.semantic import CatalogGenerator

    items = [{"cve": {**LOG4SHELL["cve"], "id": f"CVE-2026-{n:04d}"}} for n in range(300)]
    nvd = NVD(sleep=lambda s: None)
    fake = FakeNVD(items)
    nvd.session.get = fake
    duck = DuckAPI()
    duck.register_api_function("nvd_cves", nvd.cves)
    duck.register_streaming_function("nvd_cves", nvd.iter_cves)
    catalog = CatalogGenerator(SilentLLM(), duck, clock=lambda: NOW).generate().catalog
    fake.urls.clear()
    search = SemanticSearch(catalog, duck, clock=lambda: NOW)
    result = search.search("Give me a sample of vulnerabilities", sample=10)
    assert result.status == "ok", result.report()
    assert len(result.results) == 10 and result.has_more and result.sql.rstrip().endswith("LIMIT 10")
    assert len(fake.urls) == 1 and "resultsPerPage=11" in fake.urls[0]


def test_a_rate_limit_wait_stops_for_a_cancel():
    from duckduck.progress import Cancelled, Progress, tracking, wait

    slept, progress = [], Progress()

    def sleep(s):
        slept.append(s)
        if len(slept) == 3:
            progress.cancel()

    with tracking(progress), pytest.raises(Cancelled):
        wait(6.0, sleep)
    assert len(slept) == 3  # 0.75 s of the 6, not all of it
    wait(1.0, slept.append)  # no progress in context: one plain sleep
    assert slept[-1] == 1.0

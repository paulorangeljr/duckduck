"""Pages read several at a time (``slicing.parallel``), bounded by what the table declares; a join's calls too."""

import threading
import time

import pandas as pd

from duckduck import DuckAPI, progress, slicing
from duckduck.sparkplan import spark_plan


class Paged:
    """An API that reads any page on its own and reports the total — 4 requests at a time allowed."""

    def __init__(self, rows=10, page_size=2, delay=0.05):
        self.rows, self.page_size, self.delay = rows, page_size, delay
        self.requested, self.in_flight, self.most = [], 0, 0
        self.lock = threading.Lock()

    def _page(self, n):
        progress.checkpoint()  # what every connector's PageProgress.page does
        with self.lock:
            self.requested.append(n)
            self.in_flight += 1
            self.most = max(self.most, self.in_flight)
        time.sleep(self.delay)
        with self.lock:
            self.in_flight -= 1
        start = n * self.page_size
        return [{"id": i} for i in range(start, min(start + self.page_size, self.rows))], self.rows

    @spark_plan("partitioned", by="pages", max_parallel=3, why="page=N with a total; 3 at a time")
    def items(self, limit=None):
        return [r for page in slicing.pages(self._page, self.page_size) for r in page]

    def iter_items(self):
        for page in slicing.pages(self._page, self.page_size):
            yield pd.DataFrame(page)


def test_pages_are_read_up_to_the_declared_number_at_once_and_kept_in_order():
    api, duck = Paged(), DuckAPI(stream_pages=False)
    duck.register_api_function("items", api.items)
    warm = Paged(delay=0)  # a first sql() pays a one-off warm-up (~0.1 s) that isn't what's timed here
    duck.register_api_function("warm", warm.items)
    duck.sql("SELECT id FROM warm").fetchall()
    started = time.perf_counter()
    assert [r[0] for r in duck.sql("SELECT id FROM items").fetchall()] == list(range(10))
    assert api.most == 3 and sorted(api.requested) == [0, 1, 2, 3, 4]  # 5 pages, never more at once than declared
    assert time.perf_counter() - started < 5 * api.delay  # page 0, then two rounds — not five in a row


def test_off_or_undeclared_means_one_request_after_another():
    api = Paged()
    duck = DuckAPI(stream_pages=False, parallel=False)
    duck.register_api_function("items", api.items)
    duck.sql("SELECT * FROM items").fetchall()
    assert api.most == 1 and api.requested == [0, 1, 2, 3, 4]
    plain, calls = Paged(), DuckAPI(stream_pages=False)
    calls.register_api_function("plain", lambda limit=None: [r for p in slicing.pages(plain._page, 2) for r in p])
    calls.sql("SELECT * FROM plain").fetchall()
    assert plain.most == 1  # nothing declared: the API's limit is unknown


def test_page_by_page_reads_prefetch_too_and_stop_early():
    api, duck = Paged(rows=40, delay=0.02), DuckAPI()
    duck.register_api_function("items", api.items)
    duck.register_streaming_function("items", api.iter_items)
    rows = duck.sql("SELECT id FROM items WHERE id < 3 LIMIT 2").fetchall()  # the WHERE isn't the API's: pages
    assert [r[0] for r in rows] == [0, 1]
    time.sleep(0.1)
    assert len(api.requested) < 20  # stopped early: pages not yet started were cancelled


def test_cancel_reaches_pages_running_in_threads():
    api = Paged(rows=20, delay=0.05)
    p = progress.Progress()
    seen = []

    def read():
        with progress.tracking(p), slicing.parallel(3):
            try:
                for page in slicing.pages(api._page, 2):
                    seen.append(page)
                    p.cancel()
            except progress.Cancelled:
                seen.append("cancelled")

    read()
    assert len(seen) <= 2


def test_a_join_s_calls_run_several_at_once():
    duck, running, most = DuckAPI(), [0], [0]
    lock = threading.Lock()

    class Vulns:
        @spark_plan("partitioned", by="pages", max_parallel=4, why="4 at a time")
        def of(self, asset_id: int, limit=None):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1
            return [{"cve": f"CVE-{asset_id}"}]

    duck.register_api_function("assets", lambda limit=None: [{"id": i} for i in range(8)])
    duck.register_api_function("vulns", Vulns().of)
    rows = duck.sql("SELECT a.id, v.cve FROM assets a JOIN vulns v ON v.asset_id = a.id ORDER BY 1").fetchall()
    assert len(rows) == 8 and rows[0] == (0, "CVE-0") and most[0] == 4


def test_a_server_capping_its_pages_below_the_page_size_fails_instead_of_skipping_rows():
    import pytest

    class Capped(Paged):
        def _page(self, n):  # asked for 2 rows a page, the server sends 1 — but says there are 10
            rows, total = super()._page(n)
            return rows[:1], total

    api = Capped(delay=0)
    for read in (lambda: list(slicing.pages(api._page, 2)),
                 lambda: _parallel(api)):
        with pytest.raises(slicing.PageCapError, match='caps a page at 1.*"default_page_size" to 1'):
            read()
    with slicing.probing(), pytest.raises(slicing.PageCapError):  # Spark's probe says so before splitting
        list(slicing.pages(api._page, 2))
    assert list(slicing.pages(Paged(delay=0)._page, 2))  # a full first page is fine


def _parallel(api):
    with slicing.parallel(3):
        return list(slicing.pages(api._page, 2))


def test_the_service_s_max_parallel_wins_over_the_declared_one():
    api = Paged(rows=20)
    api.max_parallel = 6  # what auto_register sets from the service's "max_parallel" in duckduck.json
    duck = DuckAPI(stream_pages=False)
    duck.register_api_function("items", api.items)
    duck.sql("SELECT * FROM items").fetchall()
    assert api.most == 6  # 10 pages: page 0, then 6 at a time (the table declares 3)
    api.max_parallel, api.most = 1, 0
    duck.sql("SELECT * FROM items").fetchall()
    assert api.most == 1


def test_auto_register_takes_max_parallel_per_service(tmp_path):
    import pytest

    from duckduck import servicenow

    duck = DuckAPI()
    instances = duck.auto_register({"sn": {"connector": "servicenow", "instance": "x", "max_parallel": 8,
                                           "authentication": {"username": "u", "password": "p"}}})
    assert instances["sn"].max_parallel == 8 and isinstance(instances["sn"], servicenow.ServiceNow)
    assert duck._parallel_of("sn_incidents") == 8
    with pytest.raises(ValueError, match="max_parallel is how many requests run at once"):
        DuckAPI().auto_register({"sn": {"connector": "servicenow", "instance": "x", "max_parallel": 0,
                                        "authentication": {"username": "u", "password": "p"}}})

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

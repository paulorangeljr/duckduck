"""A source that can't be capped at the source is read page by page, keeping only the rows that pass."""

import pandas as pd
import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, SemanticSearch  # noqa: E402
from duckduck.semantic.executor import ExecutionError  # noqa: E402

from test_answer_shapes import CATALOG  # noqa: E402

PAGES = [
    [{"ip": f"10.0.{p}.{i}", "rule": "malware" if i % 5 == 0 else "brute_force",
      "severity": "critical" if i % 5 == 0 else "low"} for i in range(100)]
    for p in range(4)
]
OWNERS = [{"ip": "10.0.0.0", "owner": "ana", "department": "ops"},
          {"ip": "10.0.1.6", "owner": "bob", "department": "dev"}]


class Alerts:
    """The same alerts, whole (``fetch``) or one page at a time (``iter``) — neither takes a filter."""

    def __init__(self):
        self.whole_calls, self.pages_read = 0, 0

    def fetch(self, limit=None):
        self.whole_calls += 1
        return [row for page in PAGES for row in page]

    def iter(self):
        for page in PAGES:
            self.pages_read += 1
            yield pd.DataFrame(page)


def _search(**kw):
    alerts = Alerts()
    duck = DuckAPI()
    duck.register_api_function("alerts", alerts.fetch)
    duck.register_streaming_function("alerts", alerts.iter)
    duck.register_api_function("owners", lambda limit=None: OWNERS)
    return SemanticSearch(Catalog.model_validate(CATALOG), duck, **kw), alerts


def test_a_filter_the_api_cannot_take_is_applied_page_by_page():
    search, alerts = _search()
    result = search.search("How many critical alerts are there?")
    assert result.status == "ok", result.report()
    assert result.results.iloc[0, 0] == 80 and alerts.whole_calls == 0 and alerts.pages_read == 4
    fetch = next(f for f in result.fetches if f.source == "alerts")
    assert fetch.streamed and fetch.pages == 4 and fetch.rows_scanned == 400 and fetch.rows == 80
    assert fetch.residual_filters == ["alerts.severity"] and not fetch.limit_pushed


def test_enough_rows_stops_the_paging():
    search, alerts = _search()
    search.planner.default_limit = 25
    result = search.search("Show me the critical alerts")
    assert result.status == "ok" and len(result.results) == 25
    assert alerts.pages_read == 2  # 20 critical per page: two pages give 25, the rest are never asked for


def test_joins_run_over_the_kept_rows():
    search, alerts = _search()
    result = search.search("Which owners have critical alerts?", pinned={"source:alerts": True, "source:owners": True})
    assert result.status == "ok", result.report()
    assert sorted(result.results.iloc[:, 0]) == ["ana"]  # 10.0.0.0 is critical; 10.0.1.6 is not
    assert next(f for f in result.fetches if f.source == "alerts").streamed


def test_without_streaming_the_whole_result_is_fetched():
    search, alerts = _search(stream=False)
    result = search.search("How many critical alerts are there?")
    assert result.results.iloc[0, 0] == 80 and alerts.whole_calls == 1 and alerts.pages_read == 0


def test_a_column_the_catalog_expects_but_no_page_has_is_an_error():
    search, alerts = _search()
    alerts.iter = lambda: iter([pd.DataFrame([{"ip": "1.1.1.1", "rule": "x"}])])
    search.duck.register_streaming_function("alerts", alerts.iter)
    with pytest.raises(ExecutionError, match="out of sync"):
        search.search("How many critical alerts are there?")


def test_no_temp_table_is_left_behind():
    search, _ = _search()
    search.search("How many critical alerts are there?")
    left = search.duck.conn.sql("SELECT table_name FROM duckdb_tables() WHERE table_name LIKE '_sem_%'").df()
    assert left.empty

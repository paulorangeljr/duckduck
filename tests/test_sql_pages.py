"""DuckAPI.sql(): a table whose LIMIT can't reach the source is read page by page, keeping only what the query uses."""

import pandas as pd

from duckduck import DuckAPI

PAGES = [[{"id": p * 100 + i, "host": f"h{p}-{i}", "severity": "critical" if i % 10 == 0 else "low",
           "noise": "x" * 50} for i in range(100)] for p in range(5)]


class Assets:
    def __init__(self):
        self.whole, self.pages_read, self.closed = 0, 0, False

    def fetch(self, limit=None):  # no filters: whatever the WHERE says stays with DuckDB
        self.whole += 1
        rows = [r for page in PAGES for r in page]
        return rows[:limit] if limit else rows

    def iter(self):
        try:
            for page in PAGES:
                self.pages_read += 1
                yield pd.DataFrame(page)
        finally:
            self.closed = True


def _duck(**kw):
    assets = Assets()
    duck = DuckAPI(**kw)
    duck.register_api_function("assets", assets.fetch)
    duck.register_streaming_function("assets", assets.iter)
    duck.register_api_function("owners", lambda limit=None: [{"host": "h0-0", "owner": "ana"},
                                                             {"host": "h1-5", "owner": "bob"}])
    return duck, assets


def test_a_filter_the_source_cant_take_is_applied_per_page():
    duck, assets = _duck()
    out = duck.sql("SELECT count(*) AS n FROM assets WHERE severity = 'critical'").df()
    assert out["n"][0] == 50 and assets.whole == 0 and assets.pages_read == 5


def test_only_the_columns_the_query_uses_are_kept(caplog):
    duck, _ = _duck()
    caplog.set_level("INFO", logger="duckduck")
    out = duck.sql("SELECT host FROM assets WHERE severity = 'critical' ORDER BY id").df()
    assert list(out.columns) == ["host"] and len(out) == 50
    kept = next(r.message for r in caplog.records if "kept" in r.message)
    assert "kept 50 of 500 rows from 5 page(s), 3 column(s)" in kept  # host, severity, id — never noise


def test_select_star_keeps_every_column():
    duck, _ = _duck()
    out = duck.sql("SELECT * FROM assets WHERE severity = 'critical'").df()
    assert set(out.columns) == {"id", "host", "severity", "noise"} and len(out) == 50


def test_limit_stops_the_paging_when_it_cant_change_the_answer():
    duck, assets = _duck()
    out = duck.sql("SELECT host FROM assets WHERE severity = 'critical' LIMIT 15").df()
    assert len(out) == 15 and assets.pages_read == 2 and assets.closed  # 10 critical per page
    duck, assets = _duck()
    duck.sql("SELECT host FROM assets WHERE severity = 'critical' ORDER BY id DESC LIMIT 15").df()
    assert assets.pages_read == 5  # ORDER BY: every page is needed


def test_a_limit_the_source_takes_is_still_one_capped_request():
    duck, assets = _duck()
    out = duck.sql("SELECT * FROM assets LIMIT 7").df()
    assert len(out) == 7 and assets.whole == 1 and assets.pages_read == 0


def test_joins_filter_each_side_by_its_own_conditions():
    duck, assets = _duck()
    out = duck.sql("""SELECT a.host, o.owner FROM assets a JOIN owners o ON a.host = o.host
                      WHERE a.severity = 'critical'""").df()
    assert out.to_dict("records") == [{"host": "h0-0", "owner": "ana"}] and assets.pages_read == 5


def test_nothing_kept_is_an_empty_table_the_query_still_runs():
    duck, _ = _duck()
    assert duck.sql("SELECT host FROM assets WHERE severity = 'none'").df().empty


def test_off_fetches_it_whole():
    duck, assets = _duck(stream_pages=False)
    assert duck.sql("SELECT count(*) AS n FROM assets WHERE severity = 'critical'").df()["n"][0] == 50
    assert assets.whole == 1 and assets.pages_read == 0


def test_the_sql_tab_reads_page_by_page_too():
    from duckduck.semantic.admin import SQLConsole

    duck, assets = _duck()
    out = SQLConsole(duck).run("SELECT count(*) AS n FROM assets WHERE severity = 'critical'")
    assert out.get("error") is None and out["rows"] == [[50]]
    assert assets.whole == 0 and assets.pages_read == 5
    assert any("kept 50 of 500 rows" in line for line in out["log"])  # shown under the result

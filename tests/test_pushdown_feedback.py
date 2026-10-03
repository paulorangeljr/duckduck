"""Telling the user what reaches each source: DuckAPI.explain (nothing read), EXPLAIN PUSHDOWN, last_pushdown,
the SQL console's result and /api/sql/explain."""

import logging

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.common.pushdown import sortable


class Runs:
    def __init__(self):
        self.calls = []
        self.data = pd.DataFrame({"dag_id": ["a", "b", "a"], "state": ["ok", "failed", "ok"],
                                  "run_date": ["2026-09-10", "2026-09-11", "2026-09-12"], "paused": [True, False, False]})

    def runs(self, dag_id=None, state=None, run_date_gte=None, paused=None, limit=None):
        self.calls.append(dict(dag_id=dag_id, state=state, run_date_gte=run_date_gte, paused=paused, limit=limit))
        return self.data


@pytest.fixture
def duck():
    src = Runs()
    d = DuckAPI()
    d.register_api_function("runs", src.runs)
    d.src = src
    return d


def test_explain_says_what_reaches_the_source_and_reads_nothing(duck):
    e = duck.explain("SELECT * FROM runs WHERE dag_id = 'a' AND run_date >= '2026-09-11' LIMIT 5")
    assert duck.src.calls == []  # planned, never called
    (call,) = e.calls
    assert call["reads"] == "only what the query needs" and e.ok
    assert "✓ dag_id = 'a' → dag_id" in e.report() and "LIMIT 5 → limit" in e.report()


def test_a_condition_with_no_parameter_is_a_warning_naming_it(duck):
    e = duck.explain("SELECT * FROM runs WHERE run_date = '2026-09-11' LIMIT 1")
    assert not e.ok and e.calls[0]["reads"] == "every row"
    assert "run_date = '2026-09-11'" in e.warnings[0] and "no 'run_date' parameter" in e.warnings[0]
    assert "LIMIT 1 — not every WHERE condition reached the source" in e.warnings[0]


def test_what_only_duckdb_can_apply_is_named(duck):
    e = duck.explain("SELECT * FROM runs WHERE state IS NULL OR dag_id = 'a'")
    assert "state IS NULL OR dag_id = 'a' — DuckDB only" in e.report()


def test_a_boolean_column_is_a_condition(duck):
    duck.sql("SELECT * FROM runs WHERE paused").df()
    assert duck.src.calls[-1]["paused"] is True
    duck.sql("SELECT * FROM runs WHERE NOT paused").df()
    assert duck.src.calls[-1]["paused"] is False


def test_order_by_without_limit_says_every_row_is_read(duck):
    e = duck.explain("SELECT * FROM runs ORDER BY run_date")
    assert "ORDER BY without LIMIT" in e.report() and e.ok  # not a warning: the answer is every row


def test_explain_pushdown_in_sql_and_last_pushdown_after_a_run(duck, caplog):
    df = duck.sql("EXPLAIN PUSHDOWN SELECT * FROM runs WHERE state = 'ok'").df()
    assert list(df.columns) == ["table", "call", "how", "reads", "pushed", "decision"]
    assert df.loc[0, "pushed"] == "yes" and duck.src.calls == []
    with caplog.at_level(logging.WARNING, logger="duckduck"):
        duck.sql("SELECT * FROM runs WHERE run_date = '2026-09-11'").df()
    assert duck.last_pushdown.ran and duck.last_pushdown.warnings
    assert any("runs reads every row" in r.message for r in caplog.records)
    duck.pushdown_warnings = False
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="duckduck"):
        duck.sql("SELECT * FROM runs WHERE run_date = '2026-09-11'").df()
    assert not caplog.records


def test_a_join_shows_the_values_it_will_be_narrowed_by(duck):
    e = duck.explain("SELECT * FROM runs r JOIN runs s ON s.dag_id = r.dag_id WHERE r.state = 'failed'")
    assert "narrowed when it runs" in e.report() and duck.src.calls == []


def test_the_sql_console_returns_it_and_explains_without_running(duck):
    from duckduck.semantic.admin import SQLConsole

    console = SQLConsole(duck)
    r = console.run("SELECT * FROM runs WHERE run_date = '2026-09-11'")
    assert r["pushdown"]["warnings"] and r["pushdown"]["ran"]
    before = len(duck.src.calls)
    x = console.explain("SELECT * FROM runs WHERE state = 'ok' LIMIT 2")
    assert x["pushdown"]["ok"] and len(duck.src.calls) == before
    assert "error" in console.explain("DELETE FROM runs")


def test_paged_reads_say_when_a_limit_stops_the_pages():
    class Paged:
        def items(self, limit=None):
            return pd.DataFrame({"n": range(10)})

        @sortable("n")
        def iter_items(self, order_by=None):
            for k in range(0, 10, 3):
                yield pd.DataFrame({"n": range(k, min(k + 3, 10))})

    src = Paged()
    d = DuckAPI()
    d.register_api_function("items", lambda limit=None: src.items(limit))
    d.register_streaming_function("items", src.iter_items)
    text = d.explain("SELECT * FROM items WHERE n > 2 LIMIT 4").report()
    assert "LIMIT 4 — pages stop once 4 rows are kept" in text
    assert "the top 2 by n are read" in d.explain("SELECT * FROM items ORDER BY n DESC LIMIT 2").report()

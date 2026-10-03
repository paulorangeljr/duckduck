"""Batched reads (run_pipeline(batch_rows=…)) and resuming a failed run without duplicates (IDEMPOTENCY)."""

import datetime as dt
import glob
import json
import os
import sqlite3

import duckdb
import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.common import batches
from duckduck.common.idempotency import Idempotency, checkpoint, idempotency_of, restart
from duckduck.common.pushdown import sortable
from duckduck.connectors.registry import SERVICE_REGISTRY
from duckduck.pipeline import PipelineError, load_spec, run_pipeline
from duckduck.pipeline import runner

NOW = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)


def test_every_connector_says_whether_it_can_resume():
    for service, spec in SERVICE_REGISTRY.items():
        cls = spec.factory.__self__
        idem = getattr(cls, "IDEMPOTENCY", None)
        assert isinstance(idem, Idempotency), f"{service}: declare IDEMPOTENCY (checkpoint / restart + why)"
        assert idem.why
        if idem.resumes and idem.column:  # a default column must be one its tables sort by
            for table, method in spec.streaming_tables.items():
                columns = getattr(getattr(cls, method), "__duckduck_sortable__", None)
                if columns is not None and columns.columns is not None and table not in ("table",):
                    assert idem.column in columns.columns or service == "airflow", (service, table)


def test_idempotency_is_checked():
    with pytest.raises(ValueError, match="mode"):
        Idempotency("sometimes", "why")
    with pytest.raises(ValueError, match="why"):
        Idempotency("restart", "")


class Paged:
    """An API whose pages sort by ``n`` and that can ask for ``n > x``: a checkpoint source."""

    IDEMPOTENCY = checkpoint("pages sort by n and n_gt reaches the API", column="n")

    def __init__(self, rows=23, page=3, sort=True):
        self.data = [{"n": i // 2, "v": f"x{i}"} for i in range(rows)]  # ties on n: two rows per value
        self.page, self.sort, self.calls, self.fail_after = page, sort, [], None

    def items(self, limit=None):
        return pd.DataFrame(self.data[:limit])

    @sortable("n")
    def iter_items(self, n_gt=None, order_by=None):
        self.calls.append({"n_gt": n_gt, "order_by": order_by})
        rows = [r for r in self.data if n_gt is None or r["n"] > n_gt]
        if not self.sort:
            rows = rows[::-1]
        for k, start in enumerate(range(0, len(rows), self.page)):
            if self.fail_after is not None and k >= self.fail_after:
                self.fail_after = None  # fails once
                raise TimeoutError("the API timed out")
            yield pd.DataFrame(rows[start:start + self.page])


class Cursor(Paged):
    IDEMPOTENCY = restart("cursor pages: nothing to resume after")

    @sortable("n")
    def iter_items(self, n_gt=None, order_by=None):
        yield from Paged.iter_items(self, n_gt, order_by)


def duck_of(source):
    d = DuckAPI()
    d.register_api_function("items", source.items)
    d.register_streaming_function("items", source.iter_items)
    return d


def spec(tmp_path, **more):
    return {"pipeline": "p", "sql": "SELECT n, v FROM items", "state": str(tmp_path / "state"),
            "target": {"path": str(tmp_path / "out"), "mode": "append"}, "audit_columns": ["_run_id"], **more}


def written(tmp_path):
    return duckdb.sql(f"SELECT v FROM read_parquet('{tmp_path}/out/*.parquet') ORDER BY v").df()["v"].tolist()


def state(tmp_path):
    with open(tmp_path / "state" / "p.json") as f:
        return json.load(f)


def test_a_batched_run_writes_what_an_in_memory_one_does_and_cleans_up(tmp_path):
    src = Paged()
    run = run_pipeline(spec(tmp_path, batch_rows=4), duck=duck_of(src), now=NOW)
    assert written(tmp_path) == sorted(r["v"] for r in src.data)
    assert run.batch_rows == 4 and run.staging and not os.path.exists(run.staging)  # removed after success
    assert any("rows in" in b and "batch(es)" in b for b in run.batches)
    assert "batches of 4 rows" in run.report()
    assert src.calls == [{"n_gt": None, "order_by": [("n", False)]}]  # sorted by the resume column
    assert "pending" not in state(tmp_path)


def test_the_batches_are_on_disk_never_one_frame(tmp_path, monkeypatch):
    src = Paged(rows=40, page=5)
    sizes = []
    original = DuckAPI._materialize_batches

    def spy(self, *a, **k):
        out = original(self, *a, **k)
        staged = glob.glob(str(tmp_path / "stage" / "**" / "batch-*.parquet"), recursive=True)
        sizes.extend(duckdb.sql(f"SELECT count(*) FROM '{f}'").fetchone()[0] for f in staged)
        return out

    monkeypatch.setattr(DuckAPI, "_materialize_batches", spy)
    run_pipeline(spec(tmp_path, batch_rows=8, staging=str(tmp_path / "stage")), duck=duck_of(src), now=NOW)
    assert len(sizes) >= 4 and sum(sizes) == 40
    assert max(sizes) <= 8 + 5  # a batch: its rows, plus at most the rows tied on the last value, plus a page


def test_a_failed_read_resumes_after_the_last_batch_with_the_same_run(tmp_path):
    src = Paged()
    src.fail_after = 4  # pages 0-3 read (12 rows), then a timeout
    duck = duck_of(src)
    with pytest.raises(TimeoutError):
        run_pipeline(spec(tmp_path, batch_rows=4), duck=duck, now=NOW)
    pending = state(tmp_path)["pending"]
    assert pending["written"] == [] and os.path.isdir(pending["staging"])
    later = NOW + dt.timedelta(days=1)
    run = run_pipeline(spec(tmp_path, batch_rows=4), duck=duck, now=later)
    assert run.resumed and run.run_id == pending["run_id"]
    assert run.run_at.startswith("2026-10-01")  # the failed run's clock, not today's
    after = src.calls[-1]["n_gt"]
    assert after is not None and after >= 4  # only what's after the batches already on disk
    assert written(tmp_path) == sorted(r["v"] for r in src.data)  # every row once
    assert any("resuming after n" in b for b in run.batches)
    assert "pending" not in state(tmp_path) and state(tmp_path)["last_run_id"] == run.run_id


def test_a_source_that_cant_resume_is_read_again_from_the_start(tmp_path):
    src = Cursor()
    src.fail_after = 3
    duck = duck_of(src)
    with pytest.raises(TimeoutError):
        run_pipeline(spec(tmp_path, batch_rows=4), duck=duck, now=NOW)
    run = run_pipeline(spec(tmp_path, batch_rows=4), duck=duck, now=NOW)
    assert src.calls[-1] == {"n_gt": None, "order_by": None}
    assert any("from the start" in b for b in run.batches)
    assert written(tmp_path) == sorted(r["v"] for r in src.data)


def test_pages_that_arent_sorted_turn_the_checkpoint_off(tmp_path):
    src = Paged(sort=False)
    src.fail_after = 4
    duck = duck_of(src)
    with pytest.raises(TimeoutError):
        run_pipeline(spec(tmp_path, batch_rows=4), duck=duck, now=NOW)
    run = run_pipeline(spec(tmp_path, batch_rows=4), duck=duck, now=NOW)
    assert src.calls[-1]["n_gt"] is None  # no checkpoint to trust: read again
    assert written(tmp_path) == sorted(r["v"] for r in src.data)


def test_a_run_that_failed_after_writing_skips_what_it_wrote(tmp_path, monkeypatch):
    src = Paged()
    duck = duck_of(src)
    two = spec(tmp_path, batch_rows=4, sql="CREATE VIEW a AS SELECT n, v FROM items; "
                                            "CREATE VIEW b AS SELECT v FROM a WHERE n < 3",
               targets={"a": {"path": str(tmp_path / "out"), "mode": "append"},
                        "b": {"path": str(tmp_path / "small"), "mode": "append"}})
    del two["target"]
    real = runner.write_target
    calls = {"n": 0}

    def flaky(engine, target, *a, **k):
        calls["n"] += 1
        if target.view == "b" and calls["n"] == 2:
            raise OSError("the bucket said no")
        return real(engine, target, *a, **k)

    monkeypatch.setattr(runner, "write_target", flaky)
    with pytest.raises(OSError):
        run_pipeline(two, duck=duck, now=NOW)
    assert state(tmp_path)["pending"]["written"] == [str(tmp_path / "out")]
    reads = len(src.calls)
    run = run_pipeline(two, duck=duck, now=NOW)
    assert len(src.calls) == reads  # the source was read completely before: taken from its batches
    assert any(w.get("already_written") for w in run.writes)
    assert written(tmp_path) == sorted(r["v"] for r in src.data)  # not appended twice
    small = duckdb.sql(f"SELECT count(*) FROM '{tmp_path}/small/*.parquet'").fetchone()[0]
    assert small == 6


def test_a_resumed_append_replaces_the_files_its_failed_attempt_wrote(tmp_path):
    src = Paged()
    duck = duck_of(src)
    out = tmp_path / "out"
    out.mkdir()
    # what a write that died half-way left: one of this run's files
    duckdb.sql(f"COPY (SELECT 'stale' AS v, 0 AS n) TO '{out}/part-r1-0.parquet' (FORMAT parquet)")
    (tmp_path / "state").mkdir()
    with open(tmp_path / "state" / "p.json", "w") as f:
        json.dump({"pending": {"run_id": "r1", "now": NOW.isoformat(), "written": [], "staging": None}}, f)
    run = run_pipeline(spec(tmp_path), duck=duck)
    assert run.resumed and run.run_id == "r1"
    assert "stale" not in written(tmp_path)


def test_resume_false_starts_a_new_run_and_a_given_run_id_wins(tmp_path):
    src = Paged()
    src.fail_after = 2
    duck = duck_of(src)
    with pytest.raises(TimeoutError):
        run_pipeline(spec(tmp_path, batch_rows=4), duck=duck, now=NOW)
    failed = state(tmp_path)["pending"]["run_id"]
    run = run_pipeline(spec(tmp_path, batch_rows=4), duck=duck, now=NOW, run_id="fresh")
    assert not run.resumed and run.run_id == "fresh"
    assert "pending" not in state(tmp_path)
    src.fail_after = 2
    with pytest.raises(TimeoutError):
        run_pipeline(spec(tmp_path, batch_rows=4), duck=duck, now=NOW)
    run = run_pipeline(spec(tmp_path, batch_rows=4), duck=duck, now=NOW, resume=False)
    assert not run.resumed and run.run_id != failed


def test_a_named_resume_column_the_source_cant_sort_by_restarts(tmp_path):
    src = Paged()
    src.fail_after = 3
    duck = duck_of(src)
    with pytest.raises(TimeoutError):
        run_pipeline(spec(tmp_path, batch_rows=4, resume={"column": "v"}), duck=duck, now=NOW)
    run = run_pipeline(spec(tmp_path, batch_rows=4, resume={"column": "v"}), duck=duck, now=NOW)
    assert src.calls[-1]["n_gt"] is None and src.calls[-1]["order_by"] is None
    assert written(tmp_path) == sorted(r["v"] for r in src.data)


@pytest.mark.parametrize("data, message", [
    ({"batch_rows": 0}, "batch_rows"),
    ({"batch_rows": "10"}, "batch_rows"),
    ({"staging": ""}, "staging"),
    ({"resume": {"col": "x"}}, "resume"),
    ({"resume": "yes"}, "resume"),
])
def test_the_file_says_what_to_fix(data, message):
    with pytest.raises(PipelineError, match=message):
        load_spec({"pipeline": "p", "sql": "SELECT 1 AS a", **data})


def test_batch_rows_is_checked_on_the_call(tmp_path):
    with pytest.raises(PipelineError, match="batch_rows"):
        run_pipeline(spec(tmp_path), duck=duck_of(Paged()), batch_rows=0)


def test_sql_tables_stream_from_a_server_side_cursor_and_resume(tmp_path):
    from duckduck.connectors.databases.sql import SQLDatabase

    path = tmp_path / "x.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    c.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"n{i}") for i in range(25)])
    c.commit()
    db = SQLDatabase(f"sqlite:///{path}")
    assert [len(x) for x in db.iter_table("t", chunksize=10)] == [10, 10, 5]
    assert db.iter_table.__func__.__duckduck_sortable__.exact
    assert list(next(db.iter_table("t", order_by=[("id", True)], chunksize=3))["id"]) == [24, 23, 22]
    d = DuckAPI()
    d.register_api_function("t", lambda where=None, order_by=None, limit=None: db.table("t", where, order_by, limit))
    d.register_streaming_function("t", lambda where=None, order_by=None: db.iter_table("t", where, order_by, 4))
    with batches.batching(batches.Batching(rows=5, folder=str(tmp_path / "b"))) as b:
        out = d.sql("SELECT * FROM t WHERE id >= 5").df()
    assert len(out) == 20 and any("20 rows" in n for n in b.notes)


def test_local_files_read_in_batches_through_the_scan(tmp_path):
    from duckduck.connectors.local.files import LocalFiles

    folder = tmp_path / "data"
    folder.mkdir()
    duckdb.sql(f"COPY (SELECT range AS id, range % 3 AS g FROM range(30)) TO '{folder}/events.parquet'")
    files = LocalFiles(str(folder))
    assert idempotency_of(files.table_functions()["events"]).resumes
    d = DuckAPI()
    d.auto_register({"lake": {"connector": "files", "path": str(folder), "table_prefix": ""}})
    whole = d.sql("SELECT * FROM events WHERE g = 1").df()  # outside a batched run: one scan, as before
    with batches.batching(batches.Batching(rows=4, folder=str(tmp_path / "b"), column="id")) as b:
        out = d.sql("SELECT * FROM events WHERE g = 1").df()
    assert sorted(out["id"]) == sorted(whole["id"]) and len(out) == 10
    assert any("batch(es)" in n for n in b.notes)
    assert len(glob.glob(str(tmp_path / "b" / "*" / "batch-*.parquet"))) >= 2


def test_pipelines_and_the_cli_take_the_batch_options(tmp_path, monkeypatch):
    from duckduck.pipeline import Pipelines
    from duckduck.pipeline.__main__ import main
    from duckduck.pipeline.domains import Domain

    src = Paged()
    run = Pipelines(duck=duck_of(src)).run(spec(tmp_path), batch_rows=5, staging=str(tmp_path / "st"))
    assert run.batch_rows == 5 and run.staging.startswith(str(tmp_path / "st"))
    assert written(tmp_path) == sorted(r["v"] for r in src.data)

    seen = {}
    monkeypatch.setattr(Domain, "run", lambda self, *t, **k: seen.update(k) or type("R", (), {"report": lambda s: ""})())
    path = tmp_path / "p.json"
    path.write_text(json.dumps(spec(tmp_path)))
    assert main(["run", str(path), "--batch-rows", "1000", "--staging", "/tmp/x", "--no-resume"]) == 0
    assert seen["batch_rows"] == 1000 and seen["staging"] == "/tmp/x" and seen["resume"] is False


def test_the_state_says_which_run_is_pending(tmp_path):
    from duckduck.system import SystemTables

    src = Paged()
    src.fail_after = 1
    with pytest.raises(TimeoutError):
        run_pipeline(spec(tmp_path, batch_rows=4), duck=duck_of(src), now=NOW)
    duck = DuckAPI()
    duck.auto_register({}, system_tables=False)
    duck._config_path = None
    tables = SystemTables(duck)
    config = {"lake": {"state": str(tmp_path / "state")}}
    tables._config = lambda: config
    df = tables.pipeline_runs()
    assert df.loc[0, "pending_run_id"] == state(tmp_path)["pending"]["run_id"]


@pytest.fixture
def fake_s3(tmp_path, monkeypatch):
    """s3://… locations on a pyarrow filesystem that isn't the local one DuckDB could read by path: the batches
    must go and come back through pyarrow, as they do on S3 with the lake's AWS account."""
    import pyarrow.fs as pafs

    from duckduck.pipeline import sip, state as state_module

    root = tmp_path / "s3"
    root.mkdir()
    real = sip._filesystem

    def filesystem(location):
        if location.startswith("s3://"):
            return pafs.SubTreeFileSystem(str(root), pafs.LocalFileSystem()), location[len("s3://"):]
        return real(location)

    monkeypatch.setattr(sip, "_filesystem", filesystem)
    monkeypatch.setattr(state_module, "_filesystem", filesystem)
    return root


def test_batches_and_state_on_s3_let_another_machine_finish_the_run(tmp_path, fake_s3):
    src = Paged()
    src.fail_after = 4
    on_s3 = spec(tmp_path, batch_rows=4, state="s3://lake/state")
    with pytest.raises(TimeoutError):
        run_pipeline(on_s3, duck=duck_of(src), now=NOW)
    staged = glob.glob(str(fake_s3 / "lake" / "state" / "_staging" / "p" / "*" / "sources" / "*" / "*" / "batch-*"))
    assert len(staged) >= 2  # what was read is in the bucket, not on the machine that died
    with open(fake_s3 / "lake" / "state" / "p.json") as f:
        pending = json.load(f)["pending"]
    assert pending["staging"].startswith("s3://lake/state/_staging/p/")

    other_machine = duck_of(src)  # a new process: only the bucket knows how far the first one got
    run = run_pipeline(on_s3, duck=other_machine, now=NOW)
    assert run.resumed and run.run_id == pending["run_id"] and src.calls[-1]["n_gt"] is not None
    assert written(tmp_path) == sorted(r["v"] for r in src.data)
    assert not glob.glob(str(fake_s3 / "lake" / "state" / "_staging" / "p" / "*"))  # removed after success


def test_the_lake_sets_the_staging_for_every_pipeline(tmp_path, fake_s3):
    from duckduck.pipeline.settings import with_settings

    s = with_settings(load_spec(spec(tmp_path)), {"staging": "s3://lake/_staging"})
    assert runner.staging_folder(s, "r1") == "s3://lake/_staging/p/r1"
    assert runner.staging_folder(load_spec(spec(tmp_path, state="s3://lake/state")), "r 2") == \
        "s3://lake/state/_staging/p/r_2"


def test_batches_whose_types_differ_read_as_one_table(tmp_path):
    class Mixed(Paged):
        IDEMPOTENCY = restart("test")

        @sortable("n")
        def iter_items(self, n_gt=None, order_by=None):
            yield pd.DataFrame({"n": [1, 2], "v": [None, None]})  # an all-NULL column first
            yield pd.DataFrame({"n": [3], "v": ["x"], "extra": [{"k": 1}]})  # then text, and a new column

    d = duck_of(Mixed())
    with batches.batching(batches.Batching(rows=1, folder=str(tmp_path / "b"))):
        out = d.sql("SELECT * FROM items ORDER BY n").df()
    assert list(out["v"].fillna("-")) == ["-", "-", "x"] and "extra" in out.columns

"""Declarative pipelines (duckduck.pipeline): the file, the SQL sources, views and the key's way, runs, the sip."""

import datetime as dt
import hashlib
import json
import os

import duckdb
import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.pipeline import PipelineError, load_spec, notebook, plan_pipeline, read_sip, run_pipeline
from duckduck.pipeline.__main__ import main
from duckduck.pipeline.analysis import build_plan
from duckduck.pipeline.sources import inline_statements, notebook_statements, split_sql
from duckduck.pipeline.spec import run_parameters, substitute
from duckduck.spark import spark_query

NOW = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)


def incidents_frame(n=2000, dups=3):
    df = pd.DataFrame({"sys_id": [f"INC{i:04d}" for i in range(n)], "cmdb_ci": [f"CI{i % 50}" for i in range(n)],
                       "priority": [i % 4 for i in range(n)], "short_description": ["disk full"] * n,
                       "caller_email": [f"user{i}@example.com" for i in range(n)]})
    return pd.concat([df, df.iloc[:dups]], ignore_index=True)


ASSETS = pd.DataFrame({"sys_id": [f"CI{i}" for i in range(50)], "region": [["SP", "RJ", "MG"][i % 3] for i in range(50)]})


@pytest.fixture
def duck():
    d = DuckAPI()
    data = {"rows": incidents_frame()}

    def incidents(limit=None):
        return data["rows"]

    def cmdb(limit=None):
        return ASSETS

    d.register_api_function("incidents", incidents)
    d.register_api_function("cmdb", cmdb)
    d.test_data = data
    return d


SQL = """
-- bronze: as it came
CREATE VIEW bronze AS SELECT * FROM incidents;
SELECT * FROM bronze LIMIT 5;  -- exploration: never run by the pipeline
CREATE VIEW enriched AS
SELECT i.*, a.region FROM bronze i LEFT JOIN cmdb a ON i.cmdb_ci = a.sys_id
WHERE i.priority < 3
QUALIFY row_number() OVER (PARTITION BY i.sys_id ORDER BY i.priority) = 1;
CREATE VIEW renamed AS SELECT sys_id AS id, region, priority, upper(short_description) AS short_description,
       caller_email FROM enriched;
CREATE VIEW gold AS SELECT region, priority, count(*) AS n FROM renamed GROUP BY ALL;
CREATE VIEW scratch AS SELECT 1 AS x;
"""


def spec_dict(tmp_path, **over):
    d = {"pipeline": "risk", "primary_key": "sys_id", "sql": SQL,
         "sip": {"rate": 0.02, "max_rows": 10, "columns": ["priority", "region", "short_description", "n",
                                                           "caller_email"],
                 "mask": ["caller_email"], "watch": ["INC0001", "INC0003"], "store": str(tmp_path / "sip")},
         "targets": {"enriched": {"path": str(tmp_path / "silver"), "mode": "merge"},
                     "gold": {"path": str(tmp_path / "gold"), "mode": "overwrite", "partition_by": "region"}}}
    d.update(over)
    return d


def picked(key, rate):
    """The rule every engine runs, in Python: the key's md5's first 8 hex digits under the rate."""
    upper = format(int(rate * 16 ** 8) - 1, "08x")
    return hashlib.md5(key.encode()).hexdigest()[:8] <= upper


# -- the file ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("data, message", [
    ({"pipeline": "x"}, "exactly one of 'notebook'"),
    ({"pipeline": "x", "sql": "SELECT 1", "sql_file": "a.sql"}, "exactly one of"),
    ({"pipeline": "bad name", "sql": "SELECT 1"}, "'pipeline' is the pipeline's name"),
    ({"pipeline": "x", "sql": "SELECT 1", "engine": "pandas"}, "'engine' is one of"),
    ({"pipeline": "x", "sql": "SELECT 1", "sip": {"rate": 2}}, "sip.rate"),
    ({"pipeline": "x", "sql": "SELECT 1", "sip": True}, "add 'primary_key'"),
    ({"pipeline": "x", "sql": "SELECT 1", "target": {"path": "/x", "mode": "merge"}}, "needs a key"),
    ({"pipeline": "x", "sql": "SELECT 1", "target": {"path": "/x", "mode": "upsert"}}, "target mode is one of"),
    ({"pipeline": "x", "sql": "SELECT 1", "target": {"path": "/x", "table": "a.b"}}, "not both"),
    ({"pipeline": "x", "sql": "SELECT 1", "colour": 1}, "unknown key(s) ['colour']"),
    ({"pipeline": "x", "sql": "SELECT 1", "parameters": {"run_date": "x"}}, "built-in"),
])
def test_the_file_says_what_to_fix(data, message):
    with pytest.raises(PipelineError) as e:
        load_spec(data)
    assert message in str(e.value)


def test_a_target_is_a_path_or_a_catalog_table():
    spec = load_spec({"pipeline": "x", "sql": "SELECT 1", "targets": {
        "a": "s3://lake/gold/a/", "b": "silver.incidents", "c": {"path": "./out", "format": "delta", "mode": "merge",
                                                                 "key": "id"}}})
    a, b, c = spec.targets
    assert (a.path, a.format, a.mode) == ("s3://lake/gold/a/", "parquet", "append")
    assert (b.table, b.format) == ("silver.incidents", None)
    assert (c.format, c.key) == ("delta", ["id"])


def test_parameters_and_date_arithmetic():
    spec = load_spec({"pipeline": "x", "sql": "SELECT 1", "parameters": {"since": "{{ run_date - 1d }}",
                                                                          "region": "SP"}})
    values = run_parameters(spec, {"region": "RJ"}, now=NOW, run_id="r")
    assert values["since"] == "2026-09-30" and values["region"] == "RJ" and values["run_id"] == "r"
    assert substitute("WHERE d >= '{{ since }}' AND t > '{{ run_at - 2h }}'", values) == \
        "WHERE d >= '2026-09-30' AND t > '2026-10-01 10:00:00+00:00'"
    with pytest.raises(PipelineError, match="unknown parameter"):
        substitute("{{ nope }}", values)
    with pytest.raises(PipelineError, match="aren't in the pipeline's 'parameters'"):
        run_parameters(spec, {"other": 1}, now=NOW)


# -- where the SQL comes from ---------------------------------------------------------------------------------------


def test_statements_split_outside_strings_and_comments():
    text = "-- it's a comment; with a quote\nSELECT 'a;b' AS x;\n/* ; */ SELECT \"c;d\" FROM t; ;\n"
    parts = split_sql(text)
    assert [p.strip() for p, _ in parts] == ["-- it's a comment; with a quote\nSELECT 'a;b' AS x",
                                             "/* ; */ SELECT \"c;d\" FROM t"]
    assert [line for _, line in parts] == [2, 3]


def test_a_notebook_gives_its_sql_cells_only(tmp_path):
    nb = {"cells": [
        {"cell_type": "markdown", "source": ["%%sql\nCREATE VIEW nope AS SELECT 1"]},
        {"cell_type": "code", "source": ["from duckduck.pipeline import notebook\n", "nb = notebook('p.json')"]},
        {"cell_type": "code", "source": ["%%sql\n", "CREATE VIEW a AS SELECT 1 AS id;\n", "SELECT * FROM a"]},
        {"cell_type": "code", "source": "a_df = nb.view('a')"},
        {"cell_type": "code", "source": ["\n%%duckduck\n", "CREATE VIEW b AS SELECT * FROM a"]},
    ], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    path = tmp_path / "gold.ipynb"
    path.write_text(json.dumps(nb))
    found = notebook_statements(str(path))
    assert [(s.sql.strip(), s.origin) for s in found] == [
        ("CREATE VIEW a AS SELECT 1 AS id", "cell 3"), ("SELECT * FROM a", "cell 3"),
        ("CREATE VIEW b AS SELECT * FROM a", "cell 5")]


def test_the_three_sources_give_the_same_plan(tmp_path):
    (tmp_path / "p.sql").write_text(SQL)
    cells = [{"cell_type": "code", "source": "%%sql\n" + part} for part in SQL.split(";") if part.strip()]
    (tmp_path / "p.ipynb").write_text(json.dumps({"cells": cells}))
    base = {"pipeline": "risk", "primary_key": "sys_id", "sip": True, "target": "/lake/gold", "output": "gold"}
    plans = [plan_pipeline(load_spec({**base, **src}, base_dir=str(tmp_path))) for src in
             ({"sql": SQL}, {"sql_file": "p.sql"}, {"notebook": "p.ipynb"})]
    assert {tuple(p.needed) for p in plans} == {("bronze", "enriched", "renamed", "gold")}
    assert {tuple(p.unused) for p in plans} == {("scratch",)}


# -- views and the key's way ---------------------------------------------------------------------------------------


def test_the_plan_follows_the_key_through_renames_and_groups(tmp_path):
    plan = plan_pipeline(spec_dict(tmp_path))
    keys = {n: (plan.views[n].key.mode, plan.views[n].key.columns, plan.views[n].key.upstream) for n in plan.needed}
    assert keys == {"bronze": ("row", ["sys_id"], None), "enriched": ("row", ["sys_id"], "bronze"),
                    "renamed": ("row", ["id"], "enriched"), "gold": ("group", ["region", "priority"], "renamed")}
    assert plan.sampled == ["bronze", "enriched", "renamed", "gold"] and plan.unused == ["scratch"]
    text = plan.report()
    assert "key id (was sys_id) — from enriched" in text
    assert "each followed row of renamed traced into its group" in text
    assert "not run (no target needs them): scratch" in text


@pytest.mark.parametrize("sql, reason", [
    ("CREATE VIEW a AS SELECT region FROM incidents", "it doesn't keep sys_id"),
    ("CREATE VIEW a AS SELECT count(*) AS n FROM incidents", "aggregates every row into one"),
    ("CREATE VIEW a AS SELECT count(*) AS n FROM incidents GROUP BY region", "groups by region, which isn't"),
])
def test_a_key_it_cant_follow_fails_the_plan_with_the_fix(sql, reason):
    with pytest.raises(PipelineError) as e:
        plan_pipeline({"pipeline": "x", "primary_key": "sys_id", "sql": sql, "sip": True, "target": "/lake/a"})
    assert reason in str(e.value) and '"keys": {"<view>": ["<column>"]}' in str(e.value)


def test_declared_keys_and_lookups_off_the_way_are_fine():
    sql = """CREATE VIEW people AS SELECT name FROM users;
             CREATE VIEW a AS SELECT sys_id AS ticket, p.name FROM incidents i JOIN people p ON i.u = p.name;
             CREATE VIEW b AS SELECT concat(ticket, '-x') AS tid, name FROM a"""
    plan = plan_pipeline({"pipeline": "x", "primary_key": "sys_id", "sql": sql, "sip": True,
                          "keys": {"b": "tid"}, "target": "/lake/b"})
    assert plan.sampled == ["a", "b"]  # people is a lookup: not on the key's way
    assert plan.views["b"].key.columns == ["tid"] and plan.views["b"].key.declared


@pytest.mark.parametrize("sql, message", [
    ("INSERT INTO t SELECT 1", "INSERT isn't allowed"),
    ("CREATE VIEW a AS SELECT 1; CREATE VIEW a AS SELECT 2", "made twice"),
    ("CREATE VIEW silver.a AS SELECT 1", "plain name"),
    ("CREATE VIEW a AS SELECT * FROM a", "reads itself"),
    ("CREATE VIEW a AS SELEC 1", "can't read this SQL"),
])
def test_sql_the_pipeline_refuses(sql, message):
    with pytest.raises(PipelineError, match=message):
        plan_pipeline({"pipeline": "x", "sql": sql})


def test_without_views_the_last_query_is_the_output():
    plan = plan_pipeline({"pipeline": "daily", "sql": "SELECT 1 AS a; SELECT 2 AS b", "target": "/lake/x"})
    assert plan.needed == ["daily"] and plan.views["daily"].sql.strip() == "SELECT 2 AS b"
    assert plan.targets[0].view == "daily"


def test_ignored_statements_and_unknown_target_views():
    plan = plan_pipeline({"pipeline": "x", "sql": "DESCRIBE t; CREATE VIEW a AS SELECT 1 AS x; SHOW TABLES"})
    assert plan.needed == ["a"] and len(plan.ignored) == 2
    with pytest.raises(PipelineError, match="names view 'b', which the SQL doesn't make"):
        plan_pipeline({"pipeline": "x", "sql": "CREATE VIEW a AS SELECT 1", "targets": {"b": "/lake/b"}})


# -- running on DuckDB ---------------------------------------------------------------------------------------------


def test_a_run_writes_its_targets_and_follows_the_keys(duck, tmp_path):
    run = run_pipeline(spec_dict(tmp_path), duck=duck, run_id="r1", now=NOW)
    assert [s["view"] for s in run.steps] == ["bronze", "enriched", "renamed", "gold"]
    assert duckdb.sql(f"SELECT count(*), count(DISTINCT sys_id) FROM '{tmp_path}/silver/*.parquet'").fetchone() \
        == (1500, 1500)
    gold = duckdb.sql(f"SELECT region, sum(n) FROM read_parquet('{tmp_path}/gold/**/*.parquet', "
                      "hive_partitioning=true) GROUP BY 1 ORDER BY 1").fetchall()
    assert sum(n for _, n in gold) == 1500 and [r for r, _ in gold] == ["MG", "RJ", "SP"]

    sip = run.sip
    sampled = sorted(sip.loc[sip["stage"] == "bronze", "key"])
    expected = sorted(k for k in incidents_frame()["sys_id"].unique() if picked(k, 0.02))[:10]
    # the smallest hashes fill max_rows, the watched keys come on top
    by_hash = sorted((k for k in incidents_frame()["sys_id"].unique() if picked(k, 0.02)),
                     key=lambda k: hashlib.md5(k.encode()).hexdigest())[:10]
    assert set(sampled) == set(by_hash) | {"INC0001", "INC0003"} and len(expected) == 10

    one = sip[sip["key"] == "INC0001"].set_index("stage")
    assert one.loc["bronze", "event"] == "duplicated" and one.loc["bronze", "n"] == 2
    assert one.loc["enriched", "event"] == "seen"  # deduplicated there
    assert one.loc["renamed", "event"] == "changed" and one.loc["renamed", "changed"] == "short_description"
    assert one.loc["gold", "event"] == "grouped" and one.loc["gold", "stage_key"] == "RJ|1"
    assert json.loads(one.loc["gold", "row"]) == {"region": "RJ", "priority": 1, "n": 180}
    three = sip[sip["key"] == "INC0003"].set_index("stage")  # priority 3: filtered out
    assert three.loc["enriched", "event"] == "dropped" and "in bronze, not in enriched" in three.loc["enriched", "note"]
    assert "gold" not in three.index

    rows = [json.loads(r) for r in sip["row"].dropna()]
    assert all("cmdb_ci" not in r for r in rows)  # not in the allow-list: never leaves the pipeline
    emails = [r["caller_email"] for r in rows if "caller_email" in r]
    assert emails and all(e.startswith("sha256:") for e in emails)

    kept = read_sip(str(tmp_path / "sip"), pipeline="risk")
    assert len(kept) == len(sip) and set(kept["run_id"]) == {"r1"}
    assert run.sip_path.endswith("risk/run_date=2026-10-01/run-r1.parquet")


def test_every_engine_and_pipeline_picks_the_same_keys(duck, tmp_path):
    """Silver and gold in separate files, the same rate: the same keys, with no coordination."""
    silver = {"pipeline": "silver", "primary_key": "sys_id", "sip": {"rate": 0.01, "max_rows": 1000},
              "sql": "CREATE VIEW s AS SELECT * FROM incidents WHERE priority < 3", "target": str(tmp_path / "s")}
    gold = {"pipeline": "gold", "primary_key": "sys_id", "sip": {"rate": 0.01, "max_rows": 1000},
            "sql": "CREATE VIEW g AS SELECT sys_id, priority FROM incidents WHERE priority = 1",
            "target": str(tmp_path / "g")}
    a = run_pipeline(silver, duck=duck, now=NOW)
    b = run_pipeline(gold, duck=duck, now=NOW)
    in_gold = set(b.sip["key"])
    assert in_gold and in_gold <= set(a.sip["key"])
    assert in_gold == {k for k in incidents_frame()["sys_id"].unique() if picked(k, 0.01) and int(k[3:]) % 4 == 1}


def test_merge_upserts_and_refuses_duplicate_keys(duck, tmp_path):
    spec = {"pipeline": "m", "primary_key": "sys_id", "target": {"path": str(tmp_path / "t"), "mode": "merge"},
            "sql": "CREATE VIEW v AS SELECT sys_id, priority FROM incidents "
                   "QUALIFY row_number() OVER (PARTITION BY sys_id ORDER BY priority) = 1"}
    run_pipeline(spec, duck=duck, run_id="a")
    duck.test_data["rows"] = pd.DataFrame({"sys_id": ["INC0000", "NEW1"], "priority": [9, 9],
                                           "cmdb_ci": "x", "short_description": "", "caller_email": ""})
    run = run_pipeline(spec, duck=duck, run_id="b")
    assert run.writes[0]["rows"] == 2 and run.writes[0]["removed"] == 1
    got = dict(duckdb.sql(f"SELECT sys_id, priority FROM '{tmp_path}/t/*.parquet' "
                          "WHERE sys_id IN ('INC0000', 'INC0005', 'NEW1')").fetchall())
    assert got == {"INC0000": 9, "INC0005": 1, "NEW1": 9}
    assert duckdb.sql(f"SELECT count(*) FROM '{tmp_path}/t/*.parquet'").fetchone()[0] == 2001
    spec["sql"] = "CREATE VIEW v AS SELECT sys_id, priority FROM incidents UNION ALL SELECT 'NEW1', 1"
    with pytest.raises(PipelineError, match="more than one row for some keys"):
        run_pipeline(spec, duck=duck)


@pytest.mark.parametrize("mode, expected", [("append", 6), ("overwrite", 3), ("overwrite_partitions", 5)])
def test_write_modes_on_parquet(duck, tmp_path, mode, expected):
    path = str(tmp_path / "t")
    first = {"pipeline": "w", "sql": "SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (3, 'c')) t(id, part)",
             "target": {"path": path, "mode": mode, "partition_by": "part"}}
    run_pipeline(first, duck=duck, run_id="one")
    second = {**first, "sql": "SELECT * FROM (VALUES (4, 'a'), (5, 'a'), (6, 'd')) t(id, part)"}
    run_pipeline(second, duck=duck, run_id="two")
    ids = sorted(r[0] for r in duckdb.sql(f"SELECT id FROM read_parquet('{path}/**/*.parquet', "
                                          "hive_partitioning=true)").fetchall())
    assert len(ids) == expected
    if mode == "overwrite_partitions":  # part a replaced, b and c kept, d added
        assert ids == [2, 3, 4, 5, 6]  # b and c kept, a replaced by 4 and 5, d added
    if mode == "overwrite":
        assert ids == [4, 5, 6]


def test_delta_targets(duck, tmp_path):
    pytest.importorskip("deltalake")
    from deltalake import DeltaTable

    path = str(tmp_path / "d")
    spec = {"pipeline": "d", "primary_key": "id", "audit_columns": False,
            "sql": "SELECT * FROM (VALUES (1, 'a'), (2, 'b')) t(id, v)",
            "target": {"path": path, "format": "delta", "mode": "merge"}}
    assert run_pipeline(spec, duck=duck).writes[0]["created"]
    spec["sql"] = "SELECT * FROM (VALUES (2, 'B'), (3, 'c')) t(id, v)"
    run = run_pipeline(spec, duck=duck)
    assert run.writes[0]["num_target_rows_updated"] == 1 and run.writes[0]["num_target_rows_inserted"] == 1
    rows = sorted(DeltaTable(path).to_pyarrow_table().to_pylist(), key=lambda r: r["id"])
    assert rows == [{"id": 1, "v": "a"}, {"id": 2, "v": "B"}, {"id": 3, "v": "c"}]


def test_the_duckdb_engine_writes_paths_only(duck):
    with pytest.raises(PipelineError, match="needs the spark engine|run the pipeline with \"engine\": \"spark\""):
        run_pipeline({"pipeline": "x", "sql": "SELECT 1 AS a", "target": "silver.x"}, duck=duck)


def test_a_view_named_like_a_registered_table_is_refused(duck, tmp_path):
    with pytest.raises(PipelineError, match="has the name of a registered table"):
        run_pipeline({"pipeline": "x", "sql": "CREATE VIEW cmdb AS SELECT 1 AS a", "target": str(tmp_path)},
                     duck=duck)


def test_a_dry_run_writes_nothing(duck, tmp_path):
    run = run_pipeline(spec_dict(tmp_path), duck=duck, dry_run=True)
    assert not run.writes and len(run.sip) and run.sip_path is None
    assert not os.path.exists(tmp_path / "silver") and not os.path.exists(tmp_path / "sip")
    assert "dry run (nothing written)" in run.report()


# -- the sip never fails the pipeline ------------------------------------------------------------------------------


def test_a_sip_problem_is_an_event_not_a_failure(duck, tmp_path, monkeypatch):
    from duckduck.pipeline.sip import Sip

    def broken(self, view, cols, where):
        raise RuntimeError("the engine said no")

    monkeypatch.setattr(Sip, "_sample", broken)
    run = run_pipeline(spec_dict(tmp_path), duck=duck)
    assert len(run.writes) == 2
    assert set(run.sip["event"]) == {"sip_error"} and "the engine said no" in run.sip["note"].iloc[0]
    assert any("sip at bronze" in w for w in run.warnings)


def test_a_failed_run_still_keeps_its_sip(duck, tmp_path):
    spec = spec_dict(tmp_path)
    spec["sql"] = SQL.replace("count(*) AS n", "count(*) / 0 AS n").replace("GROUP BY ALL", "GROUP BY ALL HAVING n > 'x'")
    with pytest.raises(Exception):
        run_pipeline(spec, duck=duck, run_id="bad")
    kept = read_sip(str(tmp_path / "sip"), run_id="bad")
    assert kept["event"].iloc[-1] == "failed" and kept["stage"].iloc[-1] == "gold"
    assert {"bronze", "enriched", "renamed"} <= set(kept["stage"])


def test_null_keys_and_new_keys(duck, tmp_path):
    rows = incidents_frame(200, 0)
    rows.loc[5, "sys_id"] = None
    duck.test_data["rows"] = rows
    spec = {"pipeline": "n", "primary_key": "sys_id", "sip": {"rate": 0.5, "max_rows": 1000},
            "sql": "CREATE VIEW a AS SELECT * FROM incidents WHERE priority = 0;"
                   "CREATE VIEW b AS SELECT * FROM a UNION ALL SELECT * FROM incidents WHERE priority = 1",
            "target": str(tmp_path / "b")}
    run = run_pipeline(spec, duck=duck)
    events = run.sip
    nulls = events[events["event"] == "null_keys"]
    assert list(nulls["stage"]) == ["b"] and list(nulls["n"]) == [1]  # the null key's row has priority 1: in b only
    new = events[(events["stage"] == "b") & (events["event"] == "new")]
    assert len(new) and all(int(k[3:]) % 4 == 1 for k in new["key"])


# -- notebook --------------------------------------------------------------------------------------------------------


def test_a_notebook_session_shows_the_sip_under_each_view(duck, tmp_path, capsys):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({**spec_dict(tmp_path), "sql": None, "notebook": "p.ipynb"}))
    nb = notebook(str(p), duck=duck)
    preview = nb.run_cell("CREATE VIEW bronze AS SELECT * FROM incidents")
    assert "bronze: 2,003 rows · key sys_id — the sip starts here" in capsys.readouterr().out
    assert set(preview["event"]) == {"seen", "duplicated"}
    rows = nb.run_cell("SELECT count(*) AS n FROM bronze")
    assert rows["n"].iloc[0] == 2003
    nb.run_cell("CREATE VIEW enriched AS SELECT * FROM bronze WHERE priority = 0")
    out = nb.run_cell("CREATE VIEW enriched AS SELECT * FROM bronze WHERE priority < 2")  # run again: replaced
    assert len(nb.sip[nb.sip["stage"] == "enriched"]) == len(out)
    lookup = nb.run_cell("CREATE VIEW people AS SELECT region FROM cmdb")
    assert "not followed" in capsys.readouterr().out and list(lookup.columns) == ["region"]
    assert nb.views == ["bronze", "risk", "enriched", "people"]  # a query cell is a candidate job: "risk"


def test_the_magic_is_registered_in_ipython(duck):
    pytest.importorskip("IPython")
    from IPython.core.interactiveshell import InteractiveShell

    shell = InteractiveShell.instance()
    try:
        nb = notebook(duck=duck)
        result = shell.run_cell_magic("sql", "", "CREATE VIEW a AS SELECT 1 AS id; SELECT id + 1 AS x FROM a")
        assert result["x"].iloc[0] == 2
        assert shell.run_cell_magic("duckduck", "", "SELECT * FROM a")["id"].iloc[0] == 1
        assert nb.views == ["a"]
    finally:
        InteractiveShell.clear_instance()


# -- CLI = Python ----------------------------------------------------------------------------------------------------


def test_cli_plan_and_run_print_the_same_reports(duck, tmp_path, capsys, monkeypatch):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"pipeline": "c", "sql": "CREATE VIEW v AS SELECT 1 AS id",
                                "target": str(tmp_path / "out")}))
    assert main(["plan", str(path)]) == 0
    assert capsys.readouterr().out.strip() == plan_pipeline(str(path)).report()
    monkeypatch.setattr("duckduck.pipeline.runner._duck", lambda d, c: duck)
    assert main(["run", str(path), "--run-id", "x"]) == 0
    assert "pipeline c · run x · duckdb" in capsys.readouterr().out
    assert main(["plan", str(tmp_path / "missing.json")]) == 2
    assert "no pipeline file" in capsys.readouterr().err


# -- Spark -------------------------------------------------------------------------------------------------------------


def test_qualify_is_rewritten_for_spark_without_losing_a_qualified_star():
    sql, helpers = spark_query("SELECT i.*, a.region FROM b i JOIN c a ON i.k = a.k "
                               "QUALIFY row_number() OVER (PARTITION BY i.id ORDER BY i.p) = 1")
    assert helpers == ("_qualify_0",)
    assert sql.startswith("SELECT * FROM (SELECT i.*, a.region, ROW_NUMBER() OVER")
    assert sql.endswith("AS _qualified WHERE _qualify_0 = 1")


@pytest.fixture(scope="module")
def spark():
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    try:
        session = (SparkSession.builder.master("local[1]").appName("duckduck-pipeline-tests")
                   .config("spark.ui.enabled", "false").config("spark.ui.showConsoleProgress", "false")
                   .config("spark.sql.shuffle.partitions", "2").getOrCreate())
    except Exception as exc:  # no Java, …
        pytest.skip(f"no local Spark: {exc}")
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


def test_spark_runs_the_same_pipeline_and_follows_the_same_keys(duck, tmp_path, spark):
    spec = spec_dict(tmp_path)
    spec["targets"]["enriched"]["mode"] = "overwrite"
    on_duckdb = run_pipeline(spec, duck=duck, run_id="d", dry_run=True)
    on_spark = run_pipeline({**spec, "engine": "spark"}, duck=duck, spark=spark, run_id="s")
    assert spark.read.parquet(str(tmp_path / "silver")).count() == 1500
    assert spark.read.parquet(str(tmp_path / "gold")).count() == 9
    cols = ["stage", "key", "stage_key", "event", "n", "changed"]
    a = on_duckdb.sip[cols].sort_values(["stage", "key"]).reset_index(drop=True)
    b = on_spark.sip[cols].sort_values(["stage", "key"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b, check_dtype=False)
    assert spark.catalog.listTables() == [] or all(t.name not in ("bronze", "gold") for t in spark.catalog.listTables())


# -- the example -------------------------------------------------------------------------------------------------------


def test_the_example_runs_bronze_silver_gold_as_independent_jobs(tmp_path, capsys):
    import runpy
    import shutil

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for part in ("local", "pipeline"):
        shutil.copytree(os.path.join(repo, "examples", part), tmp_path / "examples" / part,
                        ignore=shutil.ignore_patterns("lake", "__pycache__"))
    runpy.run_path(str(tmp_path / "examples" / "pipeline" / "run.py"), run_name="__main__")
    out = capsys.readouterr().out
    assert out.count("pipeline raw_inventory_assets") == 2 and out.count("pipeline raw_inventory_owners") == 1
    assert out.count("pipeline silver_inventory_assets") == 2 and "pipeline gold_risk" in out
    assert "incremental on _loaded_at: first run: read everything" in out and "(merge, skipped)" in out
    assert "read where \"_loaded_at\" > '" in out and "nothing newer" in out
    assert "lake/raw/inventory/owners" in out and "lake/silver/inventory/owners" in out
    way = read_sip(str(tmp_path / "examples" / "pipeline" / "lake" / "_sip"), key="web-0001")
    assert list(dict.fromkeys(way["stage"])) == ["raw_inventory_assets", "silver_inventory_assets", "exposed",
                                                 "gold_risk"]
    assert way["event"].iloc[-1] == "grouped"
    example = tmp_path / "examples" / "pipeline"
    plan = plan_pipeline(str(example / "gold_risk.json"), config_path=str(example / "duckduck.pipeline.json"))
    assert plan.needed == ["exposed", "gold_risk"] and len(plan.queries) == 2  # one exploration, one job
    runpy.run_path(str(tmp_path / "examples" / "pipeline" / "python_api.py"), run_name="__main__")
    assert "dry run (nothing written)" in capsys.readouterr().out


def test_a_write_reports_no_internal_details(duck, tmp_path):
    run = run_pipeline({"pipeline": "r", "sql": "SELECT 1 AS id", "target": str(tmp_path / "out")}, duck=duck)
    assert not any(k.startswith("_") for k in run.writes[0]) and "files:" not in run.report()


# -- plain SQL: no CREATE VIEW, its WITH steps followed ----------------------------------------------------------------

PLAIN = """
SELECT * FROM incidents LIMIT 5;  -- exploration
WITH bronze AS (SELECT * FROM incidents),
enriched AS (
  SELECT i.*, a.region FROM bronze i LEFT JOIN cmdb a ON i.cmdb_ci = a.sys_id
  WHERE i.priority < 3
  QUALIFY row_number() OVER (PARTITION BY i.sys_id ORDER BY i.priority) = 1),
renamed AS (SELECT sys_id AS id, region, priority, upper(short_description) AS short_description, caller_email
            FROM enriched)
SELECT region, priority, count(*) AS n FROM renamed GROUP BY ALL
"""


def test_plain_sql_is_the_job_and_its_with_steps_are_followed(duck, tmp_path):
    views = spec_dict(tmp_path)
    views["targets"] = {"gold": {"path": str(tmp_path / "views"), "mode": "overwrite"}}
    plain = {**spec_dict(tmp_path), "pipeline": "gold", "sql": PLAIN,
             "target": {"path": str(tmp_path / "plain"), "mode": "overwrite"}}
    plain.pop("targets")
    plan = plan_pipeline(plain)
    assert plan.needed == ["bronze", "enriched", "renamed", "gold"] and plan.sampled == plan.needed
    assert "1 exploration query(ies) not run" in plan.report() and "WITH enriched" in plan.report()
    a = run_pipeline(views, duck=duck, run_id="v").sip
    b = run_pipeline(plain, duck=duck, run_id="p").sip
    cols = ["stage", "key", "stage_key", "event", "n", "changed"]
    pd.testing.assert_frame_equal(a[cols].reset_index(drop=True), b[cols].reset_index(drop=True))
    assert duckdb.sql(f"SELECT sum(n) FROM '{tmp_path}/plain/*.parquet'").fetchone()[0] == 1500


def test_a_with_that_cant_be_steps_stays_inside_one(duck):
    for sql in ("WITH cmdb AS (SELECT 1 AS sys_id) SELECT * FROM cmdb",  # named like a registered table
                "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT n AS sys_id FROM t"):
        plan = plan_pipeline({"pipeline": "x", "primary_key": "sys_id", "sql": sql, "target": "/lake/x"}, duck=duck)
        assert plan.needed == ["x"]
    run = run_pipeline({"pipeline": "x", "sql": "WITH cmdb AS (SELECT 1 AS a) SELECT * FROM cmdb"}, duck=duck,
                       dry_run=True)
    assert [s["view"] for s in run.steps] == ["x"]


def test_composite_keys_through_plain_sql(duck, tmp_path):
    duck.test_data["rows"] = pd.DataFrame({"order_id": ["A", "A", "B", "C"], "line": [1, 2, 1, 1],
                                           "qty": [1, 2, 3, 4], "sys_id": "x", "cmdb_ci": "x", "priority": 0,
                                           "short_description": "", "caller_email": ""})
    spec = {"pipeline": "lines", "primary_key": ["order_id", "line"],
            "sql": "WITH clean AS (SELECT order_id, line, qty * 10 AS qty FROM incidents) SELECT * FROM clean",
            "target": {"path": str(tmp_path / "lines"), "mode": "merge"},
            "sip": {"rate": 1.0, "watch": [["A", 2]], "columns": ["qty"]}}
    run = run_pipeline(spec, duck=duck)
    keys = set(run.sip.loc[run.sip["stage"] == "clean", "key"])
    assert keys == {"A|1", "A|2", "B|1", "C|1"}
    changed = run.sip[(run.sip["stage"] == "lines") & (run.sip["key"] == "A|2")]
    assert changed["event"].iloc[0] == "seen" and json.loads(changed["row"].iloc[0])["qty"] == 20
    duck.test_data["rows"].loc[0, "qty"] = 9  # A|1 changes; merged on both columns
    run_pipeline(spec, duck=duck)
    got = dict(duckdb.sql(f"SELECT order_id || '|' || line, qty FROM '{tmp_path}/lines/*.parquet'").fetchall())
    assert got == {"A|1": 90, "A|2": 20, "B|1": 30, "C|1": 40}


def test_a_notebook_query_cell_shows_its_with_steps(duck, tmp_path, capsys):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({**spec_dict(tmp_path), "sql": None, "notebook": "p.ipynb"}))
    nb = notebook(str(p), duck=duck)
    rows = nb.run_cell(PLAIN.split(";", 1)[1])
    out = capsys.readouterr().out
    assert "bronze: 2,003 rows" in out and "renamed:" in out and "grouped" in out
    assert rows["n"].sum() == 1500 and nb.views == ["bronze", "enriched", "renamed", "risk"]


# -- independent jobs: full and incremental loads ---------------------------------------------------------------------


def feed_of(duck, tmp_path):
    feed = tmp_path / "feed"
    seen = []

    def read(loaded_gt=None, limit=None):  # takes the push-down: the incremental WHERE reaches it
        seen.append(loaded_gt)
        if not feed.exists():
            return pd.DataFrame({"id": pd.Series(dtype="int64"), "loaded": pd.Series(dtype="datetime64[ns]")})
        df = pd.read_parquet(feed)
        return df[df["loaded"] > pd.Timestamp(loaded_gt)] if loaded_gt else df

    duck.register_api_function("feed", read)

    def load(rows, at):
        feed.mkdir(exist_ok=True)
        pd.DataFrame({"id": rows, "loaded": pd.Timestamp(at)}).to_parquet(
            feed / f"{at.replace(':', '').replace(' ', '_')}.parquet")
    return load, seen


def test_an_incremental_job_reads_only_what_changed_since_its_last_run(duck, tmp_path):
    from duckduck.pipeline.state import read_state

    load, seen = feed_of(duck, tmp_path)
    silver = {"pipeline": "silver", "primary_key": "id", "state": str(tmp_path / "state"),
              "load": {"type": "incremental", "columns": ["loaded"]},
              "sql": "SELECT id, loaded FROM feed",
              "target": {"path": str(tmp_path / "silver"), "mode": "merge"}}
    assert "no watermark yet: the first run reads everything" in plan_pipeline(silver).report()
    load([1, 2], "2026-10-01 10:00:00")
    first = run_pipeline(silver, duck=duck)
    assert first.writes[0]["rows"] == 2 and first.load["where"] is None and seen[-1] is None
    load([3], "2026-10-01 10:15:00")
    load([2, 4], "2026-10-01 10:30:00")  # bronze ran twice meanwhile
    plan = plan_pipeline(silver)
    assert "adds WHERE \"loaded\" > '2026-10-01 10:00:00'" in plan.report()
    second = run_pipeline(silver, duck=duck)
    assert second.writes[0]["rows"] == 3 and seen[-1] == "2026-10-01 10:00:00"  # pushed to the source
    assert "loaded: 2026-10-01 10:00:00 → 2026-10-01 10:30:00" in second.report()
    third = run_pipeline(silver, duck=duck)
    assert third.writes[0]["skipped"] and third.writes[0]["rows"] == 0 and "nothing newer" in third.report()
    state = read_state(str(tmp_path / "state"), "silver")
    assert state["watermarks"] == {"loaded": "2026-10-01 10:30:00"} and state["last_run_id"] == third.run_id
    assert duckdb.sql(f"SELECT count(*), count(DISTINCT id) FROM '{tmp_path}/silver/*.parquet'").fetchone() == (4, 4)
    # a re-read from a given point, as a dry run: reads as a run would and moves nothing
    again = run_pipeline(silver, duck=duck, dry_run=True, params={"watermark": "2026-10-01 10:10:00"})
    assert again.steps[0]["rows"] == 3 and seen[-1] == "2026-10-01 10:10:00"
    assert read_state(str(tmp_path / "state"), "silver")["last_run_id"] == third.run_id


def test_incremental_options(duck, tmp_path):
    load, seen = feed_of(duck, tmp_path)
    load([1], "2026-10-01 10:00:00")
    load([2], "2026-10-02 10:00:00")
    base = {"pipeline": "s", "primary_key": "id", "state": str(tmp_path / "st"), "target": str(tmp_path / "o")}
    run = run_pipeline({**base, "sql": "SELECT id, loaded FROM feed WHERE id > 0 OR id < -5",
                        "load": {"columns": "loaded", "initial": "2026-10-01 12:00:00", "lookback": "1h"}}, duck=duck)
    assert run.load["where"] == "\"loaded\" > '2026-10-01 11:00:00'" and run.steps[0]["rows"] == 1
    # the step that reads the source gets the WHERE, qualified when it joins; the job's own WHERE is kept whole
    sql = ("WITH f AS (SELECT x.id, x.loaded FROM feed x JOIN (SELECT 1 AS id UNION ALL SELECT 2) y ON x.id = y.id) "
           "SELECT id FROM f")
    plan = plan_pipeline({**base, "pipeline": "t", "sql": sql, "load": {"columns": ["loaded"], "initial": "2026-10-02"}})
    assert plan.load["step"] == "f" and plan.views["f"].sql.endswith('WHERE "x"."loaded" > \'2026-10-02\'')
    with pytest.raises(PipelineError, match="set \"state\" in duckduck.json's \"lake\""):
        plan_pipeline({"pipeline": "x", "sql": "SELECT 1", "load": {"columns": ["t"]}}, config_path="/nowhere.json")
    # no columns: the _loaded_at the layer below stamped on every row
    assert load_spec({"pipeline": "x", "sql": "SELECT 1", "load": "incremental", "state": "/s"}).load.columns == \
        ["_loaded_at"]
    with pytest.raises(PipelineError, match="couldn't read max\\(missing\\) from x"):
        run_pipeline({**base, "pipeline": "x", "sql": "SELECT id FROM feed", "load": {"columns": ["missing"]}},
                     duck=duck)
    full = run_pipeline({**base, "pipeline": "f", "sql": "SELECT id FROM feed", "load": "full"}, duck=duck)
    assert full.load == {"type": "full"} and full.state_path.endswith("f.json")


# -- shared settings: layers, the catalog, state; audit columns --------------------------------------------------------


def lake_config(tmp_path, **lake):
    config = tmp_path / "duckduck.json"
    config.write_text(json.dumps({"services": {}, "lake": {
        "layers": {"raw": "lake/raw-layer", "silver": "lake/silver-layer"}, "state": "lake/_state",
        "sip_store": "lake/_sip", **lake}}))
    return str(config)


def test_a_layer_target_goes_to_prefix_database_table_name(duck, tmp_path):
    config = lake_config(tmp_path)
    spec = {"pipeline": "bronze_incidents", "primary_key": "sys_id", "layer": "raw", "sip": True,
            "sql": "SELECT * FROM incidents", "load": {"type": "incremental", "columns": ["priority"]},
            "target": {"database": "servicenow", "table_name": "incident", "mode": "append"}}
    plan = plan_pipeline(spec, config_path=config)
    assert plan.targets[0].path == str(tmp_path / "lake" / "raw-layer" / "servicenow" / "incident")
    assert plan.targets[0].table is None  # no catalog configured: the files only
    run = run_pipeline(spec, duck=duck, config_path=config)
    assert os.listdir(tmp_path / "lake" / "raw-layer" / "servicenow" / "incident")
    assert run.state_path == str(tmp_path / "lake" / "_state" / "bronze_incidents.json")
    assert run.sip_path.startswith(str(tmp_path / "lake" / "_sip"))
    with pytest.raises(PipelineError, match="no layer 'gold'.*layers: raw, silver"):
        plan_pipeline({**spec, "layer": "gold"}, config_path=config)
    with pytest.raises(PipelineError, match="leave 'path' out"):
        load_spec({**spec, "target": {"layer": "raw", "database": "a", "table_name": "b", "path": "/x"}})
    with pytest.raises(PipelineError, match="give both 'database' and 'table_name'"):
        load_spec({**spec, "target": {"database": "a"}})


def test_a_layer_target_is_registered_in_the_lake_catalog(duck, tmp_path, monkeypatch):
    pytest.importorskip("moto")
    import boto3
    from moto import mock_aws

    from duckduck.pipeline.catalogs import GlueCatalog

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "t")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "t")
    config = lake_config(tmp_path, catalog="glue")
    with mock_aws():
        glue = boto3.client("glue", region_name="us-east-1")
        spec = {"pipeline": "silver_incidents", "primary_key": "sys_id", "layer": "silver",
                "sql": "SELECT sys_id, priority FROM incidents QUALIFY row_number() OVER (PARTITION BY sys_id "
                       "ORDER BY priority) = 1",
                "target": {"database": "servicenow", "table_name": "incident", "mode": "merge"}}
        run = run_pipeline(spec, duck=duck, config_path=config,
                           catalogs={"glue": GlueCatalog("glue", region="us-east-1", client=glue)})
        assert run.writes[0]["created"] and run.writes[0]["target"] == "glue:servicenow.incident"
        t = glue.get_table(DatabaseName="servicenow", Name="incident")["Table"]
        assert t["StorageDescriptor"]["Location"] == str(tmp_path / "lake" / "silver-layer" / "servicenow" / "incident")
        assert [c["Name"] for c in t["StorageDescriptor"]["Columns"]] == ["sys_id", "priority", "_loaded_at",
                                                                          "_load_date", "_run_id"]


def test_every_row_written_says_when_and_by_which_run(duck, tmp_path):
    now = dt.datetime(2026, 10, 2, 1, 30, tzinfo=dt.timezone.utc)
    spec = {"pipeline": "a", "sql": "SELECT 1 AS id, TIMESTAMP '2020-01-01' AS _loaded_at",
            "timezone": "America/Sao_Paulo", "target": {"path": str(tmp_path / "a"), "partition_by": "_load_date"}}
    run = run_pipeline(spec, duck=duck, now=now, run_id="r1")
    row = duckdb.sql(f"SELECT * FROM read_parquet('{tmp_path}/a/**/*.parquet', hive_partitioning=true)").df()
    assert row["_loaded_at"].iloc[0] == pd.Timestamp("2026-10-02 01:30:00")  # UTC; replaces what the source had
    assert str(row["_load_date"].iloc[0])[:10] == "2026-10-01"  # the run's date in its timezone
    assert row["_run_id"].iloc[0] == "r1" and run.writes[0]["rows"] == 1
    assert os.path.isdir(tmp_path / "a" / "_load_date=2026-10-01")
    off = run_pipeline({**spec, "audit_columns": False, "target": str(tmp_path / "b")}, duck=duck)
    assert list(duckdb.sql(f"SELECT * FROM '{tmp_path}/b/*.parquet'").df().columns) == ["id", "_loaded_at"]
    assert off.writes[0]["rows"] == 1
    some = run_pipeline({**spec, "audit_columns": ["_run_id"], "target": str(tmp_path / "c")}, duck=duck,
                        run_id="r9")
    assert list(duckdb.sql(f"SELECT * FROM '{tmp_path}/c/*.parquet'").df().columns) == ["id", "_loaded_at", "_run_id"]
    assert some.run_id == "r9"


def test_a_numeric_watermark_stays_a_number(duck, tmp_path):
    spec = {"pipeline": "n", "sql": "SELECT * FROM incidents", "state": str(tmp_path / "s"),
            "load": {"type": "incremental", "columns": ["priority"]}, "target": str(tmp_path / "o")}
    run_pipeline(spec, duck=duck)
    assert plan_pipeline(spec).load["where"] == '"priority" > 3'


# -- domain files: several tables grouped, run through the Pipelines class -----------------------------------------------


def _domain_setup(tmp_path):
    from duckduck.pipeline import Pipelines

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "hosts.csv").write_text("hostname,risk\nweb-1,70\ndb-1,40\n")
    (tmp_path / "data" / "people.csv").write_text("user,team\nana,sec\nbo,ops\n")
    config = tmp_path / "duckduck.json"
    config.write_text(json.dumps({
        "services": {"files": {"connector": "files", "path": "data", "table_prefix": ""},
                     "raw": {"connector": "files", "path": "lake/raw/corp", "table_prefix": "raw"}},
        "lake": {"layers": {"raw": "lake/raw", "silver": "lake/silver"}, "state": "lake/_state"}}))
    domain = tmp_path / "raw_corp.json"
    domain.write_text(json.dumps({
        "domain": "corp", "layer": "raw", "sip": False,
        "tables": {"hosts": {"primary_key": "hostname", "sql": "SELECT * FROM hosts", "target": {"mode": "append"}},
                 "people": {"primary_key": "user", "sql": "SELECT * FROM people",
                            "target": {"table_name": "staff", "mode": "overwrite"}}}}))
    return Pipelines(config=str(config)), domain


def test_a_domain_file_holds_several_tables_each_its_own(tmp_path):
    pipelines, file = _domain_setup(tmp_path)
    raw = pipelines.domain(str(file))
    assert raw.tables == ["hosts", "people"] and raw.name == "corp"
    hosts, people = raw.spec("hosts"), raw.spec("people")
    assert hosts.name == "raw_corp_hosts" and people.name == "raw_corp_people"  # state / sip names per table
    assert hosts.targets[0].table == "corp.hosts" and people.targets[0].table == "corp.staff"
    assert hosts.targets[0].mode == "append" and people.targets[0].mode == "overwrite"  # each table its own
    runs = raw.run()
    assert [r.pipeline for r in runs] == ["raw_corp_hosts", "raw_corp_people"]
    assert (tmp_path / "lake" / "raw" / "corp" / "staff").is_dir()
    assert "pipeline raw_corp_people" in runs.report()
    one = raw.run("hosts")
    assert len(one) == 1 and one[0].writes[0]["rows"] == 2


def test_a_table_reads_what_the_one_before_wrote(tmp_path):
    pipelines, file = _domain_setup(tmp_path)
    silver = pipelines.domain({"domain": "corp", "layer": "silver", "sip": False,
                               "tables": {"hosts": {"primary_key": "hostname", "sql": "SELECT * FROM raw_hosts",
                                                  "load": "incremental", "target": {"mode": "merge"}}}},
                              base_dir=str(tmp_path))
    pipelines.domain(str(file)).run("hosts")  # lake/raw/corp/hosts exists only now
    run = silver.run()[0]
    assert run.writes[0]["rows"] == 2 and run.load["type"] == "incremental"


def test_domain_files_say_what_is_wrong(tmp_path):
    pipelines, file = _domain_setup(tmp_path)
    with pytest.raises(PipelineError, match="'domain' names the group"):
        pipelines.domain({"domain": "a-b", "tables": {"a": {"sql": "SELECT 1"}}})
    with pytest.raises(PipelineError, match="\\['load', 'target'\\] go inside each table — a domain file shares only"):
        pipelines.domain({"domain": "d", "load": "full", "target": {"mode": "append"}, "tables": {"a": {"sql": "SELECT 1"}}})
    with pytest.raises(PipelineError, match="\"domain_description\" — \"description\" belongs to each table"):
        pipelines.domain({"domain": "d", "description": "x", "tables": {"a": {"sql": "SELECT 1"}}})
    with pytest.raises(PipelineError, match="'tables' is an object"):
        pipelines.domain({"domain": "d", "tables": []})
    with pytest.raises(PipelineError, match="table 'b' of the domain: unknown key"):
        pipelines.domain({"domain": "d", "tables": {"b": {"sql": "SELECT 1", "nope": 1}}})
    with pytest.raises(PipelineError, match="no table 'x' here \\(tables: hosts, people\\)"):
        pipelines.domain(str(file)).run("x")
    with pytest.raises(PipelineError, match="has 2 tables \\(hosts, people\\) — name one"):
        pipelines.run(str(file))


def test_the_cli_runs_a_domain_or_one_of_its_tables(tmp_path, capsys):
    from duckduck.pipeline.__main__ import main

    _, file = _domain_setup(tmp_path)
    config = str(tmp_path / "duckduck.json")
    assert main(["run", str(file), "--config", config, "--table", "people"]) == 0
    out = capsys.readouterr().out
    assert "pipeline raw_corp_people" in out and "raw_corp_hosts" not in out
    assert main(["plan", str(file), "--config", config]) == 0
    out = capsys.readouterr().out
    assert "pipeline raw_corp_hosts" in out and "pipeline raw_corp_people" in out


def test_a_table_overrides_the_domain_layer_and_sip_of_its_file(tmp_path):
    pipelines, _ = _domain_setup(tmp_path)
    d = pipelines.domain({
        "domain": "corp", "layer": "raw", "sip": {"rate": 0.5, "max_rows": 10},
        "tables": {"a": {"sql": "SELECT 1 AS id", "primary_key": "id"},
                 "b": {"sql": "SELECT 1 AS id", "primary_key": "id", "layer": "silver", "domain": "hr",
                       "sip": {"watch": [1]}},
                 "c": {"sql": "SELECT 1 AS id", "primary_key": "id", "sip": False},
                 "d": {"sql": "SELECT 1 AS id", "primary_key": "id", "target": {"path": str(tmp_path / "out")}}}},
        base_dir=str(tmp_path))
    a, b, c, dd = (d.spec(j) for j in "abcd")
    assert (a.name, a.targets[0].table) == ("raw_corp_a", "corp.a") and a.sip.max_rows == 10
    assert (b.name, b.targets[0].table) == ("silver_hr_b", "hr.b")
    assert b.sip.max_rows == 10 and b.sip.watch == ["1"]  # its own sip over the file's, key by key
    assert not c.sip.enabled
    assert dd.targets[0].path == str(tmp_path / "out")  # a table with its own path keeps it


def test_a_domain_is_optional(tmp_path):
    pipelines, _ = _domain_setup(tmp_path)
    d = pipelines.domain({"layer": "raw", "tables": {"x": {"sql": "SELECT 1 AS id", "primary_key": "id",
                                                          "target": {"database": "misc"}}}},
                         base_dir=str(tmp_path))
    assert d.spec("x").name == "raw_x" and d.spec("x").targets[0].table == "misc.x"

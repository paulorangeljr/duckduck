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
    spec = {"pipeline": "d", "primary_key": "id", "sql": "SELECT * FROM (VALUES (1, 'a'), (2, 'b')) t(id, v)",
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
    assert nb.views == ["bronze", "enriched", "people"]


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


def test_the_example_runs_bronze_to_gold_and_the_sip_spans_both_pipelines(tmp_path, capsys):
    import runpy
    import shutil

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for part in ("local", "pipeline"):
        shutil.copytree(os.path.join(repo, "examples", part), tmp_path / "examples" / part,
                        ignore=shutil.ignore_patterns("lake", "__pycache__"))
    runpy.run_path(str(tmp_path / "examples" / "pipeline" / "run.py"), run_name="__main__")
    out = capsys.readouterr().out
    assert "pipeline assets_silver" in out and "pipeline gold_risk" in out
    way = read_sip(str(tmp_path / "examples" / "pipeline" / "lake" / "_sip"), key="web-0001")
    assert list(way["stage"]) == ["bronze", "silver", "exposed", "risk_by_department"]
    assert way["event"].iloc[-1] == "grouped"
    plan = plan_pipeline(str(tmp_path / "examples" / "pipeline" / "gold_risk.json"))
    assert plan.needed == ["exposed", "risk_by_department"] and len(plan.queries) == 1
    runpy.run_path(str(tmp_path / "examples" / "pipeline" / "python_api.py"), run_name="__main__")
    assert "dry run (nothing written)" in capsys.readouterr().out


def test_a_write_reports_no_internal_details(duck, tmp_path):
    run = run_pipeline({"pipeline": "r", "sql": "SELECT 1 AS id", "target": str(tmp_path / "out")}, duck=duck)
    assert not any(k.startswith("_") for k in run.writes[0]) and "files:" not in run.report()

"""SparkJob: the engineer writes transform() in PySpark; reading, the target, the load, the state and the audit
columns are the framework's (the same runner as a SQL pipeline). The contract tests run without Spark; the ones
taking the ``spark`` fixture run with DUCKDUCK_SPARK_TESTS=1."""

import json
import os
import shutil
import textwrap

import pytest

from duckduck.pipeline import Pipelines, SparkJob
from duckduck.pipeline.job import jobs_in
from duckduck.pipeline.spec import PipelineError

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


class Risk(SparkJob):
    pipeline = "gold_risk_job"
    inputs = {"assets": "silver.inventory.assets", "owners": "silver.inventory.owners"}
    target = {"layer": "gold", "database": "inventory", "table_name": "risk_job", "mode": "overwrite"}
    primary_key = ["department", "os"]
    parameters = {"min_risk": 50}

    def transform(self, assets, owners):
        from pyspark.sql import functions as F

        exposed = (assets.filter(F.col("risk") >= int(self.params["min_risk"]))
                   .join(owners.select("ip", "owner", "department"), "ip", "left"))
        return (exposed.groupBy(F.coalesce("department", F.lit("unknown")).alias("department"), "os")
                .agg(F.count("*").alias("hosts"), F.round(F.avg("risk"), 1).alias("avg_risk")))


def risk_by_department(assets, owners, params):
    """The same transform as a plain function — what the engineer tests and steps through on its own."""
    from pyspark.sql import functions as F

    exposed = (assets.filter(F.col("risk") >= int(params["min_risk"]))
               .join(owners.select("ip", "owner", "department"), "ip", "left"))
    return (exposed.groupBy(F.coalesce("department", F.lit("unknown")).alias("department"), "os")
            .agg(F.count("*").alias("hosts"), F.round(F.avg("risk"), 1).alias("avg_risk")))


class RiskFn(SparkJob):
    pipeline = "gold_risk_fn"
    inputs = {"assets": "silver.inventory.assets", "owners": "silver.inventory.owners"}
    target = {"layer": "gold", "database": "inventory", "table_name": "risk_fn", "mode": "overwrite"}
    primary_key = ["department", "os"]
    parameters = {"min_risk": 50}
    transform = risk_by_department


# ---- the contract: no Spark needed --------------------------------------------------------------------------

def test_the_framework_methods_cant_be_overridden():
    with pytest.raises(TypeError, match="redefines run, spec .*transform"):
        class Sneaky(SparkJob):  # noqa: F841
            inputs = {"a": "silver.x.y"}

            def transform(self, a):
                return a

            def run(self, *a, **k):
                pass

            def spec(self, *a, **k):
                pass


def test_transform_is_required_and_takes_the_inputs_by_name():
    class NoTransform(SparkJob):
        inputs = {"a": "silver.x.y"}

    with pytest.raises(TypeError, match="abstract method transform"):
        NoTransform()

    class Mismatch(SparkJob):
        inputs = {"assets": "silver.inventory.assets"}
        target = "out"

        def transform(self, asset):
            return asset

    with pytest.raises(PipelineError, match=r"transform\(asset\) doesn't match inputs \(assets\)"):
        Mismatch().spec()


class _Frame:  # enough of a Spark DataFrame for the contract tests
    def __init__(self, columns, rows=()):
        self.columns, self.rows = list(columns), list(rows)

    def createOrReplaceTempView(self, name):
        pass


def test_transform_can_be_a_function_outside_the_class_and_helpers_are_the_engineers():
    def tag(assets, params, spark=None):
        return _Frame(assets.columns + ["tag"], [(*r, params["label"]) for r in assets.rows])

    class Tagged(SparkJob):
        inputs = {"assets": "silver.inventory.assets"}
        target = "out"
        primary_key = "hostname"
        parameters = {"label": "x"}
        transform = tag

        def helper(self):  # any other method is the engineer's
            return "mine"

    job = Tagged()
    assert job.transform is tag and job.helper() == "mine"
    job.spec()  # params / spark aren't inputs: the signature matches
    out = job.debug(inputs={"assets": _Frame(["hostname"], [("web",)])}, params={"label": "y"})
    assert out.columns == ["hostname", "tag"] and out.rows == [("web", "y")]
    assert job.params["pipeline"] == "tagged" and "run_date" in job.params  # the built-ins, as in a run

    with pytest.raises(PipelineError, match=r"missing \['assets'\]"):
        job.debug(inputs={})
    with pytest.raises(PipelineError, match="has no \\['hostname'\\]"):  # the run's checks run in debug too
        Tagged().debug(inputs={"assets": _Frame(["ip"])})

    class Method(SparkJob):  # a method still gets self, and params when it asks
        inputs = {"a": "silver.x.y"}

        def transform(self, a, params):
            return _Frame(a.columns + [self.helper(params)])

        def helper(self, params):
            return params["pipeline"]

    assert Method().debug(inputs={"a": _Frame(["c"])}).columns == ["c", "method"]

    class Reserved(SparkJob):
        inputs = {"params": "silver.x.y"}

        def transform(self, params):
            return params

    with pytest.raises(PipelineError, match="nor params / spark"):
        Reserved().settings_dict()
    with pytest.raises(TypeError, match="redefines debug"):
        class Sneaky(SparkJob):  # noqa: F841
            inputs = {"a": "silver.x.y"}
            transform = tag

            def debug(self, *a, **k):
                pass


def test_a_job_is_a_pipeline_whose_last_step_is_transform():
    data = Risk().settings_dict()
    assert data["engine"] == "spark" and data["pipeline"] == "gold_risk_job"
    assert data["sql"] == ["CREATE VIEW assets AS SELECT * FROM silver.inventory.assets",
                           "CREATE VIEW owners AS SELECT * FROM silver.inventory.owners",
                           "CREATE VIEW gold_risk_job AS SELECT * FROM assets, owners"]
    assert data["keys"] == {"gold_risk_job": ["department", "os"]} and data["parameters"] == {"min_risk": 50}

    class WithQuery(SparkJob):  # a query input keeps its filters (they reach the source)
        inputs = {"open": "SELECT * FROM servicenow.incident WHERE state = '1'"}
        settings = {"layer": "gold", "schema_evolution": False}
        target = "out"

        def transform(self, open):
            return open

    data = WithQuery().settings_dict()
    assert data["pipeline"] == "with_query" and data["schema_evolution"] is False
    assert data["sql"][0] == "CREATE VIEW open AS SELECT * FROM servicenow.incident WHERE state = '1'"


def test_inputs_can_be_table_names_each_argument_named_like_its_table():
    class Cmdb(SparkJob):
        inputs = ["silver.cmdb.assets_cmdb_ci", "silver.inventory.owners"]
        target = "out"

        def transform(self, assets_cmdb_ci, owners):
            return assets_cmdb_ci

    data = Cmdb().settings_dict()
    assert data["sql"] == ["CREATE VIEW assets_cmdb_ci AS SELECT * FROM silver.cmdb.assets_cmdb_ci",
                           "CREATE VIEW owners AS SELECT * FROM silver.inventory.owners",
                           "CREATE VIEW cmdb AS SELECT * FROM assets_cmdb_ci, owners"]
    seen = Cmdb().debug(inputs={"assets_cmdb_ci": _Frame(["ip"]), "owners": _Frame(["ip"])})
    assert seen.columns == ["ip"]

    class Twice(SparkJob):
        inputs = ["silver.a.assets", "raw.b.assets"]

        def transform(self, assets):
            return assets

    with pytest.raises(PipelineError, match="two inputs are tables named assets"):
        Twice().settings_dict()

    class Query(SparkJob):
        inputs = ["SELECT * FROM silver.a.b"]

        def transform(self, b):
            return b

    with pytest.raises(PipelineError, match="give a query a name of its own"):
        Query().settings_dict()


def test_bad_declarations_name_the_fix():
    class NoInputs(SparkJob):
        def transform(self):
            return None

    with pytest.raises(PipelineError, match="'inputs' names what transform"):
        NoInputs().settings_dict()

    class Clash(SparkJob):
        inputs = {"a": "silver.x.y"}
        settings = {"engine": "duckdb"}

        def transform(self, a):
            return a

    with pytest.raises(PipelineError, match="settings can't set \\['engine'\\]"):
        Clash().settings_dict()

    class Odd(SparkJob):
        inputs = {"a": "not a table; DROP"}

        def transform(self, a):
            return a

    with pytest.raises(PipelineError, match="neither a table name nor a query"):
        Odd().settings_dict()


def test_a_py_file_of_jobs_is_found_for_the_cli(tmp_path):
    (tmp_path / "jobs.py").write_text(textwrap.dedent("""
        from duckduck.pipeline import SparkJob

        class Base(SparkJob):  # abstract: no transform — not a job
            pass

        class One(SparkJob):
            inputs = {"a": "silver.x.y"}
            def transform(self, a):
                return a
    """))
    assert [c.__name__ for c in jobs_in(str(tmp_path / "jobs.py"))] == ["One"]
    with pytest.raises(PipelineError, match="no job file"):
        jobs_in(str(tmp_path / "missing.py"))


# ---- on a local Spark --------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def spark():
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    try:
        session = (SparkSession.builder.master("local[1]").appName("duckduck-job-tests")
                   .config("spark.ui.enabled", "false").config("spark.ui.showConsoleProgress", "false")
                   .config("spark.sql.shuffle.partitions", "2").getOrCreate())
    except Exception as exc:  # noqa: BLE001 — no Java…
        pytest.skip(f"no local Spark: {exc}")
    yield session
    session.stop()


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """The example's raw and silver, run with DuckDB — the gold job reads them."""
    shutil.copytree(os.path.join(EXAMPLES, "pipeline"), tmp_path / "pipeline",
                    ignore=shutil.ignore_patterns("lake"))
    shutil.copytree(os.path.join(EXAMPLES, "local"), tmp_path / "local")
    monkeypatch.chdir(tmp_path / "pipeline")
    p = Pipelines(config="duckduck.pipeline.json")
    p.domain("raw_inventory.json").run()
    p.domain("silver_inventory.json").run()
    return tmp_path / "pipeline"


def _rows(path):
    import duckdb

    return duckdb.sql(f"SELECT department, os, hosts, avg_risk FROM '{path}/**/*.parquet' ORDER BY ALL").fetchall()


def test_the_pyspark_gold_writes_what_the_sql_gold_does(spark, lake):
    Pipelines(config="duckduck.pipeline.json").domain("gold_risk.json").run()
    run = Risk().run(spark=spark, config_path="duckduck.pipeline.json")
    assert run.engine == "spark" and run.writes[0]["target"].endswith("gold/inventory/risk_job")
    sql_gold, job_gold = _rows(lake / "lake/gold/inventory/risk_by_department"), _rows(lake / "lake/gold/inventory/risk_job")
    assert job_gold == sql_gold and len(job_gold) > 0
    import duckdb

    cols = [r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{lake}/lake/gold/inventory/risk_job/**/*.parquet'").fetchall()]
    assert {"_loaded_at", "_load_date", "_run_id"} <= set(cols)  # the framework's audit columns
    state = json.loads((lake / "lake/_state/gold_risk_job.json").read_text())
    assert state["last_run_id"] == run.run_id


def test_the_result_is_checked_before_anything_is_written(spark, lake):
    class Pandas(SparkJob):
        inputs = {"assets": "silver.inventory.assets"}
        target = {"path": "out_pandas"}

        def transform(self, assets):
            return assets.toPandas()

    class Rules(SparkJob):
        inputs = {"assets": "silver.inventory.assets"}
        target = {"path": "out_rules"}

        def transform(self, assets):
            return assets

        def check(self, df):
            return ["3 hosts with risk above 100"]

    class NoKey(SparkJob):
        inputs = {"assets": "silver.inventory.assets"}
        target = {"path": "out_nokey"}
        primary_key = ["host_id"]

        def transform(self, assets):
            return assets

    for cls, message in ((Pandas, "returns the table as a Spark DataFrame"),
                         (Rules, "check\\(\\) found 1 problem.*risk above 100"),
                         (NoKey, "has no \\['host_id'\\]")):
        with pytest.raises(PipelineError, match=message):
            cls().run(spark=spark, config_path="duckduck.pipeline.json")
    assert not any((lake / p).exists() for p in ("out_pandas", "out_rules", "out_nokey"))


def test_an_incremental_job_reads_only_what_is_new(spark, lake):
    from pyspark.sql import functions as F

    class HighRisk(SparkJob):
        pipeline = "gold_high_risk"
        inputs = {"assets": "silver.inventory.assets"}
        load = "incremental"
        target = {"layer": "gold", "database": "inventory", "table_name": "high_risk", "mode": "append"}
        primary_key = ["hostname"]

        def transform(self, assets):
            return assets.filter(F.col("risk") >= 80).select("hostname", "risk")

    first = HighRisk().run(spark=spark, config_path="duckduck.pipeline.json")
    assert first.load["since"] is None or "first run" in first.report()
    again = HighRisk().run(spark=spark, config_path="duckduck.pipeline.json")
    assert again.writes[0].get("skipped") and "nothing newer" in again.report()
    p = Pipelines(config="duckduck.pipeline.json")
    p.domain("raw_inventory.json").run("assets")
    p.domain("silver_inventory.json").run("assets")
    third = HighRisk().run(spark=spark, config_path="duckduck.pipeline.json")
    assert not third.writes[0].get("skipped") and "nothing newer" not in third.report()


def test_a_function_transform_writes_the_same_and_debug_writes_nothing(spark, lake):
    from pyspark.sql import functions as F

    Risk().run(spark=spark, config_path="duckduck.pipeline.json")
    ran = RiskFn().run(spark=spark, config_path="duckduck.pipeline.json")
    assert _rows(lake / "lake/gold/inventory/risk_fn") == _rows(lake / "lake/gold/inventory/risk_job")

    df = RiskFn().debug(spark=spark, config_path="duckduck.pipeline.json")  # what a run would read; no write
    assert sorted(map(tuple, df.select("department", "os", "hosts", "avg_risk").collect())) == \
        _rows(lake / "lake/gold/inventory/risk_fn")
    state = json.loads((lake / "lake/_state/gold_risk_fn.json").read_text())
    assert state["last_run_id"] == ran.run_id and not state.get("pending")  # the state is the run's, not debug's

    assets = spark.createDataFrame([("10.0.0.1", "linux", 90), ("10.0.0.2", "linux", 10)], ["ip", "os", "risk"])
    owners = spark.createDataFrame([("10.0.0.1", "ana", "it")], ["ip", "owner", "department"])
    assert [tuple(r) for r in risk_by_department(assets, owners, {"min_risk": 50}).collect()] == \
        [("it", "linux", 1, 90.0)]  # the function alone, no framework
    out = RiskFn().debug(inputs={"assets": assets, "owners": owners}, params={"min_risk": 5})
    assert out.agg(F.sum("hosts")).first()[0] == 2  # your DataFrames, the job's checks


def test_the_example_job_writes_the_sql_gold(spark, lake):
    import importlib.util

    Pipelines(config="duckduck.pipeline.json").domain("gold_risk.json").run()
    spec = importlib.util.spec_from_file_location("gold_risk_job_example", lake / "gold_risk_job.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RiskByDepartment().run(spark=spark, config_path="duckduck.pipeline.json")
    assert _rows(lake / "lake/gold/inventory/risk_by_department_spark") == \
        _rows(lake / "lake/gold/inventory/risk_by_department")

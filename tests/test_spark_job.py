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

"""Gold in PySpark: the same table as gold_risk.json (risky hosts per department and OS).

The engineer writes the transformation (and, optionally, check()); reading the silver tables, the gold target, the
audit columns, the state and the run are the framework's:

    python -m duckduck.pipeline run examples/pipeline/gold_risk_job.py --config examples/pipeline/duckduck.pipeline.json

The transformation is a plain function: test it with DataFrames of your own, put a breakpoint in it, or run the job
without writing anything — ``RiskByDepartment().debug(config_path=…)`` returns the DataFrame a run would write.
"""
from pyspark.sql import functions as F

from duckduck.pipeline import SparkJob


def exposed_hosts(assets, owners, min_risk):
    """A helper of the engineer's: risky hosts with their owner."""
    return (assets.filter(F.col("risk") >= int(min_risk))
            .join(owners.select("ip", "owner", "department"), "ip", "left"))


def risk_by_department(assets, owners, params):
    """silver's assets and owners → hosts and average risk per department and OS."""
    return (exposed_hosts(assets, owners, params["min_risk"])
            .groupBy(F.coalesce("department", F.lit("unknown")).alias("department"), "os")
            .agg(F.count("*").alias("hosts"), F.round(F.avg("risk"), 1).alias("avg_risk")))


class RiskByDepartment(SparkJob):
    pipeline = "gold_risk_spark"
    description = "Risky hosts per department and OS, from silver's assets and owners."
    inputs = ["silver.inventory.assets", "silver.inventory.owners"]  # → the arguments assets, owners
    target = {"layer": "gold", "database": "inventory", "table_name": "risk_by_department_spark", "mode": "overwrite"}
    primary_key = ["department", "os"]
    parameters = {"min_risk": 50}

    transform = risk_by_department

    def check(self, df):
        bad = df.filter((F.col("avg_risk") < self.params["min_risk"]) | (F.col("hosts") <= 0)).count()
        return [f"{bad} group(s) below min_risk or empty"] if bad else []

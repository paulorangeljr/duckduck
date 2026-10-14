"""Gold in PySpark: the same table as gold_risk.json (risky hosts per department and OS).

The engineer writes transform() (and, optionally, check()); reading the silver tables, the gold target, the
audit columns, the state and the run are the framework's:

    python -m duckduck.pipeline run examples/pipeline/gold_risk_job.py --config examples/pipeline/duckduck.pipeline.json
"""
from pyspark.sql import functions as F

from duckduck.pipeline import SparkJob


class RiskByDepartment(SparkJob):
    pipeline = "gold_risk_spark"
    description = "Risky hosts per department and OS, from silver's assets and owners."
    inputs = {"assets": "silver.inventory.assets", "owners": "silver.inventory.owners"}
    target = {"layer": "gold", "database": "inventory", "table_name": "risk_by_department_spark", "mode": "overwrite"}
    primary_key = ["department", "os"]
    parameters = {"min_risk": 50}

    def transform(self, assets, owners):
        exposed = (assets.filter(F.col("risk") >= int(self.params["min_risk"]))
                   .join(owners.select("ip", "owner", "department"), "ip", "left"))
        return (exposed.groupBy(F.coalesce("department", F.lit("unknown")).alias("department"), "os")
                .agg(F.count("*").alias("hosts"), F.round(F.avg("risk"), 1).alias("avg_risk")))

    def check(self, df):
        bad = df.filter((F.col("avg_risk") < 50) | (F.col("hosts") <= 0)).count()
        return [f"{bad} group(s) below min_risk or empty"] if bad else []

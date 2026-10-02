"""
The pipeline from Python — everything the command line does, through the
Pipelines class:

    python -m duckduck.pipeline plan FILE [--table T] →  pipelines.domain(FILE).plan(T)
    python -m duckduck.pipeline run FILE [--table T]  →  pipelines.domain(FILE).run(T)
    python -m duckduck.pipeline sip STORE --key K     →  pipelines.sip(key=K)

Runs offline over examples/pipeline (writes to examples/pipeline/lake/):

    python examples/pipeline/python_api.py
"""

import os

from duckduck.common.logs import set_verbose
from duckduck.pipeline import PipelineError, Pipelines

HERE = os.path.dirname(os.path.abspath(__file__))

# the connectors the SQL reads and the "lake" settings (your duckduck.json)
pipelines = Pipelines(config=os.path.join(HERE, "duckduck.pipeline.json"))

raw = pipelines.domain(os.path.join(HERE, "raw_inventory.json"))
silver = pipelines.domain(os.path.join(HERE, "silver_inventory.json"))
gold = pipelines.domain(os.path.join(HERE, "gold_risk.json"))


def main() -> None:
    set_verbose(False)  # True / "info" / "debug" = the CLI's -v / -vv
    print(raw.tables, silver.tables, gold.tables)  # ['assets', 'owners'] ['assets', 'owners'] ['gold_risk']

    # 1. plan: what a table's run would do — steps, what each reads, the key's way, the targets. Nothing runs.
    plan = gold.plan("gold_risk", params={"min_risk": 70})
    print(plan.report())  # the gold notebook's WITH steps show up as the run's steps
    print("steps run:", plan.needed, "| followed by the sip:", plan.sampled)

    # 2. run. params = --param, run_id = --run-id, dry_run = --dry-run
    try:
        raw.run(params={"run_date": "2026-10-01"})  # every raw table
        silver.run()                                # what raw loaded since silver's last run
        runs = gold.run(params={"min_risk": 70})
    except PipelineError as exc:  # a problem in a file or the SQL, said in a sentence
        print("pipeline error:", exc)
        raise
    print(runs.report())

    # the run's results, as data
    run = runs[0]
    for w in run.writes:
        print("wrote", w["view"], "→", w["target"], w.get("rows"), "rows")
    print(run.sip[["stage", "key", "stage_key", "event"]].head())  # this run's sip, a DataFrame

    # a dry run: steps and sip, nothing written
    print(gold.run(params={"min_risk": 90}, dry_run=True).report())

    # 3. the sip kept in the lake: one key's way through every table
    way = pipelines.sip(key="web-0001")
    print(way[["pipeline", "run_id", "stage", "event", "stage_key", "row"]].to_string(index=False))

    # Spark instead of DuckDB: "engine": "spark" in the file, and the session given once
    #   Pipelines(config=..., spark=spark)
    #
    # A catalog built in code (instead of "catalogs" in the JSON / duckduck.json):
    #   from duckduck.pipeline import run_pipeline
    #   from duckduck.pipeline.catalogs import GlueCatalog
    #   lake = GlueCatalog("lake", region="us-east-1", warehouse="s3://my-lake/warehouse/")
    #   run_pipeline(silver.spec("assets"), catalogs={"lake": lake}, config_path=pipelines.config)


if __name__ == "__main__":
    main()

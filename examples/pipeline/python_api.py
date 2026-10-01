"""
The pipeline from Python — everything the command line does, as calls:

    python -m duckduck.pipeline plan FILE      →  plan_pipeline(FILE)
    python -m duckduck.pipeline run FILE ...   →  run_pipeline(FILE, ...)
    python -m duckduck.pipeline sip STORE ...  →  read_sip(STORE, ...)

Runs offline over examples/pipeline (writes to examples/pipeline/lake/):

    python examples/pipeline/python_api.py
"""

import os

from duckduck import DuckAPI
from duckduck.logs import set_verbose
from duckduck.pipeline import PipelineError, plan_pipeline, read_sip, run_pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
SILVER = os.path.join(HERE, "assets_silver.json")
GOLD = os.path.join(HERE, "gold_risk.json")
CONFIG = os.path.join(HERE, "duckduck.pipeline.json")  # the connectors the SQL reads (your duckduck.json)
SIP_STORE = os.path.join(HERE, "lake", "_sip")


def connect() -> DuckAPI:
    duck = DuckAPI()
    duck.auto_register(config_path=CONFIG)
    return duck


def main() -> None:
    os.makedirs(os.path.join(HERE, "lake"), exist_ok=True)
    set_verbose(False)  # True / "info" / "debug" = the CLI's -v / -vv

    # 1. plan: what a run would do — views, what each reads, the key's way, the targets. Nothing runs.
    plan = plan_pipeline(GOLD, params={"min_risk": 70})
    print(plan.report())
    print("views run:", plan.needed, "| followed by the sip:", plan.sampled)

    # 2. run. params = --param, run_id = --run-id, dry_run = --dry-run; duck = the connectors
    #    (without duck=, it auto_registers from config_path / DUCKDUCK_CONFIG / duckduck.json)
    try:
        silver = run_pipeline(SILVER, duck=connect(), params={"run_date": "2026-10-01"})
        gold = run_pipeline(GOLD, duck=connect(), params={"min_risk": 70})
    except PipelineError as exc:  # a problem in the file or the SQL, said in a sentence
        print("pipeline error:", exc)
        raise
    print(silver.report())
    print(gold.report())

    # the run's results, as data
    for w in gold.writes:
        print("wrote", w["view"], "→", w["target"], w.get("rows"), "rows")
    print(gold.sip[["stage", "key", "stage_key", "event"]].head())  # this run's sip, a DataFrame

    # a dry run: views and sip, nothing written
    trial = run_pipeline(GOLD, duck=connect(), params={"min_risk": 90}, dry_run=True)
    print(trial.report())

    # 3. sip: every event kept in the store; narrow it by pipeline, key or run
    way = read_sip(SIP_STORE, key="web-0001")
    print(way[["pipeline", "run_id", "stage", "event", "stage_key", "row"]].to_string(index=False))

    # Spark instead of DuckDB: the same file with "engine": "spark", and the session passed in
    #   run_pipeline(GOLD, duck=connect(), spark=spark)
    #
    # A catalog built in code (instead of "catalogs" in the JSON / duckduck.json):
    #   from duckduck.pipeline.catalogs import GlueCatalog
    #   lake = GlueCatalog("lake", region="us-east-1", warehouse="s3://my-lake/warehouse/")
    #   run_pipeline(SILVER, duck=connect(), catalogs={"lake": lake})


if __name__ == "__main__":
    main()

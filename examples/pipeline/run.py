"""
Bronze, silver and gold as three independent jobs, fully offline — bronze can
run more often than silver; silver reads only what bronze loaded since its
own last run (its watermark, kept in lake/_state):

    python examples/pipeline/run.py

Writes to examples/pipeline/lake/ (git-ignored): lake/<layer>/<database>/<table> (the "lake" block of
duckduck.pipeline.json), lake/_state and lake/_sip.
"""

import os

from duckduck import DuckAPI
from duckduck.pipeline import read_sip, run_pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "duckduck.pipeline.json")


def connect() -> DuckAPI:
    duck = DuckAPI()
    duck.auto_register(config_path=CONFIG)  # the lake connector lists lake/ as it is now
    return duck


def job(name: str):
    run = run_pipeline(os.path.join(HERE, f"{name}.json"), duck=connect())
    print(run.report())
    return run


def main() -> None:
    for layer in ("raw", "silver"):  # the files connectors over the layers need their folders to exist
        os.makedirs(os.path.join(HERE, "lake", layer, "inventory"), exist_ok=True)
    job("assets_bronze")   # bronze, every 15 minutes say…
    job("assets_bronze")
    job("assets_silver")   # …silver every hour: both bronze loads, deduplicated by host
    job("assets_silver")   # nothing new in bronze since: the watermark doesn't move
    job("gold_risk")

    way = read_sip(os.path.join(HERE, "lake", "_sip"), key="web-0001")
    print(way[["pipeline", "stage", "event", "stage_key", "row"]].to_string(index=False))


if __name__ == "__main__":
    main()

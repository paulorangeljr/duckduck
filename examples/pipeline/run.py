"""
Bronze → silver → gold, fully offline, with the sip following a few hosts all
the way (python -m duckduck.pipeline does the same from the command line):

    python examples/pipeline/run.py

Writes to examples/pipeline/lake/ (git-ignored): the tables and lake/_sip.
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


def main() -> None:
    os.makedirs(os.path.join(HERE, "lake"), exist_ok=True)
    silver = run_pipeline(os.path.join(HERE, "assets_silver.json"), duck=connect())
    print(silver.report())
    gold = run_pipeline(os.path.join(HERE, "gold_risk.json"), duck=connect())  # reads lake_silver_assets
    print(gold.report())

    way = read_sip(os.path.join(HERE, "lake", "_sip"), key="web-0001")
    print(way[["pipeline", "run_id", "stage", "event", "stage_key", "row"]].to_string(index=False))


if __name__ == "__main__":
    main()

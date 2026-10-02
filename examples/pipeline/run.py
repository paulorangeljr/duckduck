"""
Raw, silver and gold, fully offline — each a file of tables grouped by domain:

    python examples/pipeline/run.py

Writes to examples/pipeline/lake/ (git-ignored): lake/<layer>/<database>/<table>, lake/_state, lake/_sip
(the "lake" block of duckduck.pipeline.json).
"""

import os

from duckduck.pipeline import Pipelines

HERE = os.path.dirname(os.path.abspath(__file__))

# the connectors the SQL reads and the lake settings: duckduck.pipeline.json
pipelines = Pipelines(config=os.path.join(HERE, "duckduck.pipeline.json"))

raw = pipelines.domain(os.path.join(HERE, "raw_inventory.json"))        # tables: assets, owners
silver = pipelines.domain(os.path.join(HERE, "silver_inventory.json"))  # tables: assets, owners
gold = pipelines.domain(os.path.join(HERE, "gold_risk.json"))           # one table: gold_risk


def main() -> None:
    print(raw.run().report())            # every raw table — say, every 15 minutes
    print(raw.run("assets").report())    # just one table: assets again
    print(silver.run().report())         # every hour: what raw loaded since silver's last run, one row per key
    print(silver.run("assets").report()) # nothing new since: its watermark doesn't move
    print(gold.run().report())

    # one host's way through every table
    way = pipelines.sip(key="web-0001")
    print(way[["pipeline", "stage", "event", "stage_key", "row"]].to_string(index=False))


if __name__ == "__main__":
    main()

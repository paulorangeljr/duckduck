"""
A raw pipeline writing to S3, registered in the Glue Data Catalog — with the
AWS account declared once, in duckduck.aws.json's "lake.aws".

Needs real AWS access (and a ServiceNow instance): copy duckduck.aws.json to
your own duckduck.json, change the buckets, the profile / role and the
ServiceNow instance, then:

    python examples/aws/run.py              # the plan only: where each table goes, the incremental's WHERE
    python examples/aws/run.py --run        # every table
    python examples/aws/run.py --run incident

Install: pip install "duckduck[pipeline,aws,delta]"
"""

import os
import sys

from duckduck.pipeline import Pipelines

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("DUCKDUCK_CONFIG") or os.path.join(HERE, "duckduck.aws.json")

pipelines = Pipelines(config=CONFIG)                              # the connectors + "lake" (S3, Glue, the account)
raw = pipelines.domain(os.path.join(HERE, "raw_servicenow.json"))  # tables: incident, change_request


def main(argv) -> None:
    tables = [a for a in argv if not a.startswith("--")]
    if "--run" not in argv:
        for table in tables or raw.tables:
            print(raw.plan(table).report())  # reads the state in S3 (the watermark), runs nothing
        return
    runs = raw.run(*tables)
    print(runs.report())  # ends with "aws: profile data-prod · us-east-1" — the account it wrote with


if __name__ == "__main__":
    main(sys.argv[1:])

"""
End-to-end demo: answers the 6 MVP questions over the sample sources and
scores them against evaluation.json. Fully offline (lexical decision
engine, rule-based extraction, synthetic data).

    pip install -e ".[semantic]"
    python examples/semantic/demo.py

The sample data is generated relative to a fixed moment (NOW below), so the
expected answers in evaluation.json stay valid on any date — which is why
this script registers the sources itself instead of using
duckduck.local.json (that one generates data relative to the real clock,
for asking your own questions).
"""

import json
import os
from datetime import datetime

from duckduck import DuckAPI
from duckduck.semantic import SemanticSearch, evaluate, load_dataset

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime(2026, 9, 24, 12, 0, 0)


def main() -> None:
    duck = DuckAPI()
    duck.auto_register({
        "samples": {
            "connector": "python",
            "module": os.path.join(HERE, "sample_sources.py"),
            "kwargs": {"now": NOW.isoformat()},
            "table_prefix": "",
        },
    })
    search = SemanticSearch(os.path.join(HERE, "catalog.yaml"), duck, clock=lambda: NOW)

    cases = load_dataset(os.path.join(HERE, "evaluation.json"))
    for case in cases:
        print("=" * 78)
        print(search.search(case["question"]).report())

    print("=" * 78)
    print(json.dumps(evaluate(search, cases).metrics, indent=2))


if __name__ == "__main__":
    main()

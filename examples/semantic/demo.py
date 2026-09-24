"""
End-to-end demo: runs the MVP question set against the sample sources.

    pip install -e ".[semantic]"
    python examples/semantic/demo.py
"""

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sample_sources import register_sample_sources  # noqa: E402

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import SemanticSearch, evaluate, load_dataset  # noqa: E402

#: Fixed clock so the sample data's relative timestamps (and the
#: expected results in evaluation.json) are stable.
NOW = datetime(2026, 9, 24, 12, 0, 0)


def main() -> None:
    duck = DuckAPI()
    register_sample_sources(duck, NOW)
    search = SemanticSearch(os.path.join(HERE, "catalog.yaml"), duck, clock=lambda: NOW)

    cases = load_dataset(os.path.join(HERE, "evaluation.json"))
    for case in cases:
        result = search.search(case["question"])
        print("=" * 78)
        print(result.question, f"[{result.status}]")
        if result.status != "ok":
            print("  ->", result.clarification)
            continue
        print(result.sql)
        print(result.results.to_string(index=False))

    print("=" * 78)
    print(json.dumps(evaluate(search, cases).metrics, indent=2))


if __name__ == "__main__":
    main()

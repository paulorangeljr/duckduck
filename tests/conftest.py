import os

import pytest

SPARK_ENV = "DUCKDUCK_SPARK_TESTS"


def pytest_collection_modifyitems(config, items):
    """Tests that start a local Spark (the ``spark`` fixture) are slow: off unless DUCKDUCK_SPARK_TESTS=1.
    The Spark contract tests that need no session still run."""
    if os.environ.get(SPARK_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"local Spark tests are off — set {SPARK_ENV}=1 to run them")
    for item in items:
        if "spark" in getattr(item, "fixturenames", ()):
            item.add_marker(skip)

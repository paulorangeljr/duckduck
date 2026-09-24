"""Shared fixtures for the duckduck.semantic tests: the example catalog + sample sources."""

import importlib.util
import os
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(REPO, "examples", "semantic")
CATALOG_PATH = os.path.join(EXAMPLES, "catalog.yaml")
DATASET_PATH = os.path.join(EXAMPLES, "evaluation.json")
NOW = datetime(2026, 9, 24, 12, 0, 0)


def _load_sample_module():
    spec = importlib.util.spec_from_file_location("sample_sources", os.path.join(EXAMPLES, "sample_sources.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sample_sources = _load_sample_module()

"""
What every pipeline shares, set once in ``duckduck.json`` — the top-level
``"lake"`` block:

    "lake": {
      "catalog": "glue",                                  (the catalog targets register in)
      "layers": {"raw": "s3://raw-layer", "silver": "s3://silver-layer", "gold": "s3://gold-layer"},
      "state": "s3://raw-layer/_duckduck/state",          (each job's last run + watermarks)
      "sip_store": "s3://raw-layer/_duckduck/sip",
      "aws": {"profile": "data-prod", "region": "us-east-1"}   (the account the lake is written with — aws.py)
    }

A target then only says ``"layer": "raw", "database": "servicenow",
"table_name": "incident"``: its files go to ``s3://raw-layer/servicenow/incident``
and the table is registered as ``servicenow.incident`` in that catalog.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Optional

from .spec import PipelineError, PipelineSpec

LAKE_KEYS = {"catalog", "layers", "state", "sip_store", "aws", "read_engine", "ingestion_layers", "athena"}


def _find_config(start: Optional[str] = None) -> Optional[str]:
    directory = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(directory, "duckduck.json")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def lake_settings(duck: Any = None, config_path: Optional[str] = None) -> Dict[str, Any]:
    """duckduck.json's ``"lake"`` block (local paths made absolute against the file), or ``{}``."""
    path = config_path or getattr(duck, "_config_path", None) or os.environ.get("DUCKDUCK_CONFIG") or _find_config()
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        lake = json.load(f).get("lake") or {}
    problems = lake_problems(lake)
    if problems:
        raise PipelineError(f"{path}: " + "; ".join(problems))
    base = os.path.dirname(os.path.abspath(path))

    def local(value: str) -> str:
        return value if "://" in value or os.path.isabs(value) else os.path.normpath(os.path.join(base, value))

    out = dict(lake)
    out["layers"] = {k: local(v) for k, v in (lake.get("layers") or {}).items()}
    for k in ("state", "sip_store"):
        if lake.get(k):
            out[k] = local(lake[k])
    return out


def lake_problems(lake: Any) -> list:
    """What's wrong with a ``"lake"`` block (``validate_config`` reports them too)."""
    if not isinstance(lake, dict):
        return ["\"lake\" is an object: {\"layers\": {\"raw\": \"s3://raw-layer\"}, \"catalog\": …, \"state\": …}"]
    problems = [f"lake: unknown key(s) {sorted(set(lake) - LAKE_KEYS)} — it takes {sorted(LAKE_KEYS)}"] \
        if set(lake) - LAKE_KEYS else []
    layers = lake.get("layers", {})
    if not isinstance(layers, dict) or not all(isinstance(v, str) and v for v in layers.values()):
        problems.append("lake.layers maps a layer to its prefix: {\"raw\": \"s3://raw-layer\"}")
    for k in ("catalog", "state", "sip_store"):
        if lake.get(k) is not None and not (isinstance(lake[k], str) and lake[k]):
            problems.append(f"lake.{k} is text")
    from .aws import aws_problems
    from .lakeread import ATHENA_KEYS, read_engine_problem

    problem = read_engine_problem(lake.get("read_engine"), "lake.read_engine")
    if problem:
        problems.append(problem)
    ingest = lake.get("ingestion_layers")
    if ingest is not None:
        if not (isinstance(ingest, list) and ingest and all(isinstance(v, str) for v in ingest)):
            problems.append("lake.ingestion_layers lists the layers that read the sources: [\"raw\"]")
        elif isinstance(layers, dict) and set(ingest) - set(layers):
            problems.append(f"lake.ingestion_layers: {sorted(set(ingest) - set(layers))} isn't in lake.layers")
    athena = lake.get("athena")
    if athena is not None:
        if not isinstance(athena, dict):
            problems.append("lake.athena is an object: {\"workgroup\": \"primary\", \"output_location\": \"s3://…\"}")
        elif set(athena) - ATHENA_KEYS:
            problems.append(f"lake.athena: unknown key(s) {sorted(set(athena) - ATHENA_KEYS)} — it takes "
                            f"{sorted(ATHENA_KEYS)}")
    return problems + aws_problems(lake.get("aws"), "lake.aws")


def with_settings(spec: PipelineSpec, lake: Dict[str, Any]) -> PipelineSpec:
    """The spec with the shared settings filled in: each layer target's path ({prefix}/{database}/{table_name})
    and catalog, and the state / sip store when the file doesn't set its own."""
    spec = copy.copy(spec)
    spec.lake = dict(lake)
    spec.targets = [copy.copy(t) for t in spec.targets]
    spec.sip = copy.copy(spec.sip)
    layers = lake.get("layers") or {}
    for t in spec.targets:
        if t.layer is None:
            continue
        if t.layer not in layers:
            known = ", ".join(sorted(layers)) or "none — add \"lake\": {\"layers\": {…}} to duckduck.json"
            raise PipelineError(f"no layer {t.layer!r} in duckduck.json's \"lake\" (layers: {known})")
        t.path = f"{layers[t.layer].rstrip('/')}/{t.database}/{t.table_name}"
        t.catalog = t.catalog or lake.get("catalog")
        if not t.catalog:
            t.table = None  # no catalog to register in: the files only
    if spec.aws is None and lake.get("aws"):
        spec.aws = dict(lake["aws"])
    if not spec.state and lake.get("state"):
        spec.state = lake["state"]
    if spec.sip.enabled and not spec.sip.store and lake.get("sip_store"):
        spec.sip.store = lake["sip_store"]
    if spec.load.incremental and not spec.state:
        raise PipelineError("an incremental load remembers where the last run stopped: set \"state\" in "
                            "duckduck.json's \"lake\" (or in the pipeline file) — a folder in the lake")
    return spec

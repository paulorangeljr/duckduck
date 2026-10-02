"""
A pipeline file: the JSON that says where the SQL is, which engine runs it,
the key, where results are written and how the sip follows rows.

    {
      "pipeline": "risk_by_region",
      "notebook": "gold/risk_by_region.ipynb",     (or "sql_file": "...sql", or "sql": "SELECT ...")
      "engine": "duckdb",                          (or "spark")
      "primary_key": ["sys_id"],
      "sip": {"rate": 0.001, "store": "s3://lake/_sip/"},
      "target": "s3://lake/gold/risk_by_region/",  (+ "mode", "format", "partition_by")
      "parameters": {"watermark": "{{ run_date - 1d }}"}
    }

Everything about keys, targets and the sip lives here — never in the SQL,
which only says what each view is (``CREATE VIEW name AS SELECT …``).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


class PipelineError(ValueError):
    """Something in the pipeline file or its SQL that has to be fixed before (or instead of) running."""


NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENGINES = ("duckdb", "spark")
MODES = ("append", "overwrite", "overwrite_partitions", "merge")
FORMATS = ("parquet", "delta", "iceberg")
SOURCES = ("notebook", "sql_file", "sql")
KEYS = {"pipeline", "description", "engine", "primary_key", "keys", "sip", "target", "targets", "output",
        "parameters", "catalogs", "catalog", "schema_evolution", "state", "load", "timezone", "audit_columns", "layer", "aws", "read_engine", "virtualized_table", "page_size", *SOURCES}
SIP_KEYS = {"enabled", "rate", "max_rows", "watch", "columns", "mask", "store", "stages", "null_keys"}
TARGET_KEYS = {"path", "table", "format", "mode", "partition_by", "key", "unique", "storage_options", "catalog",
               "schema", "schema_evolution", "layer", "database", "table_name"}
SCHEMA_POLICIES = ("evolve", "fixed", "strict", "overwrite")
BUILTIN_PARAMETERS = ("run_date", "run_at", "run_id", "pipeline", "watermark", "last_success_at", "last_run_id")
EPOCH = "1970-01-01 00:00:00"


def _names(value: Any, what: str) -> List[str]:
    """A column name or a list of them → a non-empty list of names."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise PipelineError(f"{what} is a column name or a list of them (got {value!r})")
    return [v.strip() for v in value]


def is_path(text: str) -> bool:
    """A target written as a path (``s3://…``, ``/data/x``, ``./out``) rather than a catalog table (``silver.x``)."""
    return ("://" in text or text.startswith(("/", "./", "../", "~", "\\"))
            or bool(re.match(r"^[A-Za-z]:[\\/]", text)))


@dataclass
class Target:
    """Where a view's rows are written."""

    view: str
    path: Optional[str] = None  # a folder / prefix in the lake
    table: Optional[str] = None  # a catalog table (spark engine)
    format: Optional[str] = None
    mode: str = "append"
    partition_by: List[str] = field(default_factory=list)
    key: Optional[List[str]] = None  # merge key; default: the view's key
    unique: bool = True  # merge: refuse a source with two rows for one key
    storage_options: Dict[str, str] = field(default_factory=dict)
    catalog: Optional[str] = None  # a catalog of the pipeline's "catalogs" (or duckduck.json's): table = database.table
    layer: Optional[str] = None  # a layer of duckduck.json's "lake": the path is {its prefix}/{database}/{table_name}
    database: Optional[str] = None
    table_name: Optional[str] = None
    schema: Optional[str] = None  # evolve / fixed / strict / overwrite (catalogs.plan_schema); None → the file's
    # schema_evolution (true → evolve, false → fixed)

    @property
    def where(self) -> str:
        if self.catalog and self.table:
            return f"{self.catalog}:{self.table}"
        return self.path or self.table or ""

    def describe(self) -> str:
        fmt = f" ({self.format})" if self.format else ""
        return f"{self.mode} → {self.where}{fmt}"


@dataclass
class SipSpec:
    enabled: bool = False
    rate: float = 0.001
    max_rows: int = 200
    watch: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    mask: List[str] = field(default_factory=list)
    store: Optional[str] = None
    stages: str = "all"  # or "output": only the views written
    null_keys: bool = True

    @property
    def upper(self) -> str:
        """The highest hash prefix (8 hex digits of md5) a sampled key may have."""
        return format(max(0, min(int(self.rate * 16 ** 8), 16 ** 8) - 1), "08x")


@dataclass
class Load:
    """``full``: every run reads everything. ``incremental``: every run reads only what changed since the last
    successful one — ``WHERE col > <its max last time>`` is added to the step that reads the source, and the new
    max of each column is kept in the pipeline's ``state`` for the next run."""

    type: str = "full"
    columns: List[str] = field(default_factory=list)
    initial: Dict[str, Any] = field(default_factory=dict)  # column → the value to start from (none: a full read)
    lookback: Optional[str] = None  # "10m": re-read a little before the watermark (late rows; a merge dedups)
    step: Optional[str] = None  # the step to filter (default: the first one reading from outside the job)

    @property
    def incremental(self) -> bool:
        return self.type == "incremental"


@dataclass
class PipelineSpec:
    name: str
    source: str  # notebook / sql_file / sql
    source_value: Any
    base_dir: str
    engine: str = "duckdb"
    primary_key: Optional[List[str]] = None
    keys: Dict[str, List[str]] = field(default_factory=dict)
    sip: SipSpec = field(default_factory=SipSpec)
    targets: List[Target] = field(default_factory=list)
    output: Optional[str] = None  # the view "target" writes (default: the last view)
    catalogs: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # name → {type: glue|unity|iceberg, …}
    state: Optional[str] = None  # where each run's record (last success, watermarks) is kept: a folder in the lake
    load: Load = field(default_factory=Load)
    aws: Optional[Dict[str, Any]] = None  # the AWS account it writes the lake with (else duckduck.json's lake.aws)
    timezone: str = "UTC"  # the job's clock: {{ run_at }} (with its offset) and {{ run_date }} (its local date)
    audit_columns: List[str] = field(default_factory=lambda: list(AUDIT_COLUMNS))  # added to every row written
    parameters: Dict[str, Any] = field(default_factory=dict)
    description: str = ""
    path: Optional[str] = None  # the JSON file, when read from one
    read_engine: Optional[str] = None  # how a table of the lake is read: duckdb / athena (else lake.read_engine)
    virtualized: Any = False  # past ingestion, tables that aren't the lake's may be read: True or their names
    lake: Dict[str, Any] = field(default_factory=dict)  # duckduck.json's "lake", once with_settings applied it
    page_size: Any = None  # rows per API request during this run: a number (every connector) or {service: n}

    def resolve(self, value: str) -> str:
        """A local path relative to the pipeline file."""
        if "://" in value or os.path.isabs(os.path.expanduser(value)):
            return os.path.expanduser(value)
        return os.path.normpath(os.path.join(self.base_dir, value))


def load_spec(source: Union[str, os.PathLike, Dict[str, Any]], base_dir: Optional[str] = None) -> PipelineSpec:
    """A pipeline from its JSON file (or the parsed dict). Raises ``PipelineError`` naming what to fix."""
    path = None
    if isinstance(source, dict):
        data = source
        base_dir = base_dir or os.getcwd()
    else:
        path = os.path.abspath(os.fspath(source))
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise PipelineError(f"no pipeline file at {path}") from None
        except json.JSONDecodeError as exc:
            raise PipelineError(f"{path} isn't valid JSON: {exc}") from None
        base_dir = base_dir or os.path.dirname(path)
    if not isinstance(data, dict):
        raise PipelineError("a pipeline file is a JSON object")
    unknown = sorted(set(data) - KEYS)
    if unknown:
        raise PipelineError(f"unknown key(s) {unknown} — a pipeline takes {sorted(KEYS)}")

    name = data.get("pipeline")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise PipelineError("'pipeline' is the pipeline's name: letters, digits and _ (e.g. \"orders_daily\")")
    given = [k for k in SOURCES if data.get(k) not in (None, "", [])]
    if len(given) != 1:
        raise PipelineError("say where the SQL is with exactly one of 'notebook' (.ipynb), 'sql_file' (.sql) "
                            f"or 'sql' (the query itself){f' — got {given}' if given else ''}")
    source_key = given[0]
    value = data[source_key]
    if source_key == "sql":
        if not (isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))):
            raise PipelineError("'sql' is the SQL text, or a list of statements")
    elif not isinstance(value, str):
        raise PipelineError(f"'{source_key}' is a file path")

    engine = data.get("engine", "duckdb")
    if engine not in ENGINES:
        raise PipelineError(f"'engine' is one of {list(ENGINES)} (got {engine!r})")

    spec = PipelineSpec(name=name, source=source_key, source_value=value, base_dir=base_dir, engine=engine,
                        description=str(data.get("description") or ""), path=path)
    if data.get("primary_key") is not None:
        spec.primary_key = _names(data["primary_key"], "'primary_key'")
    keys = data.get("keys") or {}
    if not isinstance(keys, dict):
        raise PipelineError("'keys' maps a view to its key column(s): {\"enriched\": [\"id\"]}")
    spec.keys = {str(v).lower(): _names(k, f"keys.{v}") for v, k in keys.items()}
    spec.sip = _sip(data.get("sip"), spec.primary_key)
    spec.catalogs = _catalogs(data.get("catalogs"))
    if data.get("aws") is not None:
        from .aws import aws_problems

        problems = aws_problems(data["aws"])
        if problems:
            raise PipelineError("; ".join(problems))
        spec.aws = dict(data["aws"])
    if data.get("state") is not None:
        if not isinstance(data["state"], str) or not data["state"].strip():
            raise PipelineError("'state' is a folder in the lake (s3://lake/_state/) or a local folder")
        spec.state = data["state"].strip()
    spec.load = _load(data.get("load"))
    from .lakeread import read_engine_problem

    problem = read_engine_problem(data.get("read_engine"), "'read_engine'")
    if problem:
        raise PipelineError(problem)
    spec.read_engine = data.get("read_engine")
    virtualized = data.get("virtualized_table", False)
    if isinstance(virtualized, str):
        virtualized = [virtualized]
    if not (isinstance(virtualized, bool) or (isinstance(virtualized, list)
                                               and all(isinstance(v, str) and v for v in virtualized))):
        raise PipelineError("'virtualized_table' is true (any table that isn't the lake's) or the list of "
                            "the ones it reads: [\"axonius_devices\"]")
    spec.virtualized = virtualized
    page_size = data.get("page_size")
    if page_size is not None:
        def _size(v):
            return isinstance(v, int) and not isinstance(v, bool) and v > 0
        if not (_size(page_size) or (isinstance(page_size, dict) and page_size
                                     and all(isinstance(k, str) and _size(v) for k, v in page_size.items()))):
            raise PipelineError("'page_size' is the rows per API request for this run: a number (every connector "
                                "it reads) or one per service: {\"servicenow\": 2000}")
        spec.page_size = page_size
    audit = data.get("audit_columns", True)
    if audit is True:
        spec.audit_columns = list(AUDIT_COLUMNS)
    elif audit is False:
        spec.audit_columns = []
    elif isinstance(audit, list) and set(audit) <= set(AUDIT_COLUMNS):
        spec.audit_columns = [c for c in AUDIT_COLUMNS if c in audit]
    else:
        raise PipelineError(f"'audit_columns' is true (default), false, or some of {list(AUDIT_COLUMNS)}")
    if data.get("timezone") is not None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(str(data["timezone"]))
        except (ZoneInfoNotFoundError, ValueError):
            raise PipelineError(f"'timezone' {data['timezone']!r} isn't a timezone name — use one like "
                                "\"America/Sao_Paulo\", \"Europe/Lisbon\" or \"UTC\"") from None
        spec.timezone = str(data["timezone"])
    spec.targets, spec.output = _targets(data)
    layer = data.get("layer")
    if layer is not None:
        if not isinstance(layer, str) or not layer:
            raise PipelineError("'layer' is the name of a layer of duckduck.json's \"lake\" (raw, silver…)")
        for t in spec.targets:
            if t.layer is None and t.database:
                t.layer = layer
                if t.path:
                    raise PipelineError("with a 'layer', the path is always {the layer's prefix}/{database}/"
                                        "{table_name} — leave 'path' out")
    evolution = data.get("schema_evolution", True)
    if not isinstance(evolution, bool):
        raise PipelineError("'schema_evolution' is true (default: new columns join the table) or false")
    for t in spec.targets:
        if t.schema is None:
            t.schema = "evolve" if evolution else "fixed"
    default_catalog = data.get("catalog")
    if default_catalog is not None and not isinstance(default_catalog, str):
        raise PipelineError("'catalog' is the name of the catalog the targets' tables are in")
    for t in spec.targets:
        if t.catalog is None and default_catalog and t.table:
            t.catalog = default_catalog
        if t.catalog and not t.table:
            raise PipelineError(f"the target of {t.view or 'the output'} names catalog {t.catalog}: give it the "
                                "'table' (database.table) too")
        if t.catalog and len(t.table.split(".")) not in (2, 3):
            raise PipelineError(f"table {t.table!r}: write it as database.table (or catalog.schema.table in Unity)")
    for t in spec.targets:  # a local folder is relative to the pipeline file, like the SQL's
        if t.path and "://" not in t.path:
            t.path = spec.resolve(t.path)
    params = data.get("parameters") or {}
    if not isinstance(params, dict):
        raise PipelineError("'parameters' is an object: {\"watermark\": \"2026-09-01\"}")
    for k, v in params.items():
        if not NAME_RE.match(str(k)) or k in BUILTIN_PARAMETERS:
            raise PipelineError(f"parameter {k!r}: a plain name, not one of the built-in {list(BUILTIN_PARAMETERS)}")
        if isinstance(v, (dict, list)):
            raise PipelineError(f"parameter {k!r} is text, a number or a boolean")
    spec.parameters = dict(params)
    if spec.sip.enabled and not spec.primary_key and not spec.keys:
        raise PipelineError("the sip follows rows by their key: add 'primary_key'")
    for t in spec.targets:
        if t.mode == "merge" and not (t.key or spec.primary_key or spec.keys.get(t.view)):
            raise PipelineError(f"merge into {t.where} needs a key: 'primary_key', or 'key' on the target")
    return spec


LOAD_KEYS = {"type", "columns", "initial", "lookback", "step"}
#: written into every row of every target: when it was loaded (UTC), the run's date (its timezone), which run
DEFAULT_LOAD_COLUMN = "_loaded_at"  # an incremental load with no columns reads on it
AUDIT_COLUMNS = ("_loaded_at", "_load_date", "_run_id")
DURATION_RE = re.compile(r"^\s*(\d+)\s*([dhmw])\s*$")


def _load(raw: Any) -> Load:
    if raw is None or raw == "full":
        return Load()
    if raw == "incremental":
        raw = {"type": "incremental"}
    if not isinstance(raw, dict):
        raise PipelineError("'load' is \"full\", or {\"type\": \"incremental\", \"columns\": [\"updated_at\"]}")
    unknown = sorted(set(raw) - LOAD_KEYS)
    if unknown:
        raise PipelineError(f"unknown load key(s) {unknown} — it takes {sorted(LOAD_KEYS)}")
    kind = raw.get("type", "incremental" if raw.get("columns") else "full")
    if kind == "incremental" and not raw.get("columns"):
        raw = {**raw, "columns": [DEFAULT_LOAD_COLUMN]}  # what the layer below stamped on every row
    if kind not in ("full", "incremental"):
        raise PipelineError(f"load.type is full or incremental (got {kind!r})")
    load = Load(type=kind)
    if kind == "full":
        if raw.get("columns"):
            raise PipelineError("a full load reads everything: 'columns' goes with \"type\": \"incremental\"")
        return load
    load.columns = _names(raw["columns"], "load.columns")
    initial = raw.get("initial")
    if isinstance(initial, dict):
        load.initial = {str(k): v for k, v in initial.items()}
        stray = sorted(set(load.initial) - set(load.columns))
        if stray:
            raise PipelineError(f"load.initial names {stray}, which aren't in load.columns")
    elif initial is not None:
        load.initial = {c: initial for c in load.columns}
    if raw.get("lookback") is not None:
        if not isinstance(raw["lookback"], str) or not DURATION_RE.match(raw["lookback"]):
            raise PipelineError("load.lookback is a duration: \"10m\", \"2h\", \"1d\", \"1w\"")
        load.lookback = raw["lookback"].strip()
    if raw.get("step") is not None:
        load.step = str(raw["step"]).lower()
    return load


def _listed(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _key_text(v: Any) -> str:
    """A key part as the engines write it in text (CAST(x AS VARCHAR)): true/false lowercase."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (str, int, float)):
        return str(v)
    raise PipelineError(f"sip.watch: {v!r} isn't a key value (text or a number)")


def watch_key(w: Any, primary_key: Optional[List[str]]) -> str:
    """A watched key as the sip compares it: ``"A-1"``; a composite one as a list in the key's order
    (``["A-1", 3]``) or an object by column (``{"order_id": "A-1", "line": 3}``) — both become ``A-1|3``."""
    if isinstance(w, list):
        if len(w) < 2:
            raise PipelineError("sip.watch: a list is a composite key — its parts in the key's order")
        return "|".join(_key_text(x) for x in w)
    if isinstance(w, dict):
        if not primary_key or {k.lower() for k in w} != {k.lower() for k in primary_key}:
            raise PipelineError(f"sip.watch: {w} names columns; they must be the primary_key's "
                                f"({', '.join(primary_key or []) or 'none set'})")
        by = {k.lower(): v for k, v in w.items()}
        return "|".join(_key_text(by[k.lower()]) for k in primary_key)
    return _key_text(w)


def _sip(raw: Any, primary_key: Optional[List[str]] = None) -> SipSpec:
    if raw in (None, False):
        return SipSpec(enabled=False)
    if raw is True:
        return SipSpec(enabled=True)
    if not isinstance(raw, dict):
        raise PipelineError("'sip' is true/false or an object: {\"rate\": 0.001, \"store\": \"s3://lake/_sip/\"}")
    unknown = sorted(set(raw) - SIP_KEYS)
    if unknown:
        raise PipelineError(f"unknown sip key(s) {unknown} — it takes {sorted(SIP_KEYS)}")
    sip = SipSpec(enabled=bool(raw.get("enabled", True)))
    if "rate" in raw:
        rate = raw["rate"]
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0 < rate <= 1:
            raise PipelineError("sip.rate is the share of keys followed, above 0 and at most 1 (0.001 = 0.1%)")
        if rate * 16 ** 8 < 1:
            raise PipelineError("sip.rate is too small to pick any key (minimum 0.0000001)")
        sip.rate = float(rate)
    if "max_rows" in raw:
        if not isinstance(raw["max_rows"], int) or isinstance(raw["max_rows"], bool) or raw["max_rows"] < 1:
            raise PipelineError("sip.max_rows is a positive whole number")
        sip.max_rows = raw["max_rows"]
    sip.watch = [watch_key(w, primary_key) for w in _listed(raw.get("watch"))]
    for k in ("columns", "mask"):
        v = _listed(raw.get(k))
        if not all(isinstance(x, str) for x in v):
            raise PipelineError(f"sip.{k} is a list of column names")
        setattr(sip, k, [str(x) for x in v])
    if raw.get("store") is not None:
        if not isinstance(raw["store"], str) or not raw["store"].strip():
            raise PipelineError("sip.store is a folder in the lake (s3://lake/_sip/) or a local folder")
        sip.store = raw["store"].strip()
    stages = raw.get("stages", "all")
    if stages not in ("all", "output"):
        raise PipelineError("sip.stages is \"all\" (every view on the way to a target) or \"output\" (the written ones)")
    sip.stages = stages
    sip.null_keys = bool(raw.get("null_keys", True))
    return sip


def _target(view: str, raw: Any) -> Target:
    if isinstance(raw, str):
        raw = {"path": raw} if is_path(raw) else {"table": raw}
    if not isinstance(raw, dict):
        raise PipelineError(f"the target of {view} is a path, a table name, or an object with path/table + mode")
    unknown = sorted(set(raw) - TARGET_KEYS)
    if unknown:
        raise PipelineError(f"unknown target key(s) {unknown} — a target takes {sorted(TARGET_KEYS)}")
    path, table = raw.get("path"), raw.get("table")
    database, table_name = raw.get("database"), raw.get("table_name")
    if (database is None) != (table_name is None):
        raise PipelineError(f"the target of {view or 'the output'}: give both 'database' and 'table_name'")
    if database is not None:
        for what, value in (("database", database), ("table_name", table_name)):
            if not isinstance(value, str) or not NAME_RE.match(value):
                raise PipelineError(f"the target's {what} {value!r}: letters, digits and _")
        if table:
            raise PipelineError("give the target 'database' + 'table_name', or 'table', not both")
        table = f"{database}.{table_name}"
    if raw.get("layer") is not None:
        if database is None:
            raise PipelineError(f"the target of {view or 'the output'} is in layer {raw['layer']!r}: give its "
                                "'database' and 'table_name' (the path is {layer}/{database}/{table_name})")
        if path:
            raise PipelineError("with a 'layer', the path is always {the layer's prefix}/{database}/{table_name} — "
                                "leave 'path' out")
    if raw.get("catalog") and table:
        pass  # in a catalog: the table, and optionally where a new one goes (path)
    elif bool(path) == bool(table):
        raise PipelineError(f"the target of {view} has a 'path' (a folder in the lake) or a 'table' (a catalog "
                            "table), not both")
    t = Target(view=view, path=path, table=table, layer=raw.get("layer"), database=database,
               table_name=table_name)
    t.mode = raw.get("mode", "append")
    if t.mode not in MODES:
        raise PipelineError(f"target mode is one of {list(MODES)} (got {t.mode!r})")
    t.format = raw.get("format") or ("parquet" if path else None)
    if t.format is not None and t.format not in FORMATS:
        raise PipelineError(f"target format is one of {list(FORMATS)} (got {t.format!r})")
    t.catalog = raw.get("catalog")
    if t.catalog is not None and (not isinstance(t.catalog, str) or not t.catalog):
        raise PipelineError("a target's 'catalog' is the name of a catalog")
    t.schema = raw.get("schema")
    if t.schema is not None and t.schema not in SCHEMA_POLICIES:
        raise PipelineError(f"target 'schema' is one of {list(SCHEMA_POLICIES)} (got {t.schema!r})")
    if "schema_evolution" in raw:
        on = raw["schema_evolution"]
        if not isinstance(on, bool):
            raise PipelineError("'schema_evolution' is true or false")
        if t.schema is not None and (t.schema in ("evolve", "overwrite")) != on:
            raise PipelineError(f"the target of {view or 'the output'} says \"schema_evolution\": "
                                f"{str(on).lower()} and \"schema\": \"{t.schema}\" — keep one")
        t.schema = t.schema or ("evolve" if on else "fixed")
    if t.schema == "overwrite" and t.mode != "overwrite":
        raise PipelineError("\"schema\": \"overwrite\" replaces a table's columns: only with \"mode\": \"overwrite\"")
    if raw.get("partition_by"):
        t.partition_by = _names(raw["partition_by"], "partition_by")
    if raw.get("key"):
        t.key = _names(raw["key"], "the target's key")
    t.unique = bool(raw.get("unique", True))
    opts = raw.get("storage_options") or {}
    if not isinstance(opts, dict):
        raise PipelineError("storage_options is an object of text values")
    t.storage_options = {str(k): str(v) for k, v in opts.items()}
    return t


def _catalogs(raw: Any) -> Dict[str, Dict[str, Any]]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        raise PipelineError("'catalogs' maps a name to a catalog: {\"lake\": {\"type\": \"glue\", \"region\": …}}")
    for name, block in raw.items():
        if block.get("type") not in ("glue", "unity", "iceberg"):
            raise PipelineError(f"catalog {name}: 'type' is glue, unity or iceberg")
    return {str(k): dict(v) for k, v in raw.items()}


def _targets(data: Dict[str, Any]):
    if data.get("target") and data.get("targets"):
        raise PipelineError("use 'target' (the pipeline's output) or 'targets' ({view: target}), not both")
    if data.get("targets"):
        if data.get("output"):
            raise PipelineError("'output' goes with 'target'; with 'targets' each view is named already")
        if not isinstance(data["targets"], dict):
            raise PipelineError("'targets' maps a view to where it's written: {\"silver\": {\"table\": ..., "
                                "\"mode\": \"merge\"}}")
        return [_target(str(v).lower(), raw) for v, raw in data["targets"].items()], None
    output = data.get("output")
    if output is not None and (not isinstance(output, str) or not NAME_RE.match(output)):
        raise PipelineError("'output' is the name of the view written to the target")
    out = output.lower() if output else None
    if data.get("target"):
        return [_target(out or "", data["target"])], out
    return [], out


# ---------------------------------------------------------------------------
# Parameters: {{ name }} and {{ run_date - 1d }}
# ---------------------------------------------------------------------------

PARAM_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:([+-])\s*(\d+)\s*([dhmw]))?\s*\}\}")
_UNITS = {"d": "days", "h": "hours", "m": "minutes", "w": "weeks"}


def _shifted(value: Any, sign: str, amount: str, unit: str, name: str) -> str:
    text = str(value)
    delta = dt.timedelta(**{_UNITS[unit]: int(amount) * (-1 if sign == "-" else 1)})
    try:
        if len(text) == 10:
            day = dt.date.fromisoformat(text)
            if unit in ("d", "w"):
                return (day + delta).isoformat()
            moment = dt.datetime.combine(day, dt.time())
        else:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise PipelineError(f"{{{{ {name} {sign} {amount}{unit} }}}}: {name} isn't a date or a timestamp "
                            f"({text!r})") from None
    return (moment + delta).isoformat(sep=" ")


def substitute(text: str, values: Dict[str, Any]) -> str:
    """``{{ name }}`` → its value as written; ``{{ name - 1d }}`` shifts a date/timestamp (d, h, m, w)."""
    def one(m: re.Match) -> str:
        name = m.group(1)
        if name not in values:
            raise PipelineError(f"unknown parameter {{{{ {name} }}}} — known: {sorted(values)}")
        if m.group(2):
            return _shifted(values[name], m.group(2), m.group(3), m.group(4), name)
        v = values[name]
        return ("true" if v else "false") if isinstance(v, bool) else str(v)
    return PARAM_RE.sub(one, text)


def _first_watermark(spec: PipelineSpec, state: Dict[str, Any]) -> str:
    marks = state.get("watermarks") or {}
    if spec.load.columns:
        col = spec.load.columns[0]
        return str(marks.get(col) or spec.load.initial.get(col) or EPOCH)
    return str(next(iter(marks.values()), None) or EPOCH)


def run_parameters(spec: PipelineSpec, overrides: Optional[Dict[str, Any]] = None,
                   now: Optional[dt.datetime] = None, run_id: Optional[str] = None,
                   state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The values ``{{ … }}`` reads: built-ins, then the file's parameters (which may use the built-ins), then
    the overrides given for this run (CLI ``--param``, ``run_pipeline(params=)``)."""
    from zoneinfo import ZoneInfo

    overrides = dict(overrides or {})
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(spec.timezone))  # the job's clock
    values: Dict[str, Any] = {
        "run_at": now.replace(microsecond=0).isoformat(sep=" "),
        "run_date": str(overrides.pop("run_date", None) or now.date().isoformat()),
        "run_id": run_id or now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "pipeline": spec.name,
        # from the last successful run (state): where it stopped reading (the first load column's), when it ran
        "watermark": _first_watermark(spec, state or {}),
        "last_success_at": (state or {}).get("last_success_at") or EPOCH,
        "last_run_id": (state or {}).get("last_run_id") or "",
    }
    unknown = sorted(set(overrides) - set(spec.parameters) - set(BUILTIN_PARAMETERS))
    if unknown:
        raise PipelineError(f"parameter(s) {unknown} aren't in the pipeline's 'parameters' "
                            f"({sorted(spec.parameters) or 'none'})")
    for k, v in spec.parameters.items():
        values[k] = substitute(v, values) if isinstance(v, str) else v
    values.update(overrides)
    return values

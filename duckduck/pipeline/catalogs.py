"""
The catalog, kept apart from the storage.

The **storage** is files: Parquet (hive ``k=v`` folders), a Delta log, an
Iceberg table's metadata — written by whichever engine runs the pipeline
(DuckDB through pyarrow / deltalake / pyiceberg, Spark through ``df.write``).
The **catalog** says a table exists, where its files are, its columns and
partitions: AWS Glue, Unity Catalog (Azure Databricks or the open-source
server), or any Iceberg catalog pyiceberg speaks (REST — Polaris, Lakekeeper,
Unity's Iceberg endpoint —, Glue, Hive, SQL). The pipeline writes the files
first, then tells the catalog — through the catalog's own API, never through
the engine's — so the same pipeline file keeps the same tables whichever
engine ran it.

    "catalogs": {"lake": {"type": "glue", "region": "us-east-1", "warehouse": "s3://lake/"}},
    "targets": {"silver": {"catalog": "lake", "table": "silver.incidents", "format": "delta", "mode": "merge"}}

**Creating**: a table the catalog doesn't have is created at ``path`` (or
``{warehouse}/{database}/{table}/``) with the view's columns and the target's
``partition_by``; its database/schema/namespace too (``create_databases``).

**Schema** (``target.schema``; ``"schema_evolution": false`` in the file or
on a target = ``fixed``): ``evolve`` (default) adds the view's new
columns to the table (Glue: ``UpdateTable``; Delta: the log's schema merge;
Iceberg: ``union_by_name``), writes a column the view lacks as NULL, and
casts a value to the table's type when that loses nothing (int → bigint,
float → double, a wider decimal, an all-NULL column to anything); any other
type change fails before a byte is written, naming the column. ``fixed``
(schema evolution off) is the same but the table never changes: a new column
fails. ``strict``
fails on any difference. ``overwrite`` (only with ``mode: overwrite``)
replaces the table's schema with the view's.

**Partitions** (Parquet in Glue): the partitions written are registered
(``BatchCreatePartition``, existing ones skipped); an ``overwrite`` also
removes the ones no longer there. A table with partition projection
(``projection.enabled``) needs none. Delta and Iceberg keep their own.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote

from .spec import PipelineError

logger = logging.getLogger("duckduck.pipeline")

CATALOG_TYPES = ("glue", "unity", "iceberg")
SCHEMA_POLICIES = ("evolve", "fixed", "strict", "overwrite")


# ---------------------------------------------------------------------------
# Columns and types (Hive / Spark type strings: bigint, decimal(10,2), array<string>, struct<a:int>)
# ---------------------------------------------------------------------------


@dataclass
class Column:
    name: str
    type: str
    comment: Optional[str] = None

    def __post_init__(self):
        self.type = normalize_type(self.type)


@dataclass
class TableInfo:
    database: str
    name: str
    location: str
    format: str  # parquet / delta / iceberg
    columns: List[Column] = field(default_factory=list)  # data columns (not the partition keys)
    partition_keys: List[Column] = field(default_factory=list)
    properties: Dict[str, str] = field(default_factory=dict)
    projected: bool = False  # Glue partition projection: no partitions to register
    raw: Any = None  # the catalog's own description

    @property
    def full_name(self) -> str:
        return f"{self.database}.{self.name}"


def normalize_type(t: str) -> str:
    t = re.sub(r"\s+", "", (t or "string").lower())
    return {"integer": "int", "long": "bigint", "short": "smallint", "byte": "tinyint", "real": "float",
            "varchar": "string", "text": "string", "bool": "boolean", "timestamp_ntz": "timestamp"}.get(t, t)


def arrow_to_hive(t: Any) -> str:
    """A pyarrow type as a Hive/Glue type."""
    import pyarrow as pa

    if pa.types.is_null(t) or pa.types.is_string(t) or pa.types.is_large_string(t) or \
            getattr(pa.types, "is_string_view", lambda _: False)(t):
        return "string"
    if pa.types.is_boolean(t):
        return "boolean"
    for check, name in ((pa.types.is_int8, "tinyint"), (pa.types.is_int16, "smallint"), (pa.types.is_int32, "int"),
                        (pa.types.is_int64, "bigint"), (pa.types.is_uint8, "smallint"),
                        (pa.types.is_uint16, "int"), (pa.types.is_uint32, "bigint"),
                        (pa.types.is_float16, "float"), (pa.types.is_float32, "float"),
                        (pa.types.is_float64, "double"), (pa.types.is_date, "date"),
                        (pa.types.is_timestamp, "timestamp")):
        if check(t):
            return name
    if pa.types.is_uint64(t):
        return "decimal(20,0)"
    if pa.types.is_decimal(t):
        return f"decimal({t.precision},{t.scale})"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_fixed_size_binary(t):
        return "binary"
    if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_fixed_size_list(t):
        return f"array<{arrow_to_hive(t.value_type)}>"
    if pa.types.is_struct(t):
        return "struct<" + ",".join(f"{t.field(i).name}:{arrow_to_hive(t.field(i).type)}"
                                    for i in range(t.num_fields)) + ">"
    if pa.types.is_map(t):
        return f"map<{arrow_to_hive(t.key_type)},{arrow_to_hive(t.item_type)}>"
    return "string"  # time, interval, uuid…: as text


_INTS = ["tinyint", "smallint", "int", "bigint"]
_DECIMAL = re.compile(r"^decimal\((\d+),(\d+)\)$")


def fits(new: str, table: str) -> bool:
    """Values of type ``new`` stored as ``table`` lose nothing."""
    new, table = normalize_type(new), normalize_type(table)
    if new == table:
        return True
    if new in _INTS and table in _INTS:
        return _INTS.index(new) <= _INTS.index(table)
    if new == "float" and table == "double":
        return True
    if new in ("tinyint", "smallint", "int") and table == "double":
        return True
    d_new, d_table = _DECIMAL.match(new), _DECIMAL.match(table)
    if d_table:
        p, s = int(d_table.group(1)), int(d_table.group(2))
        if d_new:
            p0, s0 = int(d_new.group(1)), int(d_new.group(2))
            return s >= s0 and p - s >= p0 - s0
        digits = {"tinyint": 3, "smallint": 5, "int": 10, "bigint": 19}.get(new)
        return digits is not None and p - s >= digits
    return False


@dataclass
class SchemaPlan:
    casts: Dict[str, str] = field(default_factory=dict)  # view column → the table's type to cast it to
    added: List[Column] = field(default_factory=list)  # new columns the table gets
    missing: List[Column] = field(default_factory=list)  # table columns the view lacks: written as NULL
    replace: bool = False  # schema overwrite

    @property
    def changes(self) -> bool:
        return bool(self.added or self.replace)


def plan_schema(table: Optional[TableInfo], view: Sequence[Column], policy: str, mode: str,
                partition_by: Sequence[str]) -> SchemaPlan:
    """How the view's columns go into the table — or a ``PipelineError`` naming every column that can't."""
    if policy not in SCHEMA_POLICIES:
        raise PipelineError(f"target 'schema' is one of {list(SCHEMA_POLICIES)} (got {policy!r})")
    if policy == "overwrite" and mode != "overwrite":
        raise PipelineError("\"schema\": \"overwrite\" replaces the table's columns: only with \"mode\": \"overwrite\"")
    plan = SchemaPlan()
    if table is None:
        return plan
    parts = {p.lower() for p in partition_by} | {k.name.lower() for k in table.partition_keys}
    data = [c for c in view if c.name.lower() not in parts]
    known = {c.name.lower(): c for c in table.columns}
    if policy == "overwrite":
        plan.replace = [(c.name.lower(), c.type) for c in data] != [(c.name.lower(), c.type) for c in table.columns]
        return plan
    problems = []
    for c in data:
        have = known.get(c.name.lower())
        if have is None:
            plan.added.append(c)
        elif c.type != have.type:
            if c.type == "string" and view_is_null(c) or fits(c.type, have.type):
                plan.casts[c.name] = have.type
            else:
                problems.append(f"{c.name} is {c.type} in the view, {have.type} in {table.full_name}")
    names = {c.name.lower() for c in data}
    plan.missing = [c for c in table.columns if c.name.lower() not in names]
    for k in table.partition_keys:
        match = next((c for c in view if c.name.lower() == k.name.lower()), None)
        if match is None:
            problems.append(f"{table.full_name} is partitioned by {k.name}, which the view doesn't have")
        elif match.type != k.type and not fits(match.type, k.type):
            problems.append(f"partition key {k.name} is {match.type} in the view, {k.type} in {table.full_name}")
        elif match.type != k.type:
            plan.casts[match.name] = k.type
    if policy == "fixed":  # schema evolution off: the table keeps its columns
        problems += [f"{c.name} ({c.type}) is new — schema evolution is off" for c in plan.added]
    if policy == "strict":
        problems += [f"{c.name} ({c.type}) is new" for c in plan.added]
        problems += [f"{c.name} ({c.type}) is in {table.full_name}, not in the view" for c in plan.missing]
        problems += [f"{col} would be cast to {t}" for col, t in plan.casts.items()]
    if problems:
        hint = {"evolve": "cast it in the view's SELECT, or replace the table's schema with \"mode\": "
                          "\"overwrite\", \"schema\": \"overwrite\"",
                "fixed": "drop the new columns from the view's SELECT, or turn schema evolution on "
                         "(\"schema_evolution\": true)",
                "strict": "\"schema\": \"strict\" takes the table's columns exactly — or use \"evolve\""}[policy]
        raise PipelineError(f"{table.full_name}: the view doesn't fit the table —\n  " + "\n  ".join(problems)
                            + f"\n{hint}")
    return plan


def view_is_null(c: Column) -> bool:
    return getattr(c, "_null", False)


def null_column(name: str) -> Column:
    c = Column(name, "string")
    c._null = True  # type: ignore[attr-defined]
    return c


def hive_escape(value: Any) -> str:
    """A partition value as Hive/Spark write it in a folder name."""
    text = "__HIVE_DEFAULT_PARTITION__" if value is None else str(value)
    out = []
    for ch in text:
        if ord(ch) < 0x20 or ch in '"#%\'*/:=?\\\x7f{[]^':
            out.append("%{:02X}".format(ord(ch)))
        else:
            out.append(ch)
    return "".join(out)


def partitions_from_files(base: str, files: Iterable[str], keys: Sequence[str]) -> List[Tuple[Dict[str, str], str]]:
    """The partitions files were written into: (values, folder relative to ``base``), from ``k=v`` folders."""
    base = base.rstrip("/") + "/"
    seen: Dict[str, Dict[str, str]] = {}
    for f in files:
        rel = f[len(base):] if f.startswith(base) else f
        parts = rel.split("/")[:-1]
        values = {}
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                values[k.lower()] = unquote(v)
        if all(k.lower() in values for k in keys) and parts:
            seen["/".join(parts)] = {k: values[k.lower()] for k in keys}
    return [(v, folder) for folder, v in seen.items()]


# ---------------------------------------------------------------------------
# AWS Glue
# ---------------------------------------------------------------------------

_PARQUET_SD = {
    "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
    "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
    "SerdeInfo": {"SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
                  "Parameters": {"serialization.format": "1"}},
}
_TABLE_INPUT_KEYS = ("Name", "Description", "Owner", "LastAccessTime", "LastAnalyzedTime", "Retention",
                     "StorageDescriptor", "PartitionKeys", "ViewOriginalText", "ViewExpandedText", "TableType",
                     "Parameters", "TargetTable")


class GlueCatalog:
    """AWS Glue Data Catalog through boto3: tables (Parquet, Delta, Iceberg via pyiceberg), columns, partitions."""

    kind = "glue"

    def __init__(self, name: str = "glue", region: Optional[str] = None, profile: Optional[str] = None,
                 catalog_id: Optional[str] = None, warehouse: Optional[str] = None, create_databases: bool = True,
                 credentials: Optional[Dict[str, Any]] = None, client: Any = None, **iceberg: Any):
        self.name = name
        self.region = region
        self.profile = profile
        self.catalog_id = catalog_id
        self.warehouse = warehouse
        self.create_databases = create_databases
        self.credentials = credentials or {}
        self._client = client
        self.iceberg_properties = iceberg

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise PipelineError("a Glue catalog needs boto3: pip install \"duckduck[aws]\"") from None
            session = boto3.Session(profile_name=self.profile, region_name=self.region,
                                    aws_access_key_id=self.credentials.get("aws_access_key_id"),
                                    aws_secret_access_key=self.credentials.get("aws_secret_access_key"),
                                    aws_session_token=self.credentials.get("aws_session_token"))
            self._client = session.client("glue")
        return self._client

    def _ids(self) -> Dict[str, str]:
        return {"CatalogId": self.catalog_id} if self.catalog_id else {}

    def default_location(self, database: str, name: str) -> str:
        if self.warehouse:
            return f"{self.warehouse.rstrip('/')}/{database}/{name}"
        try:
            db = self.client.get_database(Name=database, **self._ids())["Database"]
            if db.get("LocationUri"):
                return f"{db['LocationUri'].rstrip('/')}/{name}"
        except Exception:  # noqa: BLE001 — no database yet, no location: the error below says what to set
            pass
        raise PipelineError(f"where should {database}.{name} live? Give the target a 'path', or the catalog a "
                            "'warehouse' (s3://lake/)")

    def table(self, database: str, name: str) -> Optional[TableInfo]:
        try:
            t = self.client.get_table(DatabaseName=database, Name=name, **self._ids())["Table"]
        except Exception as exc:  # noqa: BLE001
            if "EntityNotFound" in type(exc).__name__ or "EntityNotFound" in str(exc):
                return None
            raise
        params = t.get("Parameters") or {}
        sd = t.get("StorageDescriptor") or {}
        kind = (params.get("table_type") or params.get("spark.sql.sources.provider") or "").upper()
        fmt = "iceberg" if kind == "ICEBERG" or "metadata_location" in params else \
            "delta" if kind == "DELTA" else "parquet"
        return TableInfo(database=database, name=name, location=sd.get("Location") or "", format=fmt,
                         columns=[Column(c["Name"], c.get("Type", "string"), c.get("Comment"))
                                  for c in sd.get("Columns") or []],
                         partition_keys=[Column(c["Name"], c.get("Type", "string")) for c in t.get("PartitionKeys") or []],
                         properties=dict(params), projected=str(params.get("projection.enabled", "")).lower() == "true",
                         raw=t)

    def _ensure_database(self, database: str) -> None:
        try:
            self.client.get_database(Name=database, **self._ids())
        except Exception as exc:  # noqa: BLE001
            if "EntityNotFound" not in type(exc).__name__ and "EntityNotFound" not in str(exc):
                raise
            if not self.create_databases:
                raise PipelineError(f"Glue has no database {database} (and the catalog has create_databases off)") \
                    from None
            self.client.create_database(DatabaseInput={"Name": database}, **self._ids())
            logger.info("  Glue: created database %s", database)

    @staticmethod
    def _glue_columns(columns: Sequence[Column]) -> List[Dict[str, str]]:
        return [{"Name": c.name, "Type": c.type, **({"Comment": c.comment} if c.comment else {})} for c in columns]

    def create(self, info: TableInfo) -> None:
        self._ensure_database(info.database)
        sd: Dict[str, Any] = {"Columns": self._glue_columns(info.columns), "Location": info.location}
        params = {"EXTERNAL": "TRUE", **info.properties}
        if info.format == "parquet":
            sd.update(json.loads(json.dumps(_PARQUET_SD)))
            params.setdefault("classification", "parquet")
        elif info.format == "delta":  # what Athena (table_type) and Spark (provider + path) both read
            params.update({"table_type": "DELTA", "spark.sql.sources.provider": "delta", "classification": "delta"})
            sd["SerdeInfo"] = {"Parameters": {"path": info.location}}
        table_input = {"Name": info.name, "TableType": "EXTERNAL_TABLE", "Parameters": params, "StorageDescriptor": sd,
                       "PartitionKeys": self._glue_columns(info.partition_keys)}
        self.client.create_table(DatabaseName=info.database, TableInput=table_input, **self._ids())
        logger.info("  Glue: created %s (%s at %s)", info.full_name, info.format, info.location)

    def set_columns(self, info: TableInfo, columns: Sequence[Column]) -> None:
        t = self.client.get_table(DatabaseName=info.database, Name=info.name, **self._ids())["Table"]
        table_input = {k: t[k] for k in _TABLE_INPUT_KEYS if k in t}
        sd = dict(table_input.get("StorageDescriptor") or {})
        sd["Columns"] = self._glue_columns(columns)
        table_input["StorageDescriptor"] = sd
        self.client.update_table(DatabaseName=info.database, TableInput=table_input, **self._ids())
        logger.info("  Glue: %s now has %d column(s)", info.full_name, len(columns))

    def _partition_input(self, info: TableInfo, values: Dict[str, str], folder: str) -> Dict[str, Any]:
        sd = dict((info.raw or {}).get("StorageDescriptor") or {})
        sd.pop("Columns", None)
        sd = {**json.loads(json.dumps(_PARQUET_SD)), **{k: v for k, v in sd.items() if k != "Location"},
              "Columns": self._glue_columns(info.columns), "Location": f"{info.location.rstrip('/')}/{folder}"}
        return {"Values": [values[k.name] for k in info.partition_keys], "StorageDescriptor": sd}

    def existing_partitions(self, info: TableInfo) -> List[List[str]]:
        out = []
        for page in self.client.get_paginator("get_partitions").paginate(
                DatabaseName=info.database, TableName=info.name, ExcludeColumnSchema=True, **self._ids()):
            out += [p["Values"] for p in page.get("Partitions", [])]
        return out

    def sync_partitions(self, info: TableInfo, written: List[Tuple[Dict[str, str], str]], replace: bool) -> Dict[str, int]:
        """Registers the partitions written (existing ones skipped); with ``replace``, drops the ones not written."""
        if info.format != "parquet" or not info.partition_keys or info.projected:
            return {}
        have = {tuple(v) for v in self.existing_partitions(info)}
        new = [(v, f) for v, f in written if tuple(v[k.name] for k in info.partition_keys) not in have]
        added = 0
        for i in range(0, len(new), 100):
            chunk = new[i:i + 100]
            resp = self.client.batch_create_partition(
                DatabaseName=info.database, TableName=info.name,
                PartitionInputList=[self._partition_input(info, v, f) for v, f in chunk], **self._ids())
            errors = [e for e in resp.get("Errors", []) if "AlreadyExists" not in str(e.get("ErrorDetail"))]
            if errors:
                raise PipelineError(f"Glue refused partitions of {info.full_name}: {errors[0]}")
            added += len(chunk) - len(resp.get("Errors", []))
        removed = 0
        if replace:
            keep = {tuple(v[k.name] for k in info.partition_keys) for v, _ in written}
            gone = [list(v) for v in have if v not in keep]
            for i in range(0, len(gone), 25):
                self.client.batch_delete_partition(DatabaseName=info.database, TableName=info.name,
                                                   PartitionsToDelete=[{"Values": v} for v in gone[i:i + 25]],
                                                   **self._ids())
                removed += len(gone[i:i + 25])
        if added or removed:
            logger.info("  Glue: %s partitions +%d -%d", info.full_name, added, removed)
        return {"partitions_added": added, "partitions_removed": removed}

    def iceberg(self):
        try:
            from pyiceberg.catalog.glue import GlueCatalog as IcebergGlue
        except ImportError:
            raise PipelineError("Iceberg tables need pyiceberg: pip install \"pyiceberg[glue,pyarrow]\"") from None
        props = dict(self.iceberg_properties)
        if self.region:
            props.setdefault("glue.region", self.region)
        if self.profile:
            props.setdefault("glue.profile-name", self.profile)
        if self.catalog_id:
            props.setdefault("glue.id", self.catalog_id)
        if self.warehouse:
            props.setdefault("warehouse", self.warehouse)
        return IcebergGlue(self.name, client=self._client, **props)


# ---------------------------------------------------------------------------
# Unity Catalog (Azure Databricks, AWS/GCP Databricks, the open-source server)
# ---------------------------------------------------------------------------

_UC_TYPE_NAMES = {"tinyint": "BYTE", "smallint": "SHORT", "int": "INT", "bigint": "LONG", "float": "FLOAT",
                  "double": "DOUBLE", "boolean": "BOOLEAN", "string": "STRING", "date": "DATE",
                  "timestamp": "TIMESTAMP", "binary": "BINARY"}
_SPARK_JSON = {"tinyint": "byte", "smallint": "short", "int": "integer", "bigint": "long"}
AZURE_DATABRICKS_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"  # Azure Databricks' Entra ID application


def _uc_type_name(t: str) -> str:
    t = normalize_type(t)
    if t.startswith("decimal"):
        return "DECIMAL"
    for prefix, name in (("array<", "ARRAY"), ("struct<", "STRUCT"), ("map<", "MAP")):
        if t.startswith(prefix):
            return name
    return _UC_TYPE_NAMES.get(t, "STRING")


def _spark_json(t: str) -> Any:
    """A Hive type string as Spark's JSON data type (what Unity's ``type_json`` holds)."""
    t = normalize_type(t)

    def split(inner: str) -> List[str]:
        parts, depth, buf = [], 0, ""
        for ch in inner:
            if ch == "<" or ch == "(":
                depth += 1
            elif ch == ">" or ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(buf)
                buf = ""
            else:
                buf += ch
        return parts + [buf] if buf else parts

    if t.startswith("array<"):
        return {"type": "array", "elementType": _spark_json(t[6:-1]), "containsNull": True}
    if t.startswith("map<"):
        k, v = split(t[4:-1])
        return {"type": "map", "keyType": _spark_json(k), "valueType": _spark_json(v), "valueContainsNull": True}
    if t.startswith("struct<"):
        fields = []
        for f in split(t[7:-1]):
            n, ft = f.split(":", 1)
            fields.append({"name": n, "type": _spark_json(ft), "nullable": True, "metadata": {}})
        return {"type": "struct", "fields": fields}
    return _SPARK_JSON.get(t, t)


class UnityCatalog:
    """
    Unity Catalog's REST API (``/api/2.1/unity-catalog``): external tables in
    Delta / Parquet / Iceberg format at a storage location (``abfss://…`` on
    Azure). Unity has no call to change a table's columns: a Delta table's
    columns come from its log (written with the schema merge), and new columns
    on a Parquet table need ``recreate_on_schema_change`` (drop + create of the
    external table — the files stay; grants on it don't). It doesn't track
    Parquet partitions either: prefer Delta there.
    """

    kind = "unity"

    def __init__(self, name: str = "unity", host: str = "", catalog_name: str = "main",
                 warehouse: Optional[str] = None, token: Optional[str] = None, azure_ad: bool = False,
                 tenant_id: Optional[str] = None, create_databases: bool = True,
                 recreate_on_schema_change: bool = False, session: Any = None, timeout: float = 60):
        if not host:
            raise PipelineError(f"catalog {name}: a Unity Catalog needs its 'host' (https://adb-….azuredatabricks.net)")
        self.name = name
        self.host = host.rstrip("/")
        self.base = self.host + "/api/2.1/unity-catalog"
        self.catalog_name = catalog_name
        self.warehouse = warehouse
        self._token = token
        self.azure_ad = azure_ad
        self.tenant_id = tenant_id
        self.create_databases = create_databases
        self.recreate_on_schema_change = recreate_on_schema_change
        self.timeout = timeout
        self._session = session
        self._credential = None

    @property
    def session(self):
        if self._session is None:
            import requests

            from ..logs import instrument_session

            self._session = requests.Session()
            instrument_session(self._session, "pipeline")  # Authorization is never logged
        return self._session

    def _headers(self) -> Dict[str, str]:
        token = self._token
        if token is None and self.azure_ad:
            if self._credential is None:
                from azure.identity import DefaultAzureCredential

                self._credential = DefaultAzureCredential(
                    **({"interactive_browser_tenant_id": self.tenant_id} if self.tenant_id else {}))
            token = self._credential.get_token(AZURE_DATABRICKS_SCOPE).token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        resp = self.session.request(method, self.base + path, json=body, headers=self._headers(), timeout=self.timeout)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise PipelineError(f"Unity Catalog {method} {path}: {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def _names(self, database: str, name: str) -> Tuple[str, str, str]:
        parts = database.split(".")
        return (parts[0], parts[1], name) if len(parts) == 2 else (self.catalog_name, database, name)

    def default_location(self, database: str, name: str) -> str:
        if not self.warehouse:
            raise PipelineError(f"where should {database}.{name} live? Give the target a 'path' "
                                "(abfss://…), or the catalog a 'warehouse'")
        return f"{self.warehouse.rstrip('/')}/{database.replace('.', '/')}/{name}"

    def table(self, database: str, name: str) -> Optional[TableInfo]:
        cat, schema, table = self._names(database, name)
        t = self._call("GET", f"/tables/{cat}.{schema}.{table}")
        if t is None:
            return None
        cols = sorted(t.get("columns") or [], key=lambda c: c.get("position", 0))
        parts = sorted((c for c in cols if c.get("partition_index") is not None), key=lambda c: c["partition_index"])
        return TableInfo(database=database, name=name, location=t.get("storage_location") or "",
                         format=str(t.get("data_source_format") or "DELTA").lower(),
                         columns=[Column(c["name"], c.get("type_text") or "string", c.get("comment")) for c in cols
                                  if c.get("partition_index") is None],
                         partition_keys=[Column(c["name"], c.get("type_text") or "string") for c in parts],
                         properties=dict(t.get("properties") or {}), raw=t)

    def _columns(self, info: TableInfo) -> List[Dict[str, Any]]:
        out = []
        everything = list(info.columns) + list(info.partition_keys)
        for i, c in enumerate(everything):
            col: Dict[str, Any] = {"name": c.name, "type_text": c.type, "type_name": _uc_type_name(c.type),
                                   "type_json": json.dumps({"name": c.name, "type": _spark_json(c.type),
                                                            "nullable": True, "metadata": {}}),
                                   "position": i, "nullable": True}
            d = _DECIMAL.match(c.type)
            if d:
                col.update(type_precision=int(d.group(1)), type_scale=int(d.group(2)))
            if c in info.partition_keys:
                col["partition_index"] = info.partition_keys.index(c)
            if c.comment:
                col["comment"] = c.comment
            out.append(col)
        return out

    def create(self, info: TableInfo) -> None:
        cat, schema, table = self._names(info.database, info.name)
        if self._call("GET", f"/schemas/{cat}.{schema}") is None:
            if not self.create_databases:
                raise PipelineError(f"Unity Catalog has no schema {cat}.{schema} (and create_databases is off)")
            self._call("POST", "/schemas", {"name": schema, "catalog_name": cat})
            logger.info("  Unity: created schema %s.%s", cat, schema)
        self._call("POST", "/tables", {
            "name": table, "catalog_name": cat, "schema_name": schema, "table_type": "EXTERNAL",
            "data_source_format": info.format.upper(), "storage_location": info.location,
            "columns": self._columns(info), "properties": info.properties})
        logger.info("  Unity: created %s.%s.%s (%s at %s)", cat, schema, table, info.format, info.location)

    def set_columns(self, info: TableInfo, columns: Sequence[Column]) -> None:
        if info.format == "delta":
            return  # the Delta log holds the schema (the write merged it)
        if not self.recreate_on_schema_change:
            raise PipelineError(
                f"{info.full_name} gets new columns, and Unity Catalog can't change a table's columns in place: "
                "use Delta, or set \"recreate_on_schema_change\": true on the catalog (drops and recreates the "
                "external table — the files stay, the grants on it don't)")
        cat, schema, table = self._names(info.database, info.name)
        self._call("DELETE", f"/tables/{cat}.{schema}.{table}")
        self.create(TableInfo(info.database, info.name, info.location, info.format, list(columns),
                              list(info.partition_keys), info.properties))

    def sync_partitions(self, info: TableInfo, written, replace: bool) -> Dict[str, int]:
        return {}

    def iceberg(self):
        raise PipelineError("Iceberg tables in Unity Catalog: point an \"iceberg\" catalog at Unity's Iceberg REST "
                            "endpoint ({host}/api/2.1/unity-catalog/iceberg)")


# ---------------------------------------------------------------------------
# Any Iceberg catalog pyiceberg speaks (REST, Glue, Hive, SQL)
# ---------------------------------------------------------------------------


class IcebergCatalog:
    """``pyiceberg.catalog.load_catalog(name, **properties)`` — Iceberg tables only."""

    kind = "iceberg"

    def __init__(self, name: str = "iceberg", warehouse: Optional[str] = None, **properties: Any):
        self.name = name
        self.warehouse = warehouse
        self.properties = {**properties, **({"warehouse": warehouse} if warehouse else {})}
        self._catalog = None

    def iceberg(self):
        if self._catalog is None:
            try:
                from pyiceberg.catalog import load_catalog
            except ImportError:
                raise PipelineError("Iceberg tables need pyiceberg: pip install \"pyiceberg[pyarrow]\"") from None
            self._catalog = load_catalog(self.name, **self.properties)
        return self._catalog

    def table(self, database: str, name: str) -> Optional[TableInfo]:
        from pyiceberg.exceptions import NoSuchTableError

        try:
            t = self.iceberg().load_table((database, name))
        except NoSuchTableError:
            return None
        return TableInfo(database, name, t.location(), "iceberg", raw=t)

    def default_location(self, database: str, name: str) -> Optional[str]:
        return None  # the catalog's warehouse decides

    def create(self, info: TableInfo) -> None:
        raise PipelineError(f"catalog {self.name} keeps Iceberg tables only: set \"format\": \"iceberg\"")

    set_columns = create

    def sync_partitions(self, info, written, replace) -> Dict[str, int]:
        return {}


def make_catalog(name: str, raw: Dict[str, Any], duck: Any = None, base_dir: Optional[str] = None):
    """A catalog from its config block: ``{"type": "glue"|"unity"|"iceberg", …, "authentication": {…}}``."""
    if not isinstance(raw, dict) or raw.get("type") not in CATALOG_TYPES:
        raise PipelineError(f"catalog {name}: 'type' is one of {list(CATALOG_TYPES)}")
    opts = {k: v for k, v in raw.items() if k not in ("type", "authentication")}
    secret: Dict[str, Any] = {}
    if raw.get("authentication"):
        if duck is None:
            from ..core import DuckAPI

            duck = DuckAPI()
        secret = duck.resolve_credentials(raw["authentication"], name=f"catalog {name}")
    kind = raw["type"]
    try:
        if kind == "glue":
            region = opts.pop("region", None) or opts.pop("region_name", None) or secret.get("region_name")
            return GlueCatalog(name, region=region, credentials=secret, **opts)
        if kind == "unity":
            token = secret.get("token") or secret.get("api_key")
            return UnityCatalog(name, token=token, **opts)
        props = {**opts, **{k: v for k, v in secret.items() if k in ("token", "credential")}}
        return IcebergCatalog(name, **props)
    except TypeError as exc:
        raise PipelineError(f"catalog {name}: {exc}") from None


__all__ = ["Column", "GlueCatalog", "IcebergCatalog", "SchemaPlan", "TableInfo", "UnityCatalog", "arrow_to_hive",
           "fits", "hive_escape", "make_catalog", "partitions_from_files", "plan_schema", "quote"]

"""
S3 + AWS Glue Data Catalog connector for use with DuckAPI.

Reads tables registered in the Glue Data Catalog directly from S3
through DuckDB's own extensions — no data copied through Python for the
scan itself (see ``duckduck/lakehouse.py``). ``get_table`` (boto3's Glue
client) is used only to look up *where* the table's data lives and in
what format; the actual read happens natively in DuckDB.

Format auto-detection
----------------------
Glue's ``get_table`` response's ``Table.Parameters`` conventionally
records how the table was created:

- ``table_type == "ICEBERG"`` (or a ``metadata_location`` parameter
  present — the convention Spark/Athena/PyIceberg's Glue catalog
  integration uses) → ``iceberg_scan()``, using that
  ``metadata_location`` directly when Glue recorded one.
- ``table_type == "DELTA"`` (or ``spark.sql.sources.provider ==
  "delta"``) → ``delta_scan()`` against the table's S3 location.
- Anything else → ``read_parquet()`` of the table's files, found the
  way Athena finds them (``duckduck.connectors.lake.s3layout``): the partitions the
  query's WHERE selects — from Glue's ``get_partitions`` with an
  ``Expression``, or computed from the table's partition projection —
  then only those locations listed (in threads, kept ``listing_ttl``
  seconds), hidden files (``_SUCCESS``, ``.hive-staging``…) skipped and
  every other object read as Parquet, extension or not (Athena's own
  output files have none). ``list_files=False`` goes back to DuckDB's
  ``{location}/**/*.parquet`` glob.

Usage convention
-----------------
``database``/``table_name`` are both structural (they identify which
Glue table to read, like ``site_id``/``list_id`` elsewhere) — inline
only::

    SELECT * FROM glue_table(database='analytics', table_name='events')
     WHERE event_type = 'click' LIMIT 100

Column filters in ``WHERE`` aren't pushed down through Glue/boto3 (Glue
doesn't filter data, only metadata) — DuckDB applies them on the scanned
result, same as any push-down parameter a wrapper doesn't recognize. The
Parquet/Delta/Iceberg readers themselves do their own internal filter and
projection push-down during the scan where the format supports it,
independent of DuckAPI's own push-down layer.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from . import s3layout
from ...common.kinds import catalog
from .lakehouse import LakehouseConnection
from ...common.pushdown import Condition, LikePattern, require_like, sortable
from ...common.logs import get_logger
from ...common.sparkplan import SparkSource, spark_plan
from ...common.idempotency import checkpoint

logger = get_logger("glue")

_TABLE_COLUMNS = [
    "database", "table_name", "format", "table_type", "location", "partition_keys",
    "column_count", "description", "owner", "created", "updated",
]


def _like_matches(pattern: LikePattern, name: str) -> bool:
    text, name = pattern.text.lower(), name.lower()
    return {
        "contains": text in name, "startswith": name.startswith(text),
        "endswith": name.endswith(text), "equals": name == text,
    }[pattern.kind]

try:
    import boto3
except ImportError:
    boto3 = None


class GlueTable:
    """
    Reads AWS Glue Data Catalog tables straight from S3 via DuckDB.

    Parameters
    ----------
    region_name : str, optional
    profile_name : str, optional
        Named AWS profile — see ``SecretsManager`` for the same concept.
    aws_access_key_id : str, optional
    aws_secret_access_key : str, optional
        Explicit credentials. Without these, falls back to DuckDB's own
        ``credential_chain`` provider (env vars, ``~/.aws/credentials``
        including ``profile_name``, instance/task role) — the same chain
        boto3 itself would use, so Glue lookups (boto3) and S3 reads
        (DuckDB) end up authenticated the same way.
    """

    #: what a failed batched pipeline read does next (duckduck.common.idempotency)
    IDEMPOTENCY = checkpoint("DuckDB sorts the scan and filters col > value inside it — name a column that only grows")

    #: ``where`` goes into the DuckDB scan (``conditions_to_sql``), join key values included
    WHERE_OPS = frozenset({"eq", "like", "ilike", "gt", "gte", "lt", "lte", "in"})

    def __init__(
        self,
        region_name: Optional[str] = None,
        profile_name: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        list_files: bool = True,
        listing_ttl: float = 60.0,
        list_threads: int = 16,
    ):
        #: find a Parquet table's files like Athena (partitions, projection, own listing) — False: DuckDB's glob
        self.list_files = bool(list_files)
        #: how long a location's listing (and a partition lookup) is reused, in seconds (0: never)
        self.listing_ttl = float(listing_ttl)
        #: locations listed at once
        self.list_threads = max(1, int(list_threads))
        self._listings: Dict[str, Tuple[float, List[str]]] = {}
        self._partition_cache: Dict[Tuple[str, str, str], Tuple[float, List[Tuple[str, Dict[str, str]]]]] = {}
        self._s3_client: Any = None
        if boto3 is None:
            raise ImportError(
                "boto3 is required for the Glue Data Catalog connector.\n"
                "Install with:  pip install \"duckduck[aws]\""
            )

        session = boto3.Session(
            profile_name=profile_name,
            region_name=region_name,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )
        self._session = session
        self._glue = session.client("glue")
        self._lake = LakehouseConnection()
        self._table_cache: Dict[str, Dict[str, Any]] = {}

        if aws_access_key_id and aws_secret_access_key:
            region_clause = f", REGION '{region_name}'" if region_name else ""
            secret_sql = (
                "CREATE OR REPLACE SECRET duckduck_glue_s3 (TYPE s3, "
                f"KEY_ID '{aws_access_key_id}', SECRET '{aws_secret_access_key}'"
                f"{region_clause})"
            )
        else:
            profile_clause = f", PROFILE '{profile_name}'" if profile_name else ""
            region_clause = f", REGION '{region_name}'" if region_name else ""
            secret_sql = (
                "CREATE OR REPLACE SECRET duckduck_glue_s3 (TYPE s3, "
                f"PROVIDER credential_chain{profile_clause}{region_clause})"
            )
        self._lake.create_secret(secret_sql)

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "GlueTable":
        """
        Builds GlueTable from a credentials dict. Every key is optional —
        with none at all, falls back to the default AWS credential chain
        for both the Glue lookup and the S3 read.
        """
        return cls(
            region_name=overrides.pop("region_name", None) or secret.get("region_name"),
            profile_name=overrides.pop("profile_name", None) or secret.get("profile_name"),
            aws_access_key_id=(
                overrides.pop("aws_access_key_id", None) or secret.get("aws_access_key_id")
            ),
            aws_secret_access_key=(
                overrides.pop("aws_secret_access_key", None) or secret.get("aws_secret_access_key")
            ),
            **overrides,
        )

    # ------------------------------------------------------------------
    # Glue metadata lookup
    # ------------------------------------------------------------------

    def _describe(self, database: str, table_name: str) -> Dict[str, Any]:
        """Fetches (and caches) a Glue table's metadata via get_table."""
        cache_key = f"{database}.{table_name}"
        if cache_key not in self._table_cache:
            response = self._glue.get_table(DatabaseName=database, Name=table_name)
            self._table_cache[cache_key] = response["Table"]
        return self._table_cache[cache_key]

    @staticmethod
    def _detect_format(table: Dict[str, Any]) -> str:
        """``iceberg`` / ``delta`` / ``parquet`` from a Glue table's metadata."""
        params = table.get("Parameters", {}) or {}
        table_type = (params.get("table_type") or params.get("spark.sql.sources.provider") or "").upper()
        if table_type == "ICEBERG" or "metadata_location" in params:
            return "iceberg"
        if table_type == "DELTA":
            return "delta"
        return "parquet"

    def _scan_expression(self, database: str, table_name: str) -> str:
        table = self._describe(database, table_name)
        storage = table.get("StorageDescriptor", {}) or {}
        location = storage.get("Location")
        if not location:
            raise ValueError(
                f"Glue table '{database}.{table_name}' has no StorageDescriptor.Location."
            )
        params = table.get("Parameters", {}) or {}
        table_format = self._detect_format(table)

        if table_format == "iceberg":
            self._lake.ensure_extension("iceberg")
            metadata_location = params.get("metadata_location")
            if metadata_location:
                return f"iceberg_scan('{metadata_location}')"
            return f"iceberg_scan('{location}', allow_moved_paths => true)"

        if table_format == "delta":
            self._lake.ensure_extension("delta")
            return f"delta_scan('{location}')"

        path = location.rstrip("/") + "/**/*.parquet"
        return f"read_parquet('{path}', hive_partitioning=true, union_by_name=true)"

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @spark_plan("native", source="_spark_table", why="Parquet / Delta / Iceberg on S3, described by Glue")
    @sortable(exact=True)
    def table(
        self,
        database: str,
        table_name: str,
        where: Optional[List[Condition]] = None,
        order_by: Optional[List[Tuple[str, bool]]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Reads a Glue Data Catalog table's data directly from S3.

        Parameters
        ----------
        database : str
            Structural — the Glue database name.
        table_name : str
            Structural — the Glue table name.
        where : list[Condition], optional
            Filled by DuckAPI's push-down with the query's ``WHERE``
            conditions; they run inside the DuckDB scan itself (Parquet
            row-group/file pruning), so non-matching rows never reach Python.
        limit : int, optional
            Applied via DuckDB's own ``LIMIT`` on the scan.
        order_by : list of (column, descending), optional
            ``ORDER BY … NULLS LAST`` inside the DuckDB scan, with the
            ``limit``: a query's top N is read as such (``@sortable(exact=True)``).
        """
        table, planned = self._planned(database, table_name, where)
        if planned is None:
            return self._empty(table)
        expression, variables = planned
        return self._lake.scan(expression, limit=limit, where=where, variables=variables, order_by=order_by)

    def _planned(self, database: str, table_name: str, where: Optional[List[Condition]]):
        """(Glue's table, (scan expression, variables)) — None for the second when no file can match."""
        table = self._describe(database, table_name)
        if self.list_files and self._detect_format(table) == "parquet":
            return table, self._parquet_read(database, table_name, table, where)
        return table, (self._scan_expression(database, table_name), None)

    # ------------------------------------------------------------------
    # Finding a Parquet table's files — as Athena does (duckduck.connectors.lake.s3layout)
    # ------------------------------------------------------------------

    def _parquet_read(self, database: str, table_name: str, table: Dict[str, Any],
                      where: Optional[List[Condition]]) -> Optional[Tuple[str, Dict[str, Any]]]:
        """(scan expression, variables) over just the files the query needs; None: there are none."""
        location = (table.get("StorageDescriptor") or {}).get("Location")
        if not location:
            raise ValueError(f"Glue table '{database}.{table_name}' has no StorageDescriptor.Location.")
        keys = {k["Name"].lower(): k.get("Type") or "string" for k in table.get("PartitionKeys") or []}
        partitions = self._partitions(database, table_name, table, keys, where)
        if partitions is None:  # unpartitioned, or no condition on a partition key: the whole location
            files = self._list([location])[location]
            logger.info("glue %s.%s: %s file(s) under %s", database, table_name, f"{len(files):,}", location)
            if not files:
                return None
            return ("read_parquet(getvariable('duckduck_files'), hive_partitioning=true, union_by_name=true)",
                    {"duckduck_files": files})
        listed = self._list([loc for loc, _ in partitions])
        files = [f for loc, _ in partitions for f in listed[loc]]
        logger.info("glue %s.%s: %d partition(s), %s file(s)", database, table_name, len(partitions),
                    f"{len(files):,}")
        if not files:
            return None
        names = [k["Name"].lower() for k in table.get("PartitionKeys") or []]
        if s3layout.is_hive_layout(partitions, names):
            return ("read_parquet(getvariable('duckduck_files'), hive_partitioning=true, union_by_name=true)",
                    {"duckduck_files": files})
        # partitions anywhere (ALTER TABLE ADD PARTITION … LOCATION, a projection template): their values
        # come from Glue, one row per file joined to what the file read
        mapping = [{"file": f, **{k: values.get(k) for k in names}} for loc, values in partitions
                   for f in listed[loc]]
        columns = ", ".join(f'CAST(m."{k}" AS {s3layout.duck_type(keys[k])}) AS "{k}"' for k in names)
        expression = ("(SELECT r.* EXCLUDE (filename), " + columns + " FROM read_parquet(getvariable('duckduck_files'),"
                      " filename=true, hive_partitioning=false, union_by_name=true) r JOIN (SELECT unnest("
                      "getvariable('duckduck_partitions'), recursive := true)) m ON r.filename = m.file)")
        return expression, {"duckduck_files": files, "duckduck_partitions": mapping}

    def _partitions(self, database: str, table_name: str, table: Dict[str, Any], keys: Dict[str, str],
                    where: Optional[List[Condition]]) -> Optional[List[Tuple[str, Dict[str, str]]]]:
        """The partitions the conditions select, as (location, {key: value}); None: read the whole location."""
        conditions = s3layout.partition_conditions(where, keys)
        if not keys or not conditions:
            return None
        projected = s3layout.projected_partitions(table, conditions)
        if projected is not None:
            logger.info("glue %s.%s: partition projection → %d partition(s)", database, table_name, len(projected))
            return projected
        if str((table.get("Parameters") or {}).get("projection.enabled", "")).lower() == "true":
            return None  # a projection this can't compute: list the location
        expression = s3layout.glue_expression(conditions, keys)
        if expression is None:
            return None
        cache_key = (database, table_name, expression)
        hit = self._partition_cache.get(cache_key)
        if hit and time.time() - hit[0] < self.listing_ttl:
            return hit[1]
        names = [k["Name"].lower() for k in table.get("PartitionKeys") or []]
        found = []
        for page in self._glue.get_paginator("get_partitions").paginate(
                DatabaseName=database, TableName=table_name, Expression=expression, ExcludeColumnSchema=True):
            for p in page.get("Partitions", []) or []:
                loc = (p.get("StorageDescriptor") or {}).get("Location")
                if loc:
                    found.append((loc, dict(zip(names, p.get("Values") or []))))
        logger.info("glue %s.%s: get_partitions(%s) → %d partition(s)", database, table_name, expression, len(found))
        self._partition_cache[cache_key] = (time.time(), found)
        return found

    def _s3(self) -> Any:
        if self._s3_client is None:
            self._s3_client = self._session.client("s3")
        return self._s3_client

    def _list(self, locations: List[str]) -> Dict[str, List[str]]:
        """Each location's data files (``s3://…``), listed at once in threads and kept ``listing_ttl`` seconds."""
        out: Dict[str, List[str]] = {}
        todo = []
        now = time.time()
        for loc in dict.fromkeys(locations):
            hit = self._listings.get(loc)
            if hit and now - hit[0] < self.listing_ttl:
                out[loc] = hit[1]
            else:
                todo.append(loc)
        if todo:
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=min(self.list_threads, len(todo))) as pool:
                for loc, files in zip(todo, pool.map(self._list_one, todo)):
                    out[loc] = files
                    self._listings[loc] = (time.time(), files)
            logger.info("glue: listed %d location(s) in %.2fs", len(todo), time.perf_counter() - started)
        return out

    def _list_one(self, location: str) -> List[str]:
        bucket, prefix = s3layout.split_s3(location)
        files = []
        for page in self._s3().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key") or ""
                if s3layout.is_data_file(key[len(prefix):], obj.get("Size")):
                    files.append(f"s3://{bucket}/{key}")
        return files

    @staticmethod
    def _empty(table: Dict[str, Any]) -> pd.DataFrame:
        """No files: an empty frame with the table's columns (as Glue lists them), so the query still binds."""
        columns = [c.get("Name") for c in (table.get("StorageDescriptor") or {}).get("Columns") or []]
        columns += [c.get("Name") for c in table.get("PartitionKeys") or []]
        return pd.DataFrame({c: pd.Series(dtype="object") for c in columns if c})

    @spark_plan("native", source="_spark_path", why="an S3 location Spark reads directly")
    @sortable(exact=True)
    def path(
        self,
        s3_path: str,
        format: str = "parquet",
        where: Optional[List[Condition]] = None,
        order_by: Optional[List[Tuple[str, bool]]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Reads directly from an S3 path, bypassing Glue — for ad hoc reads
        of data that isn't (yet) cataloged.

        Parameters
        ----------
        s3_path : str
            Structural — e.g. ``"s3://bucket/path/"`` (a Parquet dataset)
            or an exact Delta/Iceberg table location.
        format : {"parquet", "delta", "iceberg"}
        where : list[Condition], optional
            Filled by DuckAPI's push-down with the query's ``WHERE``
            conditions; they run inside the DuckDB scan itself (Parquet
            row-group/file pruning), so non-matching rows never reach Python.
        limit : int, optional
        """
        if format == "parquet":
            path = s3_path.rstrip("/") + "/**/*.parquet"
            scan_expr = f"read_parquet('{path}', hive_partitioning=true, union_by_name=true)"
        elif format == "delta":
            self._lake.ensure_extension("delta")
            scan_expr = f"delta_scan('{s3_path}')"
        elif format == "iceberg":
            self._lake.ensure_extension("iceberg")
            scan_expr = f"iceberg_scan('{s3_path}')"
        else:
            raise ValueError(f"Unsupported format '{format}'. Use parquet, delta, or iceberg.")
        return self._lake.scan(scan_expr, limit=limit, where=where, order_by=order_by)

    # ------------------------------------------------------------------
    # Spark (``duckduck.spark``): where the data is, for a native read
    # ------------------------------------------------------------------

    def _spark_table(self, database: str, table_name: str) -> SparkSource:
        """The table's location and format; Spark reads it by name when its catalog is this Glue catalog."""
        table = self._describe(database, table_name)
        location = (table.get("StorageDescriptor", {}) or {}).get("Location")
        params = table.get("Parameters", {}) or {}
        table_format = self._detect_format(table)
        path = params.get("metadata_location") or location if table_format == "iceberg" else location
        if not path:
            raise ValueError(f"Glue table '{database}.{table_name}' has no location")
        return SparkSource(table_format, path=path, table=f"{database}.{table_name}")

    def _spark_path(self, s3_path: str, format: str = "parquet") -> SparkSource:
        return SparkSource(format, path=s3_path)

    # ------------------------------------------------------------------
    # Catalog discovery
    # ------------------------------------------------------------------

    def _paginate(self, operation: str, key: str, **kwargs) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for page in self._glue.get_paginator(operation).paginate(**kwargs):
            items.extend(page.get(key, []))
        return items

    @spark_plan("driver", why="catalog: a small listing")
    @catalog
    def databases(self, limit: Optional[int] = None) -> pd.DataFrame:
        """Lists the Glue Data Catalog's databases."""
        rows = [
            {
                "database": db.get("Name"),
                "description": db.get("Description"),
                "location": db.get("LocationUri"),
                "created": db.get("CreateTime"),
            }
            for db in self._paginate("get_databases", "DatabaseList")
        ]
        df = pd.DataFrame(rows, columns=["database", "description", "location", "created"])
        return df.head(limit) if limit is not None else df

    @spark_plan("driver", why="catalog: a small listing")
    @catalog(lists="table")
    def tables(
        self,
        database: Optional[str] = None,
        table_name_ilike: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists Glue tables with what's needed to query them: database,
        name, detected format (parquet / delta / iceberg), S3 location,
        partition keys, column count, description, owner and timestamps.

        Parameters
        ----------
        database : str, optional
            One database (``WHERE database = 'x'`` pushes down here). Omitted,
            every database in the catalog is listed.
        table_name_ilike : str, optional
            SQL LIKE pattern on the table name (``WHERE table_name LIKE
            'proxy%'`` pushes down here), case-insensitive.
        """
        names = [database] if database else [db["Name"] for db in self._paginate("get_databases", "DatabaseList")]
        pattern = require_like(table_name_ilike, "table_name_ilike") if table_name_ilike else None
        rows = []
        for db_name in names:
            for t in self._paginate("get_tables", "TableList", DatabaseName=db_name):
                name = t.get("Name", "")
                if pattern and not _like_matches(pattern, name):
                    continue
                storage = t.get("StorageDescriptor", {}) or {}
                rows.append({
                    "database": db_name,
                    "table_name": name,
                    "format": self._detect_format(t),
                    "table_type": t.get("TableType"),
                    "location": storage.get("Location"),
                    "partition_keys": ", ".join(k.get("Name", "") for k in t.get("PartitionKeys", []) or []),
                    "column_count": len(storage.get("Columns", []) or []),
                    "description": t.get("Description"),
                    "owner": t.get("Owner"),
                    "created": t.get("CreateTime"),
                    "updated": t.get("UpdateTime"),
                })
                if limit is not None and len(rows) >= limit:
                    return pd.DataFrame(rows, columns=_TABLE_COLUMNS)
        return pd.DataFrame(rows, columns=_TABLE_COLUMNS)

    @spark_plan("driver", why="catalog: a small listing")
    @catalog
    def columns(self, database: str, table_name: str) -> pd.DataFrame:
        """A Glue table's columns (partition keys included), with types and comments."""
        t = self._describe(database, table_name)
        storage = t.get("StorageDescriptor", {}) or {}
        rows = [
            {"column_name": c.get("Name"), "data_type": c.get("Type"), "comment": c.get("Comment"), "partition_key": False}
            for c in storage.get("Columns", []) or []
        ] + [
            {"column_name": c.get("Name"), "data_type": c.get("Type"), "comment": c.get("Comment"), "partition_key": True}
            for c in t.get("PartitionKeys", []) or []
        ]
        df = pd.DataFrame(rows, columns=["column_name", "data_type", "comment", "partition_key"])
        df.insert(0, "table_name", table_name)
        df.insert(0, "database", database)
        return df

    # ------------------------------------------------------------------
    # Streaming (iter_*) — for use with DuckAPI.stream()
    # ------------------------------------------------------------------

    @sortable(exact=True)
    def iter_table(
        self,
        database: str,
        table_name: str,
        where: Optional[List[Condition]] = None,
        order_by: Optional[List[Tuple[str, bool]]] = None,
        chunksize: int = 100_000,
    ):
        """
        Yields up to ``chunksize`` rows at a time, as DuckDB streams the scan
        (``LakehouseConnection.scan_batches``) — the table is never whole in
        memory. ``order_by`` sorts the whole scan (a pipeline resuming after a
        column's value).
        """
        table, planned = self._planned(database, table_name, where)
        if planned is None:
            yield self._empty(table)
            return
        expression, variables = planned
        yield from self._lake.scan_batches(expression, where=where, variables=variables, order_by=order_by,
                                           chunksize=chunksize)

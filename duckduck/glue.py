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
- Anything else → ``read_parquet()`` against
  ``{location}/**/*.parquet`` with Hive partitioning enabled — the
  common case for tables a Glue crawler registered from plain Parquet
  data (this connector's default assumption, per its purpose: reading
  Parquet whenever possible).

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

from typing import Any, Dict, List, Optional

import pandas as pd

from .kinds import catalog
from .lakehouse import LakehouseConnection
from .pushdown import Condition, LikePattern, require_like

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

    def __init__(
        self,
        region_name: Optional[str] = None,
        profile_name: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
    ):
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

    def table(
        self,
        database: str,
        table_name: str,
        where: Optional[List[Condition]] = None,
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
        """
        scan_expr = self._scan_expression(database, table_name)
        return self._lake.scan(scan_expr, limit=limit, where=where)

    def path(
        self,
        s3_path: str,
        format: str = "parquet",
        where: Optional[List[Condition]] = None,
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
        return self._lake.scan(scan_expr, limit=limit, where=where)

    # ------------------------------------------------------------------
    # Catalog discovery
    # ------------------------------------------------------------------

    def _paginate(self, operation: str, key: str, **kwargs) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for page in self._glue.get_paginator(operation).paginate(**kwargs):
            items.extend(page.get(key, []))
        return items

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

    @catalog
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

    def iter_table(
        self,
        database: str,
        table_name: str,
        where: Optional[List[Condition]] = None,
        chunksize: int = 10_000,
    ):
        """
        Yields up to ``chunksize`` rows at a time.

        DuckDB scans the whole result natively in one shot; this just
        splits the resulting DataFrame into ``chunksize``-row pieces —
        same trade-off as ``BlobStorage.iter_table``/``SQLDatabase.query()``.
        """
        df = self.table(database, table_name, where=where)
        for start in range(0, len(df), chunksize):
            chunk = df.iloc[start : start + chunksize]
            if not chunk.empty:
                yield chunk

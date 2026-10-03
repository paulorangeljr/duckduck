"""
Shared DuckDB-native object-storage scanning helper, used by the
``glue``/``blob_storage`` connectors.

Unlike every other wrapper in ``duckduck`` (which fetches data over HTTP/
a DB driver into a ``pd.DataFrame`` in Python), these connectors read
Parquet/Delta/Iceberg files directly through DuckDB's own extensions
(``httpfs``, ``delta``, ``iceberg``, ``azure``) — DuckDB scans S3/Azure
Blob natively, with its own filter/projection push-down where the format
supports it. The wrapper still returns a ``pd.DataFrame`` in the end
(materializing via a private, per-connector DuckDB connection) to stay
consistent with the rest of the push-down contract: DuckAPI registers
that DataFrame and lets its *own* WHERE re-apply on top, exactly like any
other wrapper.

``INSTALL``/``LOAD`` for an extension the first time it's used requires
outbound internet access to DuckDB's extension repository (they're
downloaded once and cached under ``~/.duckdb/extensions``).
"""

import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

import duckdb
import pandas as pd

from ...common.logs import get_logger
from ...common.pushdown import Condition, conditions_to_sql, order_sql

logger = get_logger("lakehouse")


class LakehouseConnection:
    """
    Owns a private DuckDB connection used purely for scanning object
    storage — never shared with the caller's own ``DuckAPI`` connection.
    """

    def __init__(self):
        self._conn = duckdb.connect()
        self._loaded_extensions = set()
        #: one scan at a time: the web app, SQL clients and Spark may share a connector, not a DuckDB connection
        self._lock = threading.RLock()
        # listings (HEAD/LIST) and Parquet footers read once, reused by the next scans of the same files
        for setting in ("enable_http_metadata_cache", "parquet_metadata_cache"):
            try:
                self._conn.execute(f"SET {setting} = true")
            except Exception as exc:  # noqa: BLE001 — an older DuckDB without it just reads again
                logger.debug("DuckDB setting %s unavailable: %s", setting, exc)

    def ensure_extension(self, name: str) -> None:
        """Installs (if needed) and loads a DuckDB extension, once per instance."""
        if name not in self._loaded_extensions:
            self._conn.execute(f"INSTALL {name}")
            self._conn.execute(f"LOAD {name}")
            self._loaded_extensions.add(name)

    def create_secret(self, sql: str) -> None:
        """Runs a ``CREATE SECRET (...)`` statement against the private connection."""
        self.ensure_extension("httpfs")
        self._conn.execute(sql)

    def scan(
        self,
        scan_expression: str,
        limit: Optional[int] = None,
        where: Optional[List[Condition]] = None,
        variables: Optional[Dict[str, Any]] = None,
        order_by: Optional[List[Tuple[str, bool]]] = None,
    ) -> pd.DataFrame:
        """
        Runs ``SELECT * FROM {scan_expression} [WHERE ...] [LIMIT n]`` and
        returns a DataFrame. ``where`` conditions (from DuckAPI's push-down)
        go into the scan itself, so DuckDB prunes Parquet row groups/files
        and only matching rows ever reach Python; ones on columns the scan
        doesn't have are skipped (a ``DESCRIBE`` — metadata only — finds out).
        ``variables``: set first (``SET VARIABLE name = value``) — a file list
        the expression reads with ``getvariable('name')``, however long.
        ``order_by`` (``@sortable(exact=True)`` tables): ``ORDER BY … NULLS LAST``
        before the LIMIT — DuckDB itself, so exactly the order asked; a column
        the scan doesn't have drops the order and the limit together.
        """
        with self._lock:
            sql, _ = self._scan_sql(scan_expression, limit, where, variables, order_by)
            return self._conn.sql(sql).df()

    def scan_batches(
        self,
        scan_expression: str,
        where: Optional[List[Condition]] = None,
        variables: Optional[Dict[str, Any]] = None,
        order_by: Optional[List[Tuple[str, bool]]] = None,
        chunksize: int = 100_000,
    ) -> Iterator[pd.DataFrame]:
        """
        ``scan`` a piece at a time: DuckDB streams the result as Arrow record
        batches of ``chunksize`` rows, each yielded as a DataFrame — only one
        is in memory (the scan itself spills as DuckDB does). ``order_by``
        sorts the whole scan (any column; one it can't sort by is dropped,
        and the reader that asked sees the rows aren't sorted). An empty
        result yields one empty frame with the columns.
        """
        with self._lock:  # the connection is busy until the last batch is read
            sql, _ = self._scan_sql(scan_expression, None, where, variables, order_by)
            result = self._conn.execute(sql)
            reader = getattr(result, "to_arrow_reader", None) or result.fetch_record_batch
            batches = reader(chunksize)
            empty = True
            for batch in batches:
                if batch.num_rows:
                    empty = False
                    yield batch.to_pandas()
            if empty:
                yield batches.schema.empty_table().to_pandas()

    def _scan_sql(self, scan_expression: str, limit: Optional[int], where: Optional[List[Condition]],
                  variables: Optional[Dict[str, Any]], order_by: Optional[List[Tuple[str, bool]]]):
        """The scan's SQL (variables set first, under the caller's lock)."""
        for name, value in (variables or {}).items():
            self._conn.execute(f"SET VARIABLE {name} = ?", [value])
        sql = f"SELECT * FROM {scan_expression}"
        columns = ([row[0] for row in self._conn.sql(f"DESCRIBE {sql}").fetchall()]
                   if where or order_by else [])
        if where:
            body = conditions_to_sql(where, columns)
            if body:
                sql += f" WHERE {body}"
        if order_by:
            order = order_sql(order_by, columns)
            if order is None:
                limit = None  # both or neither: DuckAPI sorts and cuts what it reads
            else:
                sql += f" ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        logger.info("DuckDB scan: %s", sql)
        return sql, limit

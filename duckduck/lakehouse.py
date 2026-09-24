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

from typing import Optional

import duckdb
import pandas as pd


class LakehouseConnection:
    """
    Owns a private DuckDB connection used purely for scanning object
    storage — never shared with the caller's own ``DuckAPI`` connection.
    """

    def __init__(self):
        self._conn = duckdb.connect()
        self._loaded_extensions = set()

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

    def scan(self, scan_expression: str, limit: Optional[int] = None) -> pd.DataFrame:
        """Runs ``SELECT * FROM {scan_expression} [LIMIT n]`` and returns a DataFrame."""
        sql = f"SELECT * FROM {scan_expression}"
        if limit is not None:
            sql += f" LIMIT {limit}"
        return self._conn.sql(sql).df()

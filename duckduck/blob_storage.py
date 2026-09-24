"""
Azure Blob Storage / ADLS Gen2 connector for use with DuckAPI.

Reads Parquet/CSV/JSON/Delta/Iceberg files directly from Azure Blob
Storage through DuckDB's own ``azure`` extension (``az://`` protocol) —
same "scan natively in DuckDB, return a DataFrame" approach as the
``glue`` connector (see ``duckduck/lakehouse.py``).

Usage convention
-----------------
``container``/``path`` are structural (they build the ``az://`` URL) —
inline only::

    SELECT * FROM blob_table(container='data', path='events/2024/*.parquet')
     WHERE event_type = 'click' LIMIT 100

Column filters in ``WHERE`` aren't pushed down to the blob read itself —
DuckDB applies them on the scanned result, same as any push-down
parameter a wrapper doesn't recognize.
"""

from typing import Any, Dict, List, Optional

import pandas as pd

from .lakehouse import LakehouseConnection
from .pushdown import Condition


class BlobStorage:
    """
    Reads files from Azure Blob Storage / ADLS Gen2 via DuckDB.

    Parameters
    ----------
    account_name : str, optional
        Storage account name — used with ``PROVIDER credential_chain``
        (Azure CLI login, managed identity, environment credentials).
        Required unless ``connection_string`` is given.
    connection_string : str, optional
        A full Azure Storage connection string — takes precedence over
        ``account_name``/credential-chain auth when given.
    """

    def __init__(
        self,
        account_name: Optional[str] = None,
        connection_string: Optional[str] = None,
    ):
        self._lake = LakehouseConnection()
        self._lake.ensure_extension("azure")

        if connection_string:
            secret_sql = (
                "CREATE OR REPLACE SECRET duckduck_azure_blob (TYPE AZURE, "
                f"CONNECTION_STRING '{connection_string}')"
            )
        else:
            if not account_name:
                raise ValueError(
                    "BlobStorage requires either connection_string or "
                    "account_name (for credential_chain auth)."
                )
            secret_sql = (
                "CREATE OR REPLACE SECRET duckduck_azure_blob (TYPE AZURE, "
                f"PROVIDER CREDENTIAL_CHAIN, ACCOUNT_NAME '{account_name}')"
            )
        self._lake.create_secret(secret_sql)

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "BlobStorage":
        """
        Builds BlobStorage from a credentials dict. Expects either
        ``connection_string`` or ``account_name``.
        """
        return cls(
            account_name=overrides.pop("account_name", None) or secret.get("account_name"),
            connection_string=(
                overrides.pop("connection_string", None) or secret.get("connection_string")
            ),
            **overrides,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _scan_expression(self, container: str, path: str, format: str) -> str:
        full_path = f"az://{container}/{path.lstrip('/')}"
        if format == "parquet":
            return f"read_parquet('{full_path}')"
        if format == "csv":
            return f"read_csv('{full_path}')"
        if format == "json":
            return f"read_json('{full_path}')"
        if format == "delta":
            self._lake.ensure_extension("delta")
            return f"delta_scan('{full_path}')"
        if format == "iceberg":
            self._lake.ensure_extension("iceberg")
            return f"iceberg_scan('{full_path}')"
        raise ValueError(
            f"Unsupported format '{format}'. Use parquet, csv, json, delta, or iceberg."
        )

    def table(
        self,
        container: str,
        path: str,
        format: str = "parquet",
        where: Optional[List[Condition]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Reads files from a container.

        Parameters
        ----------
        container : str
            Structural — the blob container (or ADLS Gen2 filesystem) name.
        path : str
            Structural — path/glob within the container, e.g.
            ``"events/2024/*.parquet"`` or ``"delta_table"`` for a Delta
            table's root.
        format : {"parquet", "csv", "json", "delta", "iceberg"}
            Default ``"parquet"``.
        where : list[Condition], optional
            Filled by DuckAPI's push-down with the query's ``WHERE``
            conditions; they run inside the DuckDB scan itself (Parquet
            row-group/file pruning), so non-matching rows never reach Python.
        limit : int, optional
            Applied via DuckDB's own ``LIMIT`` on the scan.
        """
        scan_expr = self._scan_expression(container, path, format)
        return self._lake.scan(scan_expr, limit=limit, where=where)

    # ------------------------------------------------------------------
    # Streaming (iter_*) — for use with DuckAPI.stream()
    # ------------------------------------------------------------------

    def iter_table(
        self,
        container: str,
        path: str,
        format: str = "parquet",
        where: Optional[List[Condition]] = None,
        chunksize: int = 10_000,
    ):
        """
        Yields up to ``chunksize`` rows at a time.

        DuckDB scans the whole result natively in one shot (that's the
        point — it's efficient even without true incremental chunking
        here); this just splits the resulting DataFrame into
        ``chunksize``-row pieces, same trade-off as ``SQLDatabase.query()``'s
        client-side ``limit``.
        """
        df = self.table(container, path, format=format, where=where)
        for start in range(0, len(df), chunksize):
            chunk = df.iloc[start : start + chunksize]
            if not chunk.empty:
                yield chunk

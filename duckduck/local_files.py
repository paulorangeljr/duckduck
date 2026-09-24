"""
Local files as tables — the offline counterpart of the ``glue`` /
``blob_storage`` connectors, for development, demos and synthetic data.

Point it at a directory; every data file in it becomes a table:

- a top-level file (``assets.csv``, ``users.parquet``, ``events.jsonl``)
  → one table named after the file (``assets``, ``users``, ``events``);
- a top-level sub-directory of data files (``proxy/dt=2026-09-24/part-0.parquet``,
  ``dns/*.csv``) → one table named after the directory, reading every
  file of its format inside it (Hive-style ``key=value`` partitions become
  columns).

Formats: ``.csv``, ``.tsv``, ``.parquet``, ``.json``, ``.jsonl`` / ``.ndjson``
— all read natively by DuckDB, offline. Each table takes the ``where``
push-down param, so WHERE conditions run inside the DuckDB scan (Parquet
row-group/file pruning), exactly like the lakehouse connectors.

Via ``auto_register()``::

    "local": {"connector": "files", "path": "./data", "table_prefix": ""}
"""

import os
import re
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from .lakehouse import LakehouseConnection
from .pushdown import Condition

#: extension → (format label, DuckDB reader call template).
_READERS = {
    ".csv": ("csv", "read_csv({path})"),
    ".tsv": ("tsv", "read_csv({path}, delim='\\t')"),
    ".parquet": ("parquet", "read_parquet({path}, hive_partitioning=true, union_by_name=true)"),
    ".json": ("json", "read_json({path})"),
    ".jsonl": ("jsonl", "read_json({path}, format='newline_delimited')"),
    ".ndjson": ("jsonl", "read_json({path}, format='newline_delimited')"),
}


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def table_name_for(name: str) -> str:
    """``Proxy Logs-2026.csv`` → ``proxy_logs_2026``: a safe, lowercase SQL name."""
    stem = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    if not stem or stem[0].isdigit():
        stem = f"t_{stem}"
    return stem


class FileTable:
    """
    One file/directory exposed as a DuckAPI table: call it with the
    push-down ``where`` and ``limit`` and it scans the data with DuckDB.
    """

    __module__ = __name__  # list_tables() labels it "Local files"

    def __init__(self, lake: LakehouseConnection, table_name: str, path: str, file_format: str, scan: str):
        self._lake = lake
        self.table_name = table_name
        self.base_url = path  # what list_tables() shows as the endpoint
        self.format = file_format
        self.scan_expression = scan
        self.__doc__ = f"{file_format.upper()} data at {path} (WHERE/LIMIT run inside the DuckDB scan)."

    def __call__(self, where: Optional[List[Condition]] = None, limit: Optional[int] = None) -> pd.DataFrame:
        return self._lake.scan(self.scan_expression, limit=limit, where=where)

    def __repr__(self) -> str:
        return f"FileTable({self.table_name!r}, {self.base_url!r})"


class LocalFiles:
    """
    Parameters
    ----------
    path : str
        Directory to scan (relative paths resolve against the current
        directory — or, via ``auto_register()``'s JSON config, the config
        file's own directory).
    include_hidden : bool
        Also pick up files/directories starting with ``.`` or ``_``
        (skipped by default: ``_SUCCESS`` markers, ``.DS_Store``...).
    """

    def __init__(self, path: str, include_hidden: bool = False):
        if not os.path.isdir(path):
            raise ValueError(f"'{path}' is not a directory")
        self.path = os.path.abspath(path)
        self.base_url = self.path
        self.include_hidden = include_hidden
        self._lake = LakehouseConnection()
        self._tables: Dict[str, FileTable] = {}
        self._discover()

    @classmethod
    def from_secret(cls, secret: Dict, **overrides) -> "LocalFiles":
        """No credentials involved — everything comes from the service config."""
        merged = {**secret, **overrides}
        if "path" not in merged:
            raise ValueError("The 'files' connector needs a 'path' (a directory).")
        return cls(**merged)

    # ------------------------------------------------------------------

    def _visible(self, name: str) -> bool:
        return self.include_hidden or not name.startswith((".", "_"))

    def _discover(self) -> None:
        for entry in sorted(os.listdir(self.path)):
            if not self._visible(entry):
                continue
            full = os.path.join(self.path, entry)
            if os.path.isfile(full):
                ext = os.path.splitext(entry)[1].lower()
                if ext in _READERS:
                    self._add(table_name_for(os.path.splitext(entry)[0]), full, ext, _sql_string(full))
            elif os.path.isdir(full):
                ext = self._dominant_extension(full)
                if ext:
                    pattern = os.path.join(full, "**", f"*{ext}")
                    self._add(table_name_for(entry), full, ext, _sql_string(pattern))

    def _dominant_extension(self, directory: str) -> Optional[str]:
        """The most common data-file extension anywhere under ``directory``."""
        counts: Dict[str, int] = {}
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if self._visible(d)]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in _READERS and self._visible(f):
                    counts[ext] = counts.get(ext, 0) + 1
        return max(counts, key=counts.get) if counts else None

    def _add(self, name: str, path: str, ext: str, path_sql: str) -> None:
        if name in self._tables:
            raise ValueError(
                f"two data sources under '{self.path}' map to the table name '{name}' "
                f"({self._tables[name].base_url} and {path}) — rename one"
            )
        label, template = _READERS[ext]
        self._tables[name] = FileTable(self._lake, name, path, label, template.format(path=path_sql))

    # ------------------------------------------------------------------

    def table_functions(self) -> Dict[str, FileTable]:
        """``{table_name: callable}`` — what ``auto_register()`` registers."""
        return dict(self._tables)

    def tables(self, limit: Optional[int] = None) -> pd.DataFrame:
        """Lists the discovered tables: name, format, path, size and last modification."""
        rows = []
        for name, t in self._tables.items():
            if os.path.isdir(t.base_url):
                sizes = [os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(t.base_url) for f in fs]
                mtimes = [os.path.getmtime(os.path.join(r, f)) for r, _, fs in os.walk(t.base_url) for f in fs]
            else:
                sizes, mtimes = [os.path.getsize(t.base_url)], [os.path.getmtime(t.base_url)]
            rows.append({
                "table_name": name,
                "format": t.format,
                "path": t.base_url,
                "files": len(sizes),
                "size_bytes": sum(sizes),
                "modified": datetime.fromtimestamp(max(mtimes)) if mtimes else None,
            })
        df = pd.DataFrame(rows, columns=["table_name", "format", "path", "files", "size_bytes", "modified"])
        return df.head(limit) if limit is not None else df

    def columns(self, table_name: str) -> pd.DataFrame:
        """A table's columns and DuckDB types (structural ``table_name``)."""
        if table_name not in self._tables:
            raise ValueError(f"unknown table '{table_name}' (have: {', '.join(self._tables)})")
        rows = self._lake._conn.sql(f"DESCRIBE SELECT * FROM {self._tables[table_name].scan_expression}").fetchall()
        return pd.DataFrame(
            [{"table_name": table_name, "column_name": r[0], "data_type": r[1]} for r in rows]
        )

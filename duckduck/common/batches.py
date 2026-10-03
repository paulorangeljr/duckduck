"""
Reading a source in batches of Parquet files instead of one DataFrame in memory.

Under ``batching(Batching(rows, folder))`` DuckAPI reads every table that has a
page-by-page reader (``iter_<table>``) page by page, keeps what passes the
table's WHERE, and every ``rows`` rows writes them as a Parquet file under
``folder`` — a local folder or an object store (``s3://bucket/staging/…``,
written with pyarrow through ``filesystem``: a pipeline passes the lake's AWS
account); the query then reads those files as an Arrow dataset — memory holds
one batch at a time, never the table. A pipeline turns it on with
``run_pipeline(batch_rows=…)``.

Each source read has its own folder (``<table>-<hash of the call>``) with a
``manifest.json``: the files, the rows, ``done`` and — when the source can resume
(``IDEMPOTENCY`` ``checkpoint``) — the sort column and ``after``, the last value
whose rows are all in the files. A later attempt at the same run finds it: a
``done`` read is taken from its files, an unfinished one continues after
``after`` (or starts over when there's no checkpoint).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger("duckduck.batches")

MANIFEST = "manifest.json"


def local_filesystem(location: str):
    """(pyarrow filesystem, path in it) for a local folder — the default; a pipeline passes its own resolver,
    which reads ``s3://…`` with the lake's AWS account."""
    import pyarrow.fs as pafs

    if "://" in location:
        return pafs.FileSystem.from_uri(location)
    return pafs.LocalFileSystem(), os.path.abspath(os.path.expanduser(location))


@dataclass
class Batching:
    #: rows per Parquet file (and the most kept in memory at once, besides one page)
    rows: int
    #: where this query's sources are staged (one folder per source read): a local folder or ``s3://…``
    folder: str
    #: resume after a failure from the staged batches (False: every read starts over)
    resume: bool = True
    #: the column to resume after, when the pipeline names one (else the incremental load's, else the connector's)
    column: Optional[str] = None
    #: the incremental load's columns: preferred over the connector's default when the source sorts by them
    load_columns: Sequence[str] = ()
    #: ``location -> (pyarrow filesystem, path in it)``
    filesystem: Callable[[str], Tuple[Any, str]] = local_filesystem
    #: one line per source read: what happened (staged / reused / resumed / restarted, why)
    notes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.rows, int) or isinstance(self.rows, bool) or self.rows < 1:
            raise ValueError(f"batch_rows must be a whole number ≥ 1, not {self.rows!r}")

    def source(self, table: str, call: Any) -> "StagedSource":
        digest = hashlib.sha1(repr(call).encode("utf-8")).hexdigest()[:12]
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in table)[:60]
        fs, base = self.filesystem(self.folder)
        return StagedSource(fs, f"{base.rstrip('/')}/{safe}-{digest}", table, digest,
                            f"{self.folder.rstrip('/')}/{safe}-{digest}")

    def note(self, line: str) -> None:
        self.notes.append(line)
        logger.info("  %s", line)


_CURRENT: contextvars.ContextVar[Optional[Batching]] = contextvars.ContextVar("duckduck_batching", default=None)


def current() -> Optional[Batching]:
    return _CURRENT.get()


@contextlib.contextmanager
def batching(spec: Optional[Batching]) -> Iterator[Optional[Batching]]:
    """Reads under it go to Parquet batches (``spec`` None: nothing changes)."""
    token = _CURRENT.set(spec)
    try:
        yield spec
    finally:
        _CURRENT.reset(token)


def _plain(value: Any) -> Any:
    """A value for the manifest's JSON: numbers stay numbers, the rest text (a timestamp as DuckDB writes it)."""
    if hasattr(value, "item") and not hasattr(value, "isoformat"):
        value = value.item()
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, str)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ")
    return str(value)


def unified_schema(schemas: List[Any]) -> Any:
    """One schema for every batch: columns of all of them; a column typed differently in two batches widens
    (an all-NULL batch's column takes the other's type), and becomes text when nothing else fits both."""
    import pyarrow as pa

    try:
        return pa.unify_schemas(schemas, promote_options="permissive")
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError):
        pass
    fields: Dict[str, Any] = {}
    for schema in schemas:
        for f in schema:
            seen = fields.get(f.name)
            if seen is None or pa.types.is_null(seen.type):
                fields[f.name] = f
            elif not pa.types.is_null(f.type) and f.type != seen.type:
                try:
                    fields[f.name] = pa.unify_schemas([pa.schema([seen]), pa.schema([f])],
                                                      promote_options="permissive").field(0)
                except (pa.ArrowInvalid, pa.ArrowTypeError):
                    fields[f.name] = pa.field(f.name, pa.string())
    return pa.schema(list(fields.values()))


class StagedSource:
    """One source read's folder: its Parquet batches and the manifest that says how far it got."""

    def __init__(self, fs: Any, path: str, table: str, call_key: str, location: Optional[str] = None):
        self.fs = fs
        self.path = path  # inside fs
        self.location = location or path  # as written (s3://…), for messages
        self.table = table
        self.call_key = call_key
        self.files: List[str] = []
        self.rows = 0
        self.done = False
        self.column: Optional[str] = None
        self.after: Any = None
        self.columns: List[str] = []
        self._load()

    def _exists(self, path: str) -> bool:
        import pyarrow.fs as pafs

        return self.fs.get_file_info(path).type != pafs.FileType.NotFound

    def _load(self) -> None:
        manifest = f"{self.path}/{MANIFEST}"
        try:
            if not self._exists(manifest):
                return
            with self.fs.open_input_stream(manifest) as f:
                data = json.loads(f.read().decode("utf-8"))
        except (OSError, ValueError):
            return
        if data.get("call") != self.call_key:
            return
        files = [f"{self.path}/{f}" for f in data.get("files") or []]
        if not all(self._exists(f) for f in files):  # a file went missing: nothing here can be trusted
            return
        self.files, self.rows, self.done = files, int(data.get("rows") or 0), bool(data.get("done"))
        self.column, self.after = data.get("column"), data.get("after")
        self.columns = list(data.get("columns") or [])

    @property
    def resumable(self) -> bool:
        return bool(self.files) and not self.done and self.column is not None and self.after is not None

    def reset(self) -> None:
        """Starts over: the batches of an unfinished read without a checkpoint are thrown away."""
        try:
            self.fs.delete_dir(self.path)
        except (OSError, FileNotFoundError):
            pass
        self.files, self.rows, self.done, self.column, self.after, self.columns = [], 0, False, None, None, []

    def write(self, table: Any, after: Any = None) -> None:
        """One batch (an Arrow table) as the next Parquet file, then the manifest that counts it."""
        import pyarrow.parquet as pq

        self.fs.create_dir(self.path, recursive=True)
        path = f"{self.path}/batch-{len(self.files):05d}.parquet"
        pq.write_table(table, path, filesystem=self.fs)
        self.files.append(path)
        self.rows += table.num_rows
        if self.column is not None:
            self.after = _plain(after)
        self.columns = list(table.column_names)
        self.save()

    def dataset(self) -> Any:
        """Every batch as one Arrow dataset (lazy: DuckDB reads it a piece at a time)."""
        import pyarrow.dataset as ds
        import pyarrow.parquet as pq

        schema = unified_schema([pq.read_schema(f, filesystem=self.fs) for f in self.files])
        return ds.dataset(self.files, schema=schema, filesystem=self.fs, format="parquet")

    def finish(self, columns: Optional[List[str]] = None) -> None:
        self.done = True
        if columns and not self.files:
            self.columns = list(columns)
        self.save()

    def save(self) -> None:
        self.fs.create_dir(self.path, recursive=True)
        data: Dict[str, Any] = {"table": self.table, "call": self.call_key, "rows": self.rows, "done": self.done,
                                "files": [f.rsplit("/", 1)[-1] for f in self.files], "column": self.column,
                                "after": self.after, "columns": self.columns}
        # one object written whole (S3 puts are atomic); a crash mid-write leaves the previous manifest
        target = f"{self.path}/{MANIFEST}"
        with self.fs.open_output_stream(target + ".tmp") as f:
            f.write(json.dumps(data, indent=1, default=str).encode("utf-8"))
        self.fs.move(target + ".tmp", target)


def delete(location: str, filesystem: Callable[[str], Tuple[Any, str]] = local_filesystem) -> None:
    """Removes a staging folder (a run's, once it succeeded) — local or ``s3://…``; never fails."""
    try:
        fs, base = filesystem(location)
        fs.delete_dir(base)
    except Exception as exc:  # noqa: BLE001
        logger.debug("couldn't remove %s: %s", location, exc)

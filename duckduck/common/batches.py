"""
Reading a source in batches on disk instead of one DataFrame in memory.

Under ``batching(Batching(rows, folder))`` DuckAPI reads every table that has a
page-by-page reader (``iter_<table>``) page by page, keeps what passes the
table's WHERE, and every ``rows`` rows writes them as a Parquet file under
``folder``; the query then reads those files (``read_parquet``) — memory holds
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
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence

logger = logging.getLogger("duckduck.batches")

MANIFEST = "manifest.json"


@dataclass
class Batching:
    #: rows per Parquet file (and the most kept in memory at once, besides one page)
    rows: int
    #: where this query's sources are staged (one folder per source read)
    folder: str
    #: resume after a failure from the staged batches (False: every read starts over)
    resume: bool = True
    #: the column to resume after, when the pipeline names one (else the incremental load's, else the connector's)
    column: Optional[str] = None
    #: the incremental load's columns: preferred over the connector's default when the source sorts by them
    load_columns: Sequence[str] = ()
    #: one line per source read: what happened (staged / reused / resumed / restarted, why)
    notes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.rows, int) or isinstance(self.rows, bool) or self.rows < 1:
            raise ValueError(f"batch_rows must be a whole number ≥ 1, not {self.rows!r}")

    def source(self, table: str, call: Any) -> "StagedSource":
        digest = hashlib.sha1(repr(call).encode("utf-8")).hexdigest()[:12]
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in table)[:60]
        return StagedSource(os.path.join(self.folder, f"{safe}-{digest}"), table, digest)

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


class StagedSource:
    """One source read's folder: its Parquet batches and the manifest that says how far it got."""

    def __init__(self, path: str, table: str, call_key: str):
        self.path = path
        self.table = table
        self.call_key = call_key
        self.files: List[str] = []
        self.rows = 0
        self.done = False
        self.column: Optional[str] = None
        self.after: Any = None
        self.columns: List[str] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(os.path.join(self.path, MANIFEST), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if data.get("call") != self.call_key:
            return
        files = [os.path.join(self.path, f) for f in data.get("files") or []]
        if not all(os.path.isfile(f) for f in files):  # a file went missing: nothing here can be trusted
            return
        self.files, self.rows, self.done = files, int(data.get("rows") or 0), bool(data.get("done"))
        self.column, self.after = data.get("column"), data.get("after")
        self.columns = list(data.get("columns") or [])

    @property
    def resumable(self) -> bool:
        return bool(self.files) and not self.done and self.column is not None and self.after is not None

    def reset(self) -> None:
        """Starts over: the batches of an unfinished read without a checkpoint are thrown away."""
        shutil.rmtree(self.path, ignore_errors=True)
        self.files, self.rows, self.done, self.column, self.after, self.columns = [], 0, False, None, None, []

    def next_file(self) -> str:
        os.makedirs(self.path, exist_ok=True)
        return os.path.join(self.path, f"batch-{len(self.files):05d}.parquet")

    def added(self, path: str, rows: int, after: Any = None, columns: Optional[List[str]] = None) -> None:
        self.files.append(path)
        self.rows += rows
        if self.column is not None:
            self.after = _plain(after)
        if columns:
            self.columns = list(columns)
        self.save()

    def finish(self, columns: Optional[List[str]] = None) -> None:
        self.done = True
        if columns:
            self.columns = list(columns)
        self.save()

    def save(self) -> None:
        os.makedirs(self.path, exist_ok=True)
        data: Dict[str, Any] = {"table": self.table, "call": self.call_key, "rows": self.rows, "done": self.done,
                                "files": [os.path.basename(f) for f in self.files], "column": self.column,
                                "after": self.after, "columns": self.columns}
        target = os.path.join(self.path, MANIFEST)
        with open(target + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, default=str)
        os.replace(target + ".tmp", target)  # a crash mid-write leaves the previous manifest whole

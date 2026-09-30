"""
What each source returned, kept for a while (``DuckAPI(cache=...)``).

``DuckAPI.sql()`` reads a source through one call — the function, the
arguments and the push-down it got (or, page by page, the conditions and
columns it kept). The same call within ``ttl`` seconds returns the frame it
returned before instead of calling the API again. DuckDB still runs the
whole query over it, so a query that only changes its SELECT, ORDER BY,
GROUP BY or a condition that stayed with DuckDB reuses the read; one that
sends the source a different filter reads again.

- ``with cache.refreshing():`` — every lookup misses, what's read replaces
  what was kept ("run again without the cache").
- ``with cache.collecting() as used:`` — ``used`` gets one entry per source
  read in that block: ``{table, cached, age_s, rows}``.

Bounded by ``max_rows`` (total rows kept, least recently used dropped
first; a frame bigger than that is never kept). Thread-safe.
"""

from __future__ import annotations

import contextvars
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

#: Default time a read is reused: 10 minutes.
DEFAULT_TTL = 600.0
#: Default total rows kept across every read.
DEFAULT_MAX_ROWS = 2_000_000

_REFRESH: contextvars.ContextVar[bool] = contextvars.ContextVar("duckduck_cache_refresh", default=False)
_USED: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = contextvars.ContextVar("duckduck_cache_used",
                                                                                         default=None)

_AGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(value: Union[int, float, str]) -> float:
    """``600`` / ``"600"`` / ``"30s"`` / ``"10m"`` / ``"1h"`` / ``"1d"`` → seconds."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"a duration can't be negative: {value!r}")
        return float(value)
    m = _AGE_RE.match(str(value))
    if not m:
        raise ValueError(f"not a duration: {value!r} (seconds, or like \"30s\", \"10m\", \"1h\")")
    return float(m.group(1)) * _UNITS[m.group(2).lower()]


@contextmanager
def refreshing(on: bool = True) -> Iterator[None]:
    """Inside it, every lookup misses: sources are read again and the new reads are kept."""
    token = _REFRESH.set(on)
    try:
        yield
    finally:
        _REFRESH.reset(token)


@contextmanager
def collecting() -> Iterator[List[Dict[str, Any]]]:
    """The sources read inside it, whether from the cache or not: ``{table, cached, age_s, rows}``."""
    used: List[Dict[str, Any]] = []
    token = _USED.set(used)
    try:
        yield used
    finally:
        _USED.reset(token)


def note(table: str, cached: bool, rows: int, age_s: Optional[float] = None) -> None:
    used = _USED.get()
    if used is not None:
        used.append({"table": table, "cached": cached, "rows": int(rows),
                     "age_s": None if age_s is None else round(age_s, 1)})


def key_of(*parts: Any) -> str:
    """A stable key for a call: its parts' reprs (``Condition`` is a dataclass, dict items sorted)."""
    def frozen(v: Any) -> Any:
        if isinstance(v, dict):
            return tuple(sorted((str(k), frozen(x)) for k, x in v.items()))
        if isinstance(v, (list, tuple)):
            return tuple(frozen(x) for x in v)
        if isinstance(v, (set, frozenset)):
            return tuple(sorted(repr(frozen(x)) for x in v))
        return v
    return repr(tuple(frozen(p) for p in parts))


class SourceCache:
    """Frames by call key, each kept ``ttl`` seconds; ``max_rows`` rows at most in all."""

    def __init__(self, ttl: Union[int, float, str] = DEFAULT_TTL, max_rows: int = DEFAULT_MAX_ROWS,
                 clock: Any = time.time):
        self.ttl = seconds(ttl)
        self.max_rows = int(max_rows)
        self._clock = clock
        self._items: "OrderedDict[str, Tuple[float, int, Any]]" = OrderedDict()
        self._rows = 0
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, value: Any) -> Optional["SourceCache"]:
        """The config file's ``sql_cache``: ``false`` → off; missing / ``true`` → the defaults; ``{ttl, max_rows}``."""
        if value is False:
            return None
        if value is None or value is True:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("sql_cache must be true, false or {\"ttl\": \"10m\", \"max_rows\": 2000000}")
        unknown = set(value) - {"ttl", "max_rows", "enabled"}
        if unknown:
            raise ValueError(f"sql_cache: unknown option(s) {', '.join(sorted(unknown))} (ttl, max_rows, enabled)")
        if value.get("enabled") is False:
            return None
        max_rows = value.get("max_rows", DEFAULT_MAX_ROWS)
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0:
            raise ValueError("sql_cache.max_rows must be a whole number ≥ 0")
        try:
            ttl = seconds(value.get("ttl", DEFAULT_TTL))
        except ValueError as exc:
            raise ValueError(f"sql_cache.ttl: {exc}") from None
        return cls(ttl=ttl, max_rows=max_rows)

    def get(self, key: str) -> Optional[Tuple[Any, float]]:
        """``(value, age in seconds)``, or None — expired, not kept, or inside ``refreshing()``."""
        if _REFRESH.get() or self.ttl <= 0:
            return None
        now = self._clock()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            at, rows, value = item
            if now - at > self.ttl:
                del self._items[key]
                self._rows -= rows
                return None
            self._items.move_to_end(key)
            return value, now - at

    def put(self, key: str, value: Any, rows: int) -> None:
        if self.ttl <= 0 or rows > self.max_rows:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._rows -= old[1]
            self._items[key] = (self._clock(), rows, value)
            self._rows += rows
            while self._rows > self.max_rows and self._items:
                _, (_, dropped, _) = self._items.popitem(last=False)
                self._rows -= dropped

    def clear(self) -> int:
        """Drops everything kept; returns how many reads were dropped."""
        with self._lock:
            n = len(self._items)
            self._items.clear()
            self._rows = 0
            return n

    def status(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            live = [(at, rows) for at, rows, _ in self._items.values() if now - at <= self.ttl]
        return {"enabled": self.ttl > 0, "ttl_s": self.ttl, "max_rows": self.max_rows,
                "entries": len(live), "rows": sum(r for _, r in live)}

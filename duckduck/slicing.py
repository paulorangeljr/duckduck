"""
Reading part of a table: the contract between a connector's pager and a
caller that splits the work (the Spark reader, ``duckduck.spark``).

A connector whose API numbers its pages (``page=N``) or takes an offset
(``offset=N``), and reports how many rows there are in all, writes its page
loop with ``pages(fetch_page, page_size)`` instead of its own ``while``.
Every table read through that loop can then be split:

- ``with probing() as p: table(**kwargs)`` makes the loop send only the
  first request and note ``p.total_rows`` / ``p.page_size`` — how big the
  read is, for one request;
- ``with window(first_page, pages) as w: table(**kwargs)`` reads only those
  pages (``w.seen`` tells the caller the loop honored it — a table that
  doesn't read through it would return everything once per window).

Without either, ``pages`` reads everything, as the connector always did.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, List, Optional, Tuple


@dataclass
class Window:
    first_page: int
    pages: int
    #: set by ``pages()`` when it read through this window
    seen: bool = False


@dataclass
class Probe:
    total_rows: Optional[int] = None
    page_size: Optional[int] = None
    #: set by ``pages()`` when the read went through it
    seen: bool = False


_WINDOW: contextvars.ContextVar[Optional[Window]] = contextvars.ContextVar("duckduck_window", default=None)
_PROBE: contextvars.ContextVar[Optional[Probe]] = contextvars.ContextVar("duckduck_probe", default=None)


@contextmanager
def window(first_page: int, pages: int) -> Iterator[Window]:
    """Reads in this block see only pages ``first_page`` … ``first_page + pages - 1``."""
    w = Window(int(first_page), int(pages))
    token = _WINDOW.set(w)
    try:
        yield w
    finally:
        _WINDOW.reset(token)


@contextmanager
def probing() -> Iterator[Probe]:
    """Reads in this block send their first request only, and say how many rows there are."""
    p = Probe()
    token = _PROBE.set(p)
    try:
        yield p
    finally:
        _PROBE.reset(token)


def pages(fetch_page: Callable[[int], Tuple[List[Any], Optional[int]]], page_size: int) -> Iterator[List[Any]]:
    """
    A connector's page loop. ``fetch_page(n)`` requests page ``n`` (0-based;
    an offset API sends ``n * page_size``) and returns ``(rows, total_rows)``
    — ``total_rows`` None when the API doesn't say. Yields each non-empty
    page; stops at the total when the API gives one (a page shorter than
    asked may just be the server's own cap), else at a short page.
    """
    probe = _PROBE.get()
    if probe is not None:
        rows, total = fetch_page(0)
        probe.total_rows, probe.page_size, probe.seen = total, page_size, True
        if rows:
            yield rows
        return
    win = _WINDOW.get()
    page, end = (win.first_page, win.first_page + win.pages) if win else (0, None)
    if win is not None:
        win.seen = True
    while end is None or page < end:
        rows, total = fetch_page(page)
        if rows:
            yield rows
        if not rows or ((page + 1) * page_size >= total if total is not None else len(rows) < page_size):
            break
        page += 1

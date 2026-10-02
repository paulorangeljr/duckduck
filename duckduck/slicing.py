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

Without either, ``pages`` reads everything, as the connector always did —
one page after another, or, inside ``with parallel(n):``, up to ``n`` pages at
once once the first page told the total (``DuckAPI`` sets it from the table's
``@spark_plan(max_parallel=…)``: the API's own limit). Pages are still
yielded in order; closing the loop early cancels what hasn't started.
"""

from __future__ import annotations

import contextvars
from collections import deque
from concurrent.futures import ThreadPoolExecutor
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


_PARALLEL: contextvars.ContextVar[int] = contextvars.ContextVar("duckduck_parallel", default=1)


@contextmanager
def parallel(requests: int) -> Iterator[int]:
    """Reads in this block may request up to ``requests`` pages at once (once a total is known)."""
    token = _PARALLEL.set(max(1, int(requests or 1)))
    try:
        yield _PARALLEL.get()
    finally:
        _PARALLEL.reset(token)


@contextmanager
def probing() -> Iterator[Probe]:
    """Reads in this block send their first request only, and say how many rows there are."""
    p = Probe()
    token = _PROBE.set(p)
    try:
        yield p
    finally:
        _PROBE.reset(token)


class PageCapError(RuntimeError):
    """The server returned fewer rows than a page asked for while it says there are more: it caps its pages
    below ``default_page_size``, and reading on by offsets of that size would skip rows."""


def _check_first(rows: List[Any], page_size: int, total: Optional[int]) -> None:
    if total is not None and rows and len(rows) < page_size and len(rows) < int(total):
        raise PageCapError(
            f"the server returned {len(rows)} rows for a page of {page_size} and says there are {int(total):,}: "
            f"it caps a page at {len(rows)}, and reading on in steps of {page_size} would skip rows — set the "
            f"service's \"default_page_size\" to {len(rows)} (or less) in duckduck.json")


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
        _check_first(rows, page_size, total)
        probe.total_rows, probe.page_size, probe.seen = total, page_size, True
        if rows:
            yield rows
        return
    win = _WINDOW.get()
    page, end = (win.first_page, win.first_page + win.pages) if win else (0, None)
    if win is not None:
        win.seen = True
    elif _PARALLEL.get() > 1:
        yield from _parallel_pages(fetch_page, page_size, _PARALLEL.get())
        return
    while end is None or page < end:
        rows, total = fetch_page(page)
        if page == 0:
            _check_first(rows, page_size, total)
        if rows:
            yield rows
        if not rows or ((page + 1) * page_size >= total if total is not None else len(rows) < page_size):
            break
        page += 1


def _parallel_pages(fetch_page: Callable[[int], Tuple[List[Any], Optional[int]]], page_size: int,
                    workers: int) -> Iterator[List[Any]]:
    """Page 0 first (it tells the total), then the rest ``workers`` at a time, yielded in order. Without a total
    the API can't be read out of order: one page after another, as ``pages`` does."""
    rows, total = fetch_page(0)
    _check_first(rows, page_size, total)
    if rows:
        yield rows
    if not rows or total is None:
        if rows and total is None and len(rows) >= page_size:
            page = 1
            while True:
                rows, _ = fetch_page(page)
                if rows:
                    yield rows
                if not rows or len(rows) < page_size:
                    return
                page += 1
        return
    last = -(-int(total) // page_size)  # pages in all
    if last <= 1:
        return
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="duckduck-page")
    pending: deque = deque()
    following = iter(range(1, last))
    try:
        for page in following:  # the first `workers` requests
            pending.append(pool.submit(contextvars.copy_context().run, fetch_page, page))
            if len(pending) >= workers:
                break
        while pending:
            rows, _ = pending.popleft().result()
            nxt = next(following, None)
            if nxt is not None:
                pending.append(pool.submit(contextvars.copy_context().run, fetch_page, nxt))
            if not rows:
                return  # the data shrank since page 0 counted it: nothing after this
            yield rows
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

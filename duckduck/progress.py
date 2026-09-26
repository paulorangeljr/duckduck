"""
Live progress for a long-running question — what it's doing now, and a way
to pause or cancel it.

The work reports steps (``step("retrieval", "Finding the tables…")``) and
what it has so far (``note(sql=…, decisions=…)``) to the ``Progress`` of
the current context (a ``ContextVar``: concurrent requests never mix, and
thread pools that submit with ``contextvars.copy_context()`` — e.g.
``metering.submit_in_context`` — carry it along). With no ``Progress`` set,
every call here is a no-op, so library use is unchanged.

Pausing and cancelling are cooperative: ``checkpoint()`` (called by every
``step``, before each decision-engine batch and between pages of a fetch)
blocks while paused and raises ``Cancelled`` once cancelled. A call already
in flight (an HTTP request to the engine or an API) finishes first — the
next checkpoint is where it stops. ``Cancelled`` is a ``BaseException`` on
purpose: the pipeline's ``except Exception`` fallbacks (a reading that fails
to plan, a probe that errors) must not swallow a cancel.
"""

import contextlib
import contextvars
import threading
import time
from typing import Any, Dict, Iterator, List, Optional


class Cancelled(BaseException):
    """The user cancelled the work (``Progress.cancel``)."""


class Progress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._running.set()
        self.cancelled = False
        self.started = time.perf_counter()
        #: One entry per step, in order: ``{i, stage, text, ms}`` (``ms`` since the start).
        self.events: List[Dict[str, Any]] = []
        #: What's there so far — decisions, SQL, rows read per source… (``note``).
        self.partial: Dict[str, Any] = {}
        #: ``running`` / ``pausing`` (asked, not reached a checkpoint yet) / ``paused``.
        self.state = "running"

    # -- the work's side -----------------------------------------------------------------------

    def step(self, stage: str, text: str) -> None:
        self.checkpoint()
        with self._lock:
            self.events.append({"i": len(self.events), "stage": stage, "text": text,
                                "ms": round((time.perf_counter() - self.started) * 1000)})

    def update(self, text: str) -> None:
        """Rewrites the latest step's text (page counts) without adding a line."""
        with self._lock:
            if self.events:
                self.events[-1] = {**self.events[-1], "text": text}

    def note(self, **partial: Any) -> None:
        with self._lock:
            self.partial.update(partial)

    def note_item(self, group: str, key: str, value: Any) -> None:
        """``partial[group][key] = value`` (one entry per source read, ...)."""
        with self._lock:
            self.partial[group] = {**self.partial.get(group, {}), key: value}

    def checkpoint(self) -> None:
        if self.cancelled:
            raise Cancelled()
        if not self._running.is_set():
            with self._lock:
                self.state = "paused"
            while not self._running.wait(0.2):
                if self.cancelled:
                    raise Cancelled()
            with self._lock:
                if self.state == "paused":
                    self.state = "running"
            if self.cancelled:
                raise Cancelled()

    # -- the user's side -----------------------------------------------------------------------

    def pause(self) -> None:
        with self._lock:
            if self.state == "running":
                self.state = "pausing"
        self._running.clear()

    def resume(self) -> None:
        with self._lock:
            self.state = "running"
        self._running.set()

    def cancel(self) -> None:
        self.cancelled = True
        self._running.set()  # a paused worker wakes up to see it

    def to_dict(self, since: int = 0) -> Dict[str, Any]:
        with self._lock:
            return {"state": "cancelled" if self.cancelled else self.state,
                    # from ``since``, plus the one before it: the step still running may have new counts
                    "events": self.events[max(0, since - 1):],
                    "partial": dict(self.partial),
                    "elapsed_ms": round((time.perf_counter() - self.started) * 1000)}


_current: contextvars.ContextVar[Optional[Progress]] = contextvars.ContextVar("duckduck_progress", default=None)
_quiet: contextvars.ContextVar[bool] = contextvars.ContextVar("duckduck_progress_quiet", default=False)


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """Steps in this block only checkpoint — for work done many times over (readings planned in parallel)."""
    token = _quiet.set(True)
    try:
        yield
    finally:
        _quiet.reset(token)


@contextlib.contextmanager
def tracking(progress: Optional[Progress]) -> Iterator[Optional[Progress]]:
    """Reports of the work in this block go to ``progress``."""
    token = _current.set(progress)
    try:
        yield progress
    finally:
        _current.reset(token)


def current() -> Optional[Progress]:
    return _current.get()


def step(stage: str, text: str) -> None:
    p = _current.get()
    if p is not None:
        p.checkpoint() if _quiet.get() else p.step(stage, text)


def update(text: str) -> None:
    p = _current.get()
    if p is not None and not _quiet.get():
        p.update(text)


def note(**partial: Any) -> None:
    p = _current.get()
    if p is not None and not _quiet.get():
        p.note(**partial)


def note_item(group: str, key: str, value: Any) -> None:
    p = _current.get()
    if p is not None and not _quiet.get():
        p.note_item(group, key, value)


def checkpoint() -> None:
    p = _current.get()
    if p is not None:
        p.checkpoint()

"""
What a connector says about reading again after a failure — its class attribute ``IDEMPOTENCY``.

A pipeline run that reads in batches (``run_pipeline(batch_rows=…)``) keeps every
batch it read on disk. When the run fails half-way, the next run is the same run
again (same run id, same parameters, the targets it already wrote skipped), and
for each source it was reading:

- ``checkpoint`` — it **resumes after the last batch**: the source sorts its pages
  by a column (``@sortable``) and the read can ask for ``column > <last value
  kept>``; nothing kept is read twice, nothing is lost (rows tied on the last value
  are always kept together). ``column`` is the connector's default — a column every
  row has and that only grows (a creation date, an id); a pipeline's
  ``"resume": {"column": …}`` or its incremental load column win when the source
  sorts by them. A connector without a default column (a database: tables differ)
  resumes only when the pipeline names one.
- ``restart`` — it **reads that source again from the start** (the batches of the
  unfinished read are thrown away; a source the failed run had read completely is
  still taken from its batches). ``why`` says what's missing: no sort, no
  "after this value" filter, one response.

Either way the targets don't get rows twice: the resumed run has the failed run's
id, skips the targets that run had written, and an append replaces that run's own
files / rows. Every connector in the registry declares one
(``tests/test_batches.py::test_every_connector_says_whether_it_can_resume``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

CHECKPOINT = "checkpoint"
RESTART = "restart"
MODES = (CHECKPOINT, RESTART)


@dataclass(frozen=True)
class Idempotency:
    #: ``checkpoint`` (resumes after the last batch kept) or ``restart`` (reads again from the start)
    mode: str
    #: one line: the fact that decides it — shown in the run's log when it resumes or restarts
    why: str
    #: ``checkpoint``: the default column to sort by and resume after (None: the pipeline must name one)
    column: Optional[str] = None

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"IDEMPOTENCY mode {self.mode!r}: one of {', '.join(MODES)}")
        if not self.why:
            raise ValueError("IDEMPOTENCY needs a why: the fact about the source that decides it")

    @property
    def resumes(self) -> bool:
        return self.mode == CHECKPOINT


def checkpoint(why: str, column: Optional[str] = None) -> Idempotency:
    return Idempotency(CHECKPOINT, why, column)


def restart(why: str) -> Idempotency:
    return Idempotency(RESTART, why)


def idempotency_of(fn: Any) -> Optional[Idempotency]:
    """The ``IDEMPOTENCY`` of the connector a table function belongs to (a bound method's instance, a saved
    table's connector, a callable object itself), or None."""
    from .sparkplan import owner_of

    owner = owner_of(fn)
    found = getattr(owner, "IDEMPOTENCY", None) if owner is not None else None
    return found if isinstance(found, Idempotency) else None

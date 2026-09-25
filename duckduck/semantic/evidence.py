"""
Live evidence for the decision engine: before deciding, check whether a
value from the question actually appears in a candidate field — one tiny
query per (field, value), its result sent to the engine as a fact.

A probe runs only when it's cheap and safe: the connector must apply the
filter itself (pushdown — ``col_ilike`` / ``where`` / ``col``) and take a
``limit``, so a probe is ``WHERE field ILIKE '%value%' LIMIT 1`` at the
source; otherwise it's skipped ("couldn't check"), never a full download.
There's a budget per question (``max_probes``) and a timeout per probe.
Opt-in: ``semantic.live_evidence.enabled``.
"""

import inspect
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .metering import submit_in_context
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..pushdown import Condition, blocker_of, map_conditions
from .catalog import Catalog

logger = logging.getLogger("duckduck.semantic")

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="evidence")


@dataclass
class Probe:
    field: str               #: source.field
    value: str
    found: Optional[bool]    #: None: couldn't check (no pushdown, over budget, timeout, error)
    seconds: float = 0.0
    note: str = ""


class EvidenceProber:
    """One per question: remembers what it checked, stops at ``max_probes``."""

    def __init__(self, catalog: Catalog, duck: Any, max_probes: int = 8, timeout: float = 5.0):
        self.catalog = catalog
        self.duck = duck
        self.max_probes = max_probes
        self.timeout = timeout
        self.probes: List[Probe] = []
        self._cache: Dict[Tuple[str, str], Optional[bool]] = {}
        self._runs = 0

    def found(self, ref: str, value: str) -> Optional[bool]:
        """Does ``value`` appear in ``ref`` (case-insensitive, as a substring)? ``None``: unknown."""
        key = (ref, str(value).lower())
        if key not in self._cache:
            self._cache[key] = self._probe(ref, str(value))
        return self._cache[key]

    def summary(self, value: str, refs: List[str]) -> Dict[str, Any]:
        """The fact sent to the engine: where the value was / wasn't found among ``refs``."""
        results = {ref: self.found(ref, value) for ref in refs}
        return {
            "value": value,
            "found_in": [r for r, v in results.items() if v is True],
            "not_found_in": [r for r, v in results.items() if v is False],
        }

    # ------------------------------------------------------------------

    def _probe(self, ref: str, value: str) -> Optional[bool]:
        source, fname = ref.split(".", 1)
        src = self.catalog.sources.get(source)
        if src is None or not src.table or fname not in src.fields:
            return None
        fn = self.duck.functions.get(src.table.lower()) if self.duck is not None else None
        if fn is None:
            return self._record(ref, value, None, 0.0, "table not registered")
        if self._runs >= self.max_probes:
            return self._record(ref, value, None, 0.0, "over the probe budget")
        params = set(inspect.signature(fn).parameters)
        if "limit" not in params:
            return self._record(ref, value, None, 0.0, "the source takes no limit")
        column = src.fields[fname].param or src.physical_column(fname)
        condition = Condition(column, "ilike", f"%{value}%")
        kwargs, consumed = map_conditions(params - set(src.args), [condition], blocker_of(fn))
        if condition not in consumed:
            return self._record(ref, value, None, 0.0, "the source can't filter this field itself")
        kwargs = {**src.args, **kwargs, "limit": 1}
        self._runs += 1
        started = time.perf_counter()
        try:
            df = submit_in_context(_POOL, lambda: self.duck.fetch(src.table, **kwargs)).result(timeout=self.timeout)
        except FutureTimeout:
            return self._record(ref, value, None, time.perf_counter() - started, f"timed out after {self.timeout:g}s")
        except Exception as exc:  # evidence is optional: a failing probe must not fail the question
            return self._record(ref, value, None, time.perf_counter() - started, f"failed: {exc}")
        seconds = time.perf_counter() - started
        col = column if column in df.columns else next((c for c in df.columns if c.lower() == column.lower()), None)
        if col is None:
            found = not df.empty  # the source filtered it; trust it
        else:
            found = bool(df[col].astype(str).str.contains(value, case=False, regex=False).any())
        return self._record(ref, value, found, seconds)

    def _record(self, ref: str, value: str, found: Optional[bool], seconds: float, note: str = "") -> Optional[bool]:
        self.probes.append(Probe(ref, value, found, seconds, note))
        outcome = {True: "found", False: "not found", None: f"not checked ({note})"}[found]
        logger.info("  evidence: %r in %s → %s%s", value, ref, outcome, f" ({seconds:.2f}s)" if seconds else "")
        return found

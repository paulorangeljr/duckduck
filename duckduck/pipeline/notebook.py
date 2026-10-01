"""
Developing a pipeline in Jupyter: one cell magic, ``%%sql``, and the sip
shown under each view while you write it.

    from duckduck.pipeline import notebook
    nb = notebook("gold/risk_by_region.json")    # key, sip and parameters from the pipeline file

    %%sql
    CREATE VIEW enriched AS
    SELECT i.*, a.region FROM glue.silver.incidents i LEFT JOIN glue.silver.assets a ON i.cmdb_ci = a.sys_id

A cell that makes a view shows how many rows it has and what happened to the
followed keys there (seen / changed / dropped / grouped…); a cell with a
SELECT shows its rows. Running a cell again replaces its view. Nothing is
ever written: the pipeline run (``python -m duckduck.pipeline run …``) does
that, from the same cells.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, Optional

import pandas as pd

from .analysis import Analyzer
from .engines import make_engine
from .runner import _duck
from .sip import Sip
from .sources import Statement, split_sql
from .spec import PipelineError, PipelineSpec, SipSpec, load_spec, run_parameters, substitute

logger = logging.getLogger("duckduck.pipeline")


class PipelineSession:
    """The views of a notebook, run as its cells are; ``sip`` holds every event so far."""

    def __init__(self, spec: Optional[PipelineSpec], engine: Any, params: Dict[str, Any]):
        self.spec = spec
        self.engine = engine
        self.params = params
        self.analyzer = Analyzer(spec.primary_key if spec else None, spec.keys if spec else None, redefine=True)
        sip_spec = spec.sip if spec and spec.sip.enabled else SipSpec(enabled=False)
        self._sip = Sip(sip_spec, engine, spec.name if spec else "notebook", "notebook",
                        params.get("run_at", ""), params.get("run_date", "")) if sip_spec.enabled else None
        self.cells = 0

    @property
    def sip(self) -> pd.DataFrame:
        return self._sip.dataframe() if self._sip else pd.DataFrame()

    @property
    def views(self):
        return list(self.analyzer.views)

    def view(self, name: str) -> pd.DataFrame:
        return self.engine.query(f'SELECT * FROM "{name}"')

    def run_cell(self, text: str) -> Any:
        """Runs a cell's statements; returns the last one's result (a view's sip preview, a query's rows)."""
        self.cells += 1
        result: Any = None
        for sql, _line in split_sql(substitute(text, self.params)):
            kind, what = self.analyzer.add(Statement(sql, f"cell {self.cells}"))
            if kind == "view":
                result = self._define(what)
            elif kind == "query":
                result = self.engine.frame(sql)
            else:
                result = self.engine.query(sql)
        return result

    def _define(self, view) -> Any:
        self.engine.define(view.name, view.sql, keep=True)
        rows = self.engine.rows(view.name)
        if self._sip is None or view.key.mode == "none":
            why = view.key.reason if view.key.mode == "none" else "the pipeline file has no sip"
            print(f"{view.name}: {f'{rows:,} rows' if rows is not None else 'ready'} · not followed ({why})")
            return self.engine.query(f'SELECT * FROM "{view.name}" LIMIT 10')
        # a view run again: its own events and every later state go (they're out of date)
        if self._sip.states:
            stale = list(self._sip.states)[list(self._sip.states).index(view.name):] \
                if view.name in self._sip.states else []
            for s in stale:
                self._sip.states.pop(s, None)
            self._sip.events = [e for e in self._sip.events if e["stage"] not in stale]
        before = len(self._sip.events)
        self._sip.observe(view)
        events = pd.DataFrame(self._sip.events[before:])
        counts = events["event"].value_counts().to_dict() if len(events) else {}
        print(f"{view.name}: {f'{rows:,} rows · ' if rows is not None else ''}{view.key.describe()}"
              + (" · " + ", ".join(f"{n} {e}" for e, n in counts.items()) if counts else " · no key followed"))
        if not len(events):
            return None
        return events[["key", "stage_key", "event", "n", "changed", "row", "note"]].reset_index(drop=True)

    def close(self) -> None:
        self.engine.close()


def notebook(pipeline: Optional[str] = None, duck: Any = None, spark: Any = None, config_path: Optional[str] = None,
             engine: Optional[str] = None, magic: str = "sql", **params: Any) -> PipelineSession:
    """
    Starts a session for developing a pipeline in a notebook and registers the
    ``%%sql`` cell magic (and ``%%duckduck``, the same — for when another
    library already has ``%%sql``). ``pipeline``: the pipeline file (key, sip,
    parameters, engine); ``params``: values for its ``{{ parameters }}``.
    """
    spec = load_spec(pipeline) if pipeline else None
    values = run_parameters(spec, params, now=dt.datetime.now(dt.timezone.utc), run_id="notebook") if spec else \
        {"run_date": dt.date.today().isoformat(), "run_at": dt.datetime.now().isoformat(sep=" "), **params}
    chosen = engine or (spec.engine if spec else "duckdb")
    session = PipelineSession(spec, make_engine(chosen, duck=_duck(duck, config_path), spark=spark), values)
    try:
        from IPython import get_ipython
    except ImportError:
        return session
    ip = get_ipython()
    if ip is None:
        return session

    def cell(line: str, cell_text: str) -> Any:
        try:
            return session.run_cell(cell_text)
        except PipelineError as exc:
            print(f"✗ {exc}")
            return None

    names = [magic, "duckduck"] if magic != "duckduck" else ["duckduck"]
    taken = ip.magics_manager.magics.get("cell", {})
    for name in names:
        if name in taken and name != "duckduck" and getattr(taken[name], "__duckduck__", False) is False:
            print(f"%%{name} belongs to another library here: use %%duckduck")
            continue
        cell.__duckduck__ = True  # type: ignore[attr-defined]
        ip.register_magic_function(cell, magic_kind="cell", magic_name=name)
    return session

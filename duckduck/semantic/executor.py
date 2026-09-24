"""
Plan execution on top of the virtualization layer (``DuckAPI``), with
per-source push-down — the MVP query optimizer.

Why not just ``duck.sql(compiled_sql)``: DuckAPI's SQL-level push-down is
global — it extracts one LIMIT and one WHERE from the whole query and
hands them to *every* function referenced, which is wrong for joins (the
outer ``LIMIT 1000`` would cap each side *before* the join) and for
non-equality predicates. Here each source is fetched on its own, with
only what's provably safe for that source:

- **Predicate push-down**: an ``eq`` filter goes to the API as a keyword
  argument when the function accepts one for that field (``param`` in
  the catalog, else the physical column name — the wrapper contract's
  equality semantics). DuckDB re-applies every filter anyway, so a
  looser server-side match (case, partial) never leaks into results.
- **Limit push-down**: only for a single-source, non-DISTINCT, unordered
  plan whose filters were *all* pushed (and no time range) — otherwise
  capping rows at the source would silently drop or misorder results.
- **Projection push-down**: each source CTE selects only the columns the
  plan touches (what the API returns is out of our hands, but native
  relations — Parquet, attached DBs — skip the rest at scan time).

Native ``relation:`` sources aren't fetched at all: the compiled CTE scans
them in place and DuckDB does its own filter/projection push-down.
"""

import inspect
import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Tuple

import pandas as pd

from .catalog import Catalog
from .compiler import compile_plan, default_order, display_relation
from .plan import LogicalQueryPlan

_EMPTY_DTYPES = {
    "string": "string",
    "integer": "Int64",
    "float": "float64",
    "boolean": "boolean",
    "datetime": "datetime64[ns]",
    "date": "datetime64[ns]",
}


class ExecutionError(RuntimeError):
    pass


@dataclass
class SourceFetch:
    """What was actually asked of one source — the execution audit trail."""

    source: str
    scan: str
    kwargs: Dict[str, Any] = field(default_factory=dict)
    pushed_filters: List[str] = field(default_factory=list)
    residual_filters: List[str] = field(default_factory=list)
    limit_pushed: bool = False
    rows: int = 0


class PlanExecutor:
    _ids = itertools.count(1)

    def __init__(self, catalog: Catalog, duck):
        self.catalog = catalog
        self.duck = duck

    def execute(self, plan: LogicalQueryPlan, now: datetime) -> Tuple[pd.DataFrame, str, List[SourceFetch]]:
        relations: Dict[str, str] = {}
        fetches: List[SourceFetch] = []
        registered: List[str] = []
        try:
            for source in plan.sources:
                src = self.catalog.sources[source]
                if src.relation:
                    relations[source] = src.relation
                    fetches.append(SourceFetch(source=source, scan=src.relation))
                    continue
                df, fetch = self._fetch(plan, source)
                tmp = f"_sem_{source}_{next(self._ids)}"
                self.duck.conn.register(tmp, df)
                registered.append(tmp)
                relations[source] = tmp
                fetches.append(fetch)

            sql = compile_plan(plan, self.catalog, relations, now)
            result = self.duck.conn.sql(sql).df()
        finally:
            for tmp in registered:
                try:
                    self.duck.conn.unregister(tmp)
                except Exception:
                    pass
        return result, sql, fetches

    def _fetch(self, plan: LogicalQueryPlan, source: str) -> Tuple[pd.DataFrame, SourceFetch]:
        src = self.catalog.sources[source]
        fn = self.duck.functions.get(src.table.lower())
        if fn is None:
            raise ExecutionError(f"source '{source}': table '{src.table}' isn't registered in DuckAPI")
        params = set(inspect.signature(fn).parameters)

        fetch = SourceFetch(source=source, scan=display_relation(self.catalog, source), kwargs=dict(src.args))
        for flt in plan.filters:
            s, fname = flt.field.split(".")
            if s != source:
                continue
            fdef = src.fields[fname]
            param = fdef.param or src.physical_column(fname)
            if flt.operator == "eq" and param in params and param not in fetch.kwargs:
                fetch.kwargs[param] = flt.value
                fetch.pushed_filters.append(flt.field)
            else:
                fetch.residual_filters.append(flt.field)
        if plan.time_range and plan.time_range.field.split(".")[0] == source:
            fetch.residual_filters.append(plan.time_range.field)

        # Capping rows at the source is only equivalent to the final LIMIT when
        # nothing downstream can drop, merge or reorder rows: one source, no
        # DISTINCT, every filter already applied server-side, and no ORDER BY
        # (the API's first N rows aren't the newest N).
        if (
            len(plan.sources) == 1 and not plan.distinct and not fetch.residual_filters
            and not default_order(plan, self.catalog) and "limit" in params
        ):
            fetch.kwargs["limit"] = plan.limit
            fetch.limit_pushed = True

        df = self.duck.fetch(src.table, **fetch.kwargs)
        fetch.rows = len(df)

        needed = {src.physical_column(f): src.fields[f].type for f in plan.source_fields(source)}
        missing = [c for c in needed if c not in df.columns]
        if missing:
            if not df.empty:
                raise ExecutionError(
                    f"source '{source}' ({src.table}) returned no column(s) {missing} — "
                    f"the catalog is out of sync with the data (got: {list(df.columns)})"
                )
            # No rows → nothing to infer columns from; use the catalog's schema.
            df = pd.DataFrame({c: pd.Series(dtype=_EMPTY_DTYPES[t]) for c, t in needed.items()})
        return df, fetch

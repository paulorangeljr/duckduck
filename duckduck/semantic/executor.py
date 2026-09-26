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

**Streaming** (``stream=True``, the default): when a source can't be capped
at the source (its LIMIT wasn't pushed — a filter stayed with DuckDB, a
join, an aggregate, an order) and its table has a streaming function
(``register_streaming_function`` / the registry's ``streaming_tables``),
its pages are read one at a time: each page goes through DuckDB with that
source's own conditions and only the columns the plan uses, and only the
rows that pass are kept — in a typed DuckDB temp table, never a whole
DataFrame of everything the API returned. A single-source plan without
DISTINCT, aggregate or ORDER BY stops paging once it has ``limit`` rows.
The final query then runs over that table as over any other.
"""

import inspect
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .. import progress
from ..logs import get_logger
from ..pushdown import Condition, blocker_of, map_conditions
from .catalog import Catalog
from .compiler import compile_plan, default_order, display_relation, quote_ident, source_conditions
from .plan import LogicalQueryPlan

logger = get_logger("semantic")

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
    #: Read page by page (``stream``): how many pages, how many rows the API returned in all
    #: (``rows`` is what was kept after this source's own filters).
    streamed: bool = False
    pages: int = 0
    rows_scanned: int = 0


#: A catalog field type → the DuckDB column type a streamed source's rows are kept as.
_DUCK_TYPES = {"string": "VARCHAR", "integer": "BIGINT", "float": "DOUBLE", "boolean": "BOOLEAN",
               "datetime": "TIMESTAMP", "date": "DATE"}


class PlanExecutor:
    _ids = itertools.count(1)

    def __init__(self, catalog: Catalog, duck, stream: bool = True):
        self.catalog = catalog
        self.duck = duck
        #: Read a source page by page when it can't be capped at the source (see the module docstring).
        self.stream = stream

    def execute(self, plan: LogicalQueryPlan, now: datetime) -> Tuple[pd.DataFrame, str, List[SourceFetch]]:
        relations: Dict[str, str] = {}
        fetches: List[SourceFetch] = []
        registered: List[str] = []
        tables: List[str] = []
        try:
            for source in plan.sources:
                src = self.catalog.sources[source]
                if src.relation:
                    relations[source] = src.relation
                    fetches.append(SourceFetch(source=source, scan=src.relation))
                    continue
                progress.step("fetching", f"Reading {source} ({src.table})")
                df, fetch, table = self._fetch(plan, source, now)
                progress.note_item("fetched", source, {"table": src.table, "rows": fetch.rows, "pages": fetch.pages,
                                                       "rows_scanned": fetch.rows_scanned,
                                                       "pushed": fetch.pushed_filters})
                if table is not None:  # streamed: the kept rows are already in a DuckDB table
                    tables.append(table)
                    relations[source] = table
                else:
                    tmp = f"_sem_{source}_{next(self._ids)}"
                    self.duck.conn.register(tmp, df)
                    registered.append(tmp)
                    relations[source] = tmp
                fetches.append(fetch)

            sql = compile_plan(plan, self.catalog, relations, now)
            progress.step("combining", "Filtering and combining the rows in DuckDB")
            result = self.duck.conn.sql(sql).df()
        finally:
            for tmp in registered:
                try:
                    self.duck.conn.unregister(tmp)
                except Exception:
                    pass
            for table in tables:
                try:
                    self.duck.conn.execute(f"DROP TABLE IF EXISTS {table}")
                except Exception:
                    pass
        return result, sql, fetches

    @staticmethod
    def _as_condition(column: str, flt) -> Optional[Condition]:
        """A plan filter as a push-down condition, or None if no connector parameter can express it."""
        if flt.operator == "eq":
            return Condition(column, "eq", flt.value)
        if flt.operator in ("gt", "gte", "lt", "lte"):
            return Condition(column, flt.operator, flt.value)
        if flt.operator in ("contains", "starts_with", "ends_with"):
            text = str(flt.value)
            if "%" in text or "_" in text or "\\" in text:
                return None  # would need LIKE escaping no connector param accepts
            pattern = {"contains": f"%{text}%", "starts_with": f"{text}%", "ends_with": f"%{text}"}[flt.operator]
            # the compiled SQL matches these case-insensitively → ILIKE
            return Condition(column, "ilike", pattern)
        return None  # neq / in: no parameter convention for them

    def _fetch(self, plan: LogicalQueryPlan, source: str,
               now: datetime) -> Tuple[Optional[pd.DataFrame], SourceFetch, Optional[str]]:
        src = self.catalog.sources[source]
        fn = self.duck.functions.get(src.table.lower())
        if fn is None:
            raise ExecutionError(f"source '{source}': table '{src.table}' isn't registered in DuckAPI")
        params = set(inspect.signature(fn).parameters)

        fetch = SourceFetch(source=source, scan=display_relation(self.catalog, source), kwargs=dict(src.args))
        conditions: List[Tuple[str, Condition]] = []
        for flt in plan.filters:
            s, fname = flt.field.split(".")
            if s != source:
                continue
            cond = self._as_condition(src.fields[fname].param or src.physical_column(fname), flt)
            if cond is None:
                fetch.residual_filters.append(flt.field)
            else:
                conditions.append((flt.field, cond))
        tr = plan.time_range
        if tr and tr.field.split(".")[0] == source:
            column = src.physical_column(tr.field.split(".")[1])
            start = now - timedelta(hours=tr.last_hours) if tr.last_hours is not None else tr.start
            if start is not None:
                conditions.append((tr.field, Condition(column, "gte", start.isoformat(sep=" "))))
            if tr.end is not None:
                conditions.append((tr.field, Condition(column, "lt", tr.end.isoformat(sep=" "))))

        # Same operator→parameter mapping as DuckAPI.sql() (duckduck.pushdown):
        # eq → col, LIKE → col_like/col_ilike, comparisons → col_gt..., or all
        # of them to a `where` param. Structural args always win.
        pushed, consumed = map_conditions(params - set(fetch.kwargs), [c for _, c in conditions], blocker_of(fn))
        fetch.kwargs.update(pushed)
        # A field is "pushed" only if every one of its conditions was
        # (a time range carries two: >= start and < end).
        for ref in dict.fromkeys(ref for ref, _ in conditions):
            all_pushed = all(c in consumed for r, c in conditions if r == ref)
            (fetch.pushed_filters if all_pushed else fetch.residual_filters).append(ref)

        # Capping rows at the source is only equivalent to the final LIMIT when
        # nothing downstream can drop, merge or reorder rows: one source, no
        # DISTINCT, no count (it needs every row), every filter already
        # applied server-side, and no ORDER BY (the API's first N rows aren't
        # the newest N).
        if (
            len(plan.sources) == 1 and not plan.distinct and not plan.aggregate and not fetch.residual_filters
            and not default_order(plan, self.catalog) and "limit" in params
        ):
            fetch.kwargs["limit"] = plan.limit
            fetch.limit_pushed = True

        iter_fn = self.duck.streaming_function(src.table) if self.stream else None
        if iter_fn is not None and not fetch.limit_pushed:
            return None, fetch, self._stream(plan, source, iter_fn, conditions, fetch, now)

        logger.info(
            "▶ %s ← %s(%s) · pushed: %s · DuckDB: %s · limit %s",
            source, src.table, ", ".join(f"{k}={v!r}" for k, v in fetch.kwargs.items()),
            ", ".join(fetch.pushed_filters) or "—", ", ".join(fetch.residual_filters) or "—",
            "pushed" if fetch.limit_pushed else "not pushed",
        )
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
        return df, fetch, None

    def _stream(self, plan: LogicalQueryPlan, source: str, iter_fn: Any, conditions: List[Tuple[str, Condition]],
                fetch: SourceFetch, now: datetime) -> str:
        """
        The source page by page: its conditions pushed to the streaming
        function where it takes them, each page filtered by DuckDB with the
        source's own conditions, the kept rows (only the plan's columns,
        typed as the catalog says) appended to a temp table — its name.
        """
        src = self.catalog.sources[source]
        # the generator's own signature decides what reaches the API (it takes no limit)
        fetch.kwargs = dict(src.args)
        fetch.pushed_filters, fetch.limit_pushed, fetch.streamed = [], False, True
        residual = list(fetch.residual_filters)
        fetch.residual_filters = []
        params = set(inspect.signature(iter_fn).parameters)
        pushed, consumed = map_conditions(params - set(fetch.kwargs), [c for _, c in conditions], blocker_of(iter_fn))
        fetch.kwargs.update(pushed)
        for ref in dict.fromkeys(ref for ref, _ in conditions):
            all_pushed = all(c in consumed for r, c in conditions if r == ref)
            (fetch.pushed_filters if all_pushed else fetch.residual_filters).append(ref)
        fetch.residual_filters += [r for r in residual if r not in fetch.residual_filters + fetch.pushed_filters]

        needed = {src.physical_column(f): src.fields[f].type for f in plan.source_fields(source)}
        where = source_conditions(plan, self.catalog, source, now)
        enough = (plan.limit if len(plan.sources) == 1 and not plan.distinct and not plan.aggregate
                  and not default_order(plan, self.catalog) else None)
        table = f"_sem_{source}_{next(self._ids)}_stream"
        conn = self.duck.conn
        conn.execute(f"CREATE TEMP TABLE {table} ("
                     + ", ".join(f"{quote_ident(c)} {_DUCK_TYPES.get(t, 'VARCHAR')}" for c, t in needed.items()) + ")")
        logger.info(
            "▶ %s ← %s(%s), page by page · pushed: %s · DuckDB, per page: %s%s",
            source, src.table, ", ".join(f"{k}={v!r}" for k, v in fetch.kwargs.items()),
            ", ".join(fetch.pushed_filters) or "—", ", ".join(fetch.residual_filters) or "—",
            f" · stops at {enough} rows" if enough else "",
        )
        seen = set()
        try:
            self._read_pages(src, fetch, table, needed, where, enough, seen)
        except BaseException:  # cancelled (or failed) mid-way: the half-filled table goes
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            raise
        missing = [c for c in needed if c not in seen]
        if missing and fetch.rows_scanned:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            raise ExecutionError(
                f"source '{source}' ({src.table}) returned no column(s) {missing} — "
                f"the catalog is out of sync with the data (got: {sorted(seen)})"
            )
        fetch.rows = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        logger.info("  %s: kept %s of %s rows from %s page(s)", source, f"{fetch.rows:,}",
                    f"{fetch.rows_scanned:,}", fetch.pages)
        return table

    def _read_pages(self, src: Any, fetch: SourceFetch, table: str, needed: Dict[str, str], where: List[str],
                    enough: Optional[int], seen: set) -> None:
        """Appends each page's kept rows to ``table``; a pause or cancel takes effect between pages."""
        conn = self.duck.conn
        kept = 0
        for page in self.duck.fetch_pages(src.table, **fetch.kwargs):
            fetch.pages += 1
            fetch.rows_scanned += len(page)
            seen.update(page.columns)
            view = f"_sem_page_{next(self._ids)}"
            conn.register(view, page)
            try:
                cols = ", ".join(f"TRY_CAST({quote_ident(c)} AS {_DUCK_TYPES.get(t, 'VARCHAR')})" for c, t in needed.items())
                absent = "".join(f", NULL AS {quote_ident(c)}" for c in needed if c not in page.columns)
                conn.execute(f"INSERT INTO {table} SELECT {cols} FROM (SELECT *{absent} FROM {view}) AS page"
                             + (" WHERE " + " AND ".join(where) if where else ""))
            finally:
                conn.unregister(view)
            kept = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            progress.update(f"Reading {src.table} page by page: page {fetch.pages} · "
                            f"{fetch.rows_scanned:,} rows read · {kept:,} kept")
            if enough is not None and kept >= enough:
                break  # enough rows for the answer: no more pages asked
            progress.checkpoint()

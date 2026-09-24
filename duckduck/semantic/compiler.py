"""
Plan → SQL (DuckDB dialect).

Each source becomes a CTE that projects only the fields the plan touches
(physical column → logical field name) and applies that source's own
filters — so predicates and projections sit right on top of each source
scan, where DuckDB pushes them into native scans (Parquet/Delta/attached
DBs) and where ``executor.py`` pushes them into API calls. The outer
SELECT only joins, deduplicates and limits.

Every identifier comes from the validated catalog (pattern-constrained,
double-quoted anyway); every value is rendered as an escaped literal —
nothing from the question is ever spliced in raw.
"""

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping

from .catalog import Catalog
from .plan import Filter, LogicalQueryPlan


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def render_literal(value: Any, field_type: str = "string") -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"non-finite number {value!r} can't be a SQL literal")
        return repr(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    text = "'" + str(value).replace("'", "''") + "'"
    if field_type == "datetime":
        return f"CAST({text} AS TIMESTAMP)"
    if field_type == "date":
        return f"CAST({text} AS DATE)"
    return text


def _column_expr(column: str, field_type: str) -> str:
    col = quote_ident(column)
    if field_type == "datetime":
        return f"CAST({col} AS TIMESTAMP)"
    if field_type == "date":
        return f"CAST({col} AS DATE)"
    return col


def render_condition(column: str, field_type: str, flt: Filter) -> str:
    op = flt.operator
    if op in ("contains", "starts_with", "ends_with"):
        return f"{op}(lower(CAST({quote_ident(column)} AS VARCHAR)), lower({render_literal(flt.value)}))"
    col = _column_expr(column, field_type)
    if op == "in":
        return f"{col} IN ({', '.join(render_literal(v, field_type) for v in flt.value)})"
    symbol = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
    return f"{col} {symbol} {render_literal(flt.value, field_type)}"


def source_conditions(plan: LogicalQueryPlan, catalog: Catalog, source: str, now: datetime) -> List[str]:
    """WHERE conditions (physical columns) that apply to ``source`` alone."""
    src = catalog.sources[source]
    conds = []
    for flt in plan.filters:
        s, fname = flt.field.split(".")
        if s == source:
            conds.append(render_condition(src.physical_column(fname), src.fields[fname].type, flt))
    tr = plan.time_range
    if tr and tr.field.split(".")[0] == source:
        fname = tr.field.split(".")[1]
        col = _column_expr(src.physical_column(fname), "datetime")
        start = now - timedelta(hours=tr.last_hours) if tr.last_hours is not None else tr.start
        if start is not None:
            conds.append(f"{col} >= {render_literal(start)}")
        if tr.end is not None:
            conds.append(f"{col} < {render_literal(tr.end)}")
    return conds


def compile_plan(
    plan: LogicalQueryPlan,
    catalog: Catalog,
    relations: Mapping[str, str],
    now: datetime,
) -> str:
    """
    ``relations`` maps each source to what its CTE scans — a registered
    temp table name, a native DuckDB relation, or a display-only
    ``table(args)`` string.
    """
    ctes = []
    for source in plan.sources:
        src = catalog.sources[source]
        cols = []
        for fname in plan.source_fields(source):
            physical = src.physical_column(fname)
            cols.append(quote_ident(physical) if physical == fname else f"{quote_ident(physical)} AS {quote_ident(fname)}")
        body = f"SELECT {', '.join(cols)}\n    FROM {relations[source]}"
        conds = source_conditions(plan, catalog, source, now)
        if conds:
            body += "\n    WHERE " + "\n      AND ".join(conds)
        ctes.append(f"{quote_ident(source)} AS (\n    {body}\n)")

    field_names = [ref.split(".")[1] for ref in plan.select]
    select_items = []
    for ref, fname in zip(plan.select, field_names):
        s, f = ref.split(".")
        alias = f if field_names.count(f) == 1 else f"{s}_{f}"
        expr = f"{quote_ident(s)}.{quote_ident(f)}"
        select_items.append(expr if alias == f else f"{expr} AS {quote_ident(alias)}")

    joined = {plan.sources[0]}
    from_clause = quote_ident(plan.sources[0])
    for join in plan.joins:
        ls, lf = join.left.split(".")
        rs, rf = join.right.split(".")
        new = rs if ls in joined else ls
        joined.add(new)
        from_clause += (
            f"\n{join.type.upper()} JOIN {quote_ident(new)}"
            f" ON {quote_ident(ls)}.{quote_ident(lf)} = {quote_ident(rs)}.{quote_ident(rf)}"
        )

    sql = "WITH " + ",\n".join(ctes)
    sql += f"\nSELECT {'DISTINCT ' if plan.distinct else ''}{', '.join(select_items)}"
    sql += f"\nFROM {from_clause}"
    order = default_order(plan, catalog)
    if order:
        sql += f"\nORDER BY {order}"
    sql += f"\nLIMIT {int(plan.limit)}"
    return sql


def default_order(plan: LogicalQueryPlan, catalog: Catalog) -> str:
    """Deterministic output: distinct lists sorted; row lists newest first."""
    if plan.distinct:
        return ", ".join(str(i + 1) for i in range(len(plan.select)))
    primary = plan.sources[0]
    time_field = catalog.sources[primary].resolved_time_field
    if time_field and f"{primary}.{time_field}" in plan.select:
        return f"CAST({quote_ident(primary)}.{quote_ident(time_field)} AS TIMESTAMP) DESC"
    return ""


def display_relation(catalog: Catalog, source: str) -> str:
    """How a source's scan reads in human-facing SQL: ``table(arg=...)`` or the raw relation."""
    src = catalog.sources[source]
    if src.relation:
        return src.relation
    if src.args:
        args = ", ".join(f"{k}={render_literal(v)}" for k, v in src.args.items())
        return f"{src.table}({args})"
    return src.table


def display_sql(plan: LogicalQueryPlan, catalog: Catalog, now: datetime) -> str:
    relations: Dict[str, str] = {s: display_relation(catalog, s) for s in plan.sources}
    return compile_plan(plan, catalog, relations, now)

"""
DuckAPI — query external APIs with SQL.

Supports push-down of SQL predicates to registered functions:

- LIMIT  → passed as ``limit=N`` if the function accepts that parameter
- WHERE  → simple conditions (=, LIKE, >, <, >=, <=) are passed as
           kwargs if the function accepts that parameter name

Registered functions receive the predicates they know about and ignore
the rest; DuckDB applies the remainder normally on top of the returned
DataFrame.
"""

import ast
import contextvars
import inspect
import json
import logging
import os
import re
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

import duckdb
import pandas as pd
import sqlglot
import sqlglot.expressions as exp

from .logs import get_logger, set_verbose, short, verbose_from_env
from . import slicing
from .pushdown import (Condition, assign_conditions, blocker_of, conditions_to_sql, map_conditions, parse_like,
                       where_ops_of)

logger = get_logger("core")


# ---------------------------------------------------------------------------
# Push-down context
# ---------------------------------------------------------------------------


@dataclass
class PushDownContext:
    """
    Predicates extracted from the SQL that can be sent to the API.

    Attributes
    ----------
    limit : int | None
        The query's LIMIT value, if present.
    filters : dict[str, Any]
        Simple WHERE conditions: ``{column_name: value}``.
        Operators supported for push-down: ``=``, ``LIKE``,
        ``>``, ``<``, ``>=``, ``<=``.

    Examples
    --------
    For the query::

        SELECT * FROM assets(hostname='web') WHERE severity = 'critical' LIMIT 50

    the context will be::

        PushDownContext(limit=50, filters={"severity": "critical"})

    Note that ``hostname='web'`` was already passed explicitly in the
    function call and does **not** show up in ``filters``.
    """

    limit: Optional[int] = None
    #: Equality conditions only, ``{column: value}`` (kept for callers that
    #: read it directly; ``conditions`` is the full, operator-aware list).
    filters: Dict[str, Any] = field(default_factory=dict)
    #: Every simple condition extracted from the WHERE clause.
    conditions: List[Condition] = field(default_factory=list)
    #: False when the WHERE clause has anything that couldn't be extracted
    #: (OR, NOT, IN, functions...) — some filtering is left to DuckDB alone.
    complete: bool = True
    #: Whether the query's shape allows LIMIT to reach the source at all:
    #: one relation, no JOIN / GROUP BY / DISTINCT / ORDER BY / aggregate /
    #: window / subquery / set operation. Otherwise capping rows at the
    #: source would change the answer.
    limit_safe: bool = True
    #: Why ``limit_safe`` is False (e.g. ``"ORDER BY"``), for verbose output.
    limit_blocker: Optional[str] = None


# ---------------------------------------------------------------------------
# SQL extraction helpers
# ---------------------------------------------------------------------------

_CONDITION_OPS = {
    exp.EQ: "eq", exp.Like: "like", exp.ILike: "ilike",
    exp.GT: "gt", exp.GTE: "gte", exp.LT: "lt", exp.LTE: "lte",
}


def _literal_value(node: exp.Expression) -> Any:
    """Converts a sqlglot Literal node into a Python value."""
    if isinstance(node, exp.Literal):
        if node.is_number:
            try:
                return int(node.this)
            except ValueError:
                return float(node.this)
        return node.this  # unquoted string
    if isinstance(node, exp.Boolean):
        return node.this
    # TIMESTAMP '2026-09-30 11:00:00' / DATE '…' / CAST('…' AS TIMESTAMP): the source gets the text,
    # DuckDB keeps the typed comparison (it re-applies the whole WHERE)
    if (isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal) and node.this.is_string
            and node.to is not None and node.to.is_type(*exp.DataType.TEMPORAL_TYPES)):
        return node.this.this
    return None


def _extract_filters(node: exp.Expression, ctx: "PushDownContext") -> None:
    """
    Walks the WHERE tree and extracts simple conditions into ``ctx``.

    Supports: ``col (= | LIKE | ILIKE | > | < | >= | <=) literal``, ANDed.
    Anything else (OR, NOT, IN, functions, column-to-column...) is left
    to DuckDB and marks the context incomplete.
    """
    if node is None:
        return

    op = _CONDITION_OPS.get(type(node))
    if op is not None:
        left, right = node.left, node.right
        val = _literal_value(right) if isinstance(left, exp.Column) else None
        if val is None:
            ctx.complete = False
            return
        column = left.name.lower()
        table = left.table.lower() if left.table else None
        ctx.conditions.append(Condition(column=column, op=op, value=val, table=table))
        if op == "eq":
            ctx.filters[column] = val
        return

    if isinstance(node, exp.Between) and isinstance(node.this, exp.Column):
        # col BETWEEN a AND b ≡ col >= a AND col <= b
        low, high = _literal_value(node.args.get("low")), _literal_value(node.args.get("high"))
        if low is None or high is None or node.args.get("symmetric"):
            ctx.complete = False
            return
        column, table = node.this.name.lower(), (node.this.table.lower() if node.this.table else None)
        ctx.conditions.append(Condition(column=column, op="gte", value=low, table=table))
        ctx.conditions.append(Condition(column=column, op="lte", value=high, table=table))
        return

    if isinstance(node, exp.Paren):  # (a = 1 AND b = 2): the same conditions
        _extract_filters(node.this, ctx)
        return

    if isinstance(node, (exp.And, exp.Where)):
        for child in node.args.values():
            if isinstance(child, exp.Expression):
                _extract_filters(child, ctx)
        return

    ctx.complete = False


def _sources_of(select: exp.Select) -> List[exp.Expression]:
    """The FROM and JOIN sources of one SELECT (tables, calls, subqueries)."""
    frm = select.args.get("from_") or select.args.get("from")
    nodes = [frm.this] if frm is not None else []
    return nodes + [j.this for j in select.args.get("joins") or []]


def _source_label(node: exp.Expression) -> Optional[str]:
    """How a condition names a table source — its alias, else its (function's) name; None for a subquery."""
    if not isinstance(node, exp.Table):
        return None
    if node.alias:
        return node.alias.lower()
    name = node.name or (node.this.name if isinstance(node.this, exp.Func) else "")
    return name.lower() or None


def _passes_through(node: exp.Expression) -> Optional[Tuple[exp.Select, Callable[[str], Optional[str]]]]:
    """
    A derived table over one source whose columns are the source's own —
    ``SELECT * FROM t``, ``SELECT a, b FROM t``, ``SELECT a AS x FROM t``,
    ``SELECT *, a / 2 AS y FROM t`` (KQL's project / extend) — with the column
    each of its names is in the source (``x`` → ``a``; ``y`` → None: computed).
    None for anything else (aggregates, DISTINCT, LIMIT, ORDER…: the rows aren't the source's).
    """
    inner = node.this if isinstance(node, exp.Subquery) else None
    if not isinstance(inner, exp.Select) or len(_sources_of(inner)) != 1:
        return None
    for arg in ("group", "distinct", "having", "limit", "offset", "qualify", "order", "joins", "with"):
        if inner.args.get(arg):
            return None
    star, plain, computed = False, {}, set()
    for e in inner.expressions:
        if isinstance(e, exp.Star):
            if any(e.args.values()):  # SELECT * EXCLUDE / REPLACE / RENAME: not the source's columns
                return None
            star = True
        elif isinstance(e, exp.Column) and isinstance(e.this, exp.Identifier):
            plain[e.name.lower()] = e.name.lower()
        elif isinstance(e, exp.Alias) and isinstance(e.this, exp.Column) and isinstance(e.this.this, exp.Identifier):
            plain[e.alias.lower()] = e.this.name.lower()
        elif isinstance(e, exp.Alias):
            computed.add(e.alias.lower())
        else:
            return None
    if not star and not plain:
        return None

    def source_column(column: str) -> Optional[str]:
        if column in plain:
            return plain[column]
        return column if star and column not in computed else None

    return inner, source_column


def _extract_scoped(selects: List[exp.Select], ctx: "PushDownContext", arg_qualifiers: Tuple[str, ...]) -> None:
    """
    Conditions of a query with subqueries / CTEs / set operations, each
    scoped to the table it filters: every one comes out qualified with that
    table's alias or name, so it reaches that call only. Pushed only when
    it can't change the answer:

    - a SELECT with one source that is a table (or a ``SELECT *`` pass-through
      down to one) → its conditions go to that table;
    - a qualified condition (``L.x``) → to the table with that alias in its SELECT;
    - a condition on a derived table's computed column, an unqualified one
      over several sources, or on a table name used twice without an alias →
      DuckDB only (and the WHERE counts as incomplete).

    ``arg.x`` conditions keep their qualifier (they fill arguments, never filter).
    """
    labels = [lbl for s in selects for src in _sources_of(s) if isinstance(src, exp.Table) and not src.alias
              for lbl in [_source_label(src)] if lbl]
    twice = {lbl for lbl in labels if labels.count(lbl) > 1}

    def target(node: exp.Expression, column: str, depth: int = 0) -> Optional[Tuple[str, str]]:
        """(the table's label, the column's name there) a condition on ``column`` of ``node`` reaches, or None."""
        if isinstance(node, exp.Table):
            label = _source_label(node)
            return None if label is None or (not node.alias and label in twice) else (label, column)
        through = _passes_through(node)
        if through is None or depth >= 20:
            return None
        inner, source_column = through
        mapped = source_column(column)
        return target(_sources_of(inner)[0], mapped, depth + 1) if mapped else None

    for select in selects:
        where = select.args.get("where")
        if where is None:
            continue
        found = PushDownContext()
        _extract_filters(where, found)
        if not found.complete:
            ctx.complete = False
        sources = _sources_of(select)
        by_label = {}
        for src in sources:
            lbl = (src.alias or "").lower() or _source_label(src)
            if lbl:
                by_label[lbl] = src
        for c in found.conditions:
            if c.table in arg_qualifiers:
                hit = (c.table, c.column)
            elif c.table:
                hit = target(by_label[c.table], c.column) if c.table in by_label else None
            else:
                hit = target(sources[0], c.column) if len(sources) == 1 else None
            if hit is None:
                ctx.complete = False
                continue
            table, column = hit
            ctx.conditions.append(Condition(column=column, op=c.op, value=c.value, table=table))
            if c.op == "eq":
                ctx.filters[column] = c.value


def _ago(seconds: float) -> str:
    """``12s ago`` / ``3 min ago`` / ``2 h ago``."""
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    return f"{seconds / 3600:.1f} h ago"


def _limit_blocker(parsed: exp.Expression) -> Optional[str]:
    """
    Why a source-side LIMIT could change the query's answer, or None when
    it can't (see PushDownContext.limit_safe).
    """
    if not isinstance(parsed, exp.Select):
        return "set operation (UNION/EXCEPT/INTERSECT)"
    for arg, reason in (
        ("joins", "JOIN"), ("group", "GROUP BY"), ("distinct", "DISTINCT"),
        ("order", "ORDER BY"), ("having", "HAVING"), ("with", "WITH/CTE"),
    ):
        if parsed.args.get(arg):
            return reason
    for node, reason in ((exp.AggFunc, "aggregate"), (exp.Window, "window function"), (exp.Subquery, "subquery")):
        if parsed.find(node):
            return reason
    return None


_OP_SQL = {"eq": "=", "like": "LIKE", "ilike": "ILIKE", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _describe_condition(c: Condition) -> str:
    column = f"{c.table}.{c.column}" if c.table else c.column
    if c.op == "in":
        values = list(c.value or ())
        shown = ", ".join(repr(v) for v in values[:3]) + (f", … ({len(values)} values)" if len(values) > 3 else "")
        return f"{column} IN ({shown})"
    return f"{column} {_OP_SQL.get(c.op, c.op)} {c.value!r}"


def _why_not_pushed(c: Condition, accepted: set) -> str:
    if c.op in ("like", "ilike"):
        has_param = f"{c.column}_ilike" in accepted or (c.op == "like" and f"{c.column}_like" in accepted)
        if has_param and parse_like(c.value) is None:
            return "pattern not translatable ('_' wildcard / inner '%'), DuckDB filters"
        if c.op == "ilike" and f"{c.column}_like" in accepted:
            return f"{c.column}_like is case-sensitive, ILIKE needs {c.column}_ilike; DuckDB filters"
        return f"no {c.column}_like/_ilike parameter, DuckDB filters"
    if c.op == "eq":
        return f"no '{c.column}' parameter, DuckDB filters"
    if c.op == "in":
        return f"no {c.column}_in parameter, and its where doesn't take IN — DuckDB joins"
    return f"no {c.column}_{c.op} parameter, DuckDB filters"


# ---------------------------------------------------------------------------
# Joins: one side's key values narrow the other side's read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JoinEdge:
    """``ON probe.probe_column = build.build_column``: once ``build`` is read, ``probe`` only needs rows whose
    ``probe_column`` is one of ``build``'s values — every other row of it can't be in the answer."""

    probe: str
    probe_column: str
    build: str
    build_column: str
    #: the join as written, for the log (``LEFT JOIN``)
    join: str = "JOIN"


def _table_ref(node: Any) -> Optional[str]:
    """How a table is referred to in the query (its alias, else its name or function name), lowercased."""
    if not isinstance(node, exp.Table):
        return None
    name = node.alias or node.name or (node.this.name if isinstance(node.this, exp.Anonymous) else "")
    return name.lower() or None


def _conjuncts(node: Optional[exp.Expression]) -> List[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _join_plan(parsed: Optional[exp.Expression]) -> Tuple[List[JoinEdge], List[Condition]]:
    """
    The joins between tables whose rows one side can narrow, and the ON
    conditions that filter one side only (``ON b.table_name = 'incident'``).

    Which side gets narrowed — the side whose unmatched rows the join
    drops anyway: the joined table in ``[INNER] JOIN`` / ``LEFT JOIN`` /
    ``SEMI`` / ``ANTI``, the table before it in ``RIGHT JOIN``; never in
    ``FULL`` / ``CROSS`` / ``ASOF`` / ``POSITIONAL``. Only equalities
    between two qualified columns (``b.id = a.b_id``) or ``USING`` with one
    table before it; a table referred to twice by the same name is left
    alone. ON constants go to the narrowed side (and, in an inner join,
    to either side).
    """
    edges: List[JoinEdge] = []
    constants: List[Condition] = []
    if parsed is None:
        return edges, constants
    counts: Dict[str, int] = {}
    for t in parsed.find_all(exp.Table):
        ref = _table_ref(t)
        if ref:
            counts[ref] = counts.get(ref, 0) + 1
    for select in parsed.find_all(exp.Select):
        frm = select.args.get("from_") or select.args.get("from")
        left = [_table_ref(frm.this)] if frm is not None and _table_ref(frm.this) else []
        for join in select.args.get("joins") or []:
            jref = _table_ref(join.this)
            side = (join.args.get("side") or "").upper()
            kind = (join.args.get("kind") or "").upper()
            if jref is None:
                continue
            written = " ".join(x for x in (side, kind, "JOIN") if x)
            if side == "FULL" or kind not in ("", "INNER", "OUTER", "SEMI", "ANTI") or counts.get(jref, 0) > 1:
                left.append(jref)
                continue
            narrow_joined = side != "RIGHT"
            pairs: List[Tuple[str, str, str, str]] = []  # (joined column, left ref, left column)
            using = join.args.get("using") or []
            if using and len(left) == 1:
                pairs += [(u.name.lower(), left[0], u.name.lower(), "") for u in using if isinstance(u, exp.Identifier)]
            for cond in _conjuncts(join.args.get("on")):
                if isinstance(cond, exp.EQ) and isinstance(cond.this, exp.Column) and \
                        isinstance(cond.expression, exp.Column):
                    a, b = cond.this, cond.expression
                    ta, tb = (a.table or "").lower(), (b.table or "").lower()
                    if ta == jref and tb in left:
                        pairs.append((a.name.lower(), tb, b.name.lower(), ""))
                    elif tb == jref and ta in left:
                        pairs.append((b.name.lower(), ta, a.name.lower(), ""))
                    continue
                op = _CONDITION_OPS.get(type(cond))
                if op and isinstance(cond.this, exp.Column) and not isinstance(cond.expression, exp.Column):
                    value = _literal_value(cond.expression)
                    table = (cond.this.table or "").lower()
                    if value is None or not table or counts.get(table, 0) > 1:
                        continue
                    inner = not side and kind in ("", "INNER")
                    # the side whose rows the ON only filters: the joined one (unless RIGHT), the earlier
                    # ones in an inner join or a RIGHT JOIN — never a side the join keeps whole
                    if (table == jref and narrow_joined) or (table in left and (inner or side == "RIGHT")):
                        constants.append(Condition(cond.this.name.lower(), op, value, table))
            for jcol, lref, lcol, _ in pairs:
                if counts.get(lref, 0) > 1:
                    continue
                if narrow_joined:
                    edges.append(JoinEdge(jref, jcol, lref, lcol, written))
                else:
                    edges.append(JoinEdge(lref, lcol, jref, jcol, written))
            left.append(jref)
    return edges, constants


def _mask_strings(query: str) -> str:
    """String literals blanked (same length), so a ``)`` inside one doesn't end a call."""
    return re.sub(r"'(?:[^']|'')*'", lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", query)


_UNUSABLE = object()


def _join_value(value: Any) -> Any:
    """A key value as a source compares it: 12.0 (pandas' ints next to a NULL) → 12, lists/structs unusable."""
    if hasattr(value, "item") and not isinstance(value, (list, dict)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (list, tuple, dict, set, bytes)):
        return _UNUSABLE
    return value


def _with_in(kwargs: Dict[str, Any], cond: Condition, param: str, chunk: List[Any]) -> Dict[str, Any]:
    out = dict(kwargs)
    if param in out:
        out[param] = list(chunk)
    else:
        piece = replace(cond, value=tuple(chunk))
        out["where"] = [piece if x is cond else x for x in out.get("where") or []]
    return out


def _without_in(kwargs: Dict[str, Any], cond: Condition, param: str) -> Dict[str, Any]:
    out = dict(kwargs)
    out.pop(param, None)
    if "where" in out:
        out["where"] = [x for x in out["where"] if x.column != cond.column or x.op != "in" or x.table != cond.table]
        if not out["where"]:
            del out["where"]
    return out


_PARSER: Optional[Any] = None
_PARSER_LOCK = threading.Lock()


def check_syntax(query: str) -> None:
    """DuckDB's own parser on the query — before any source is read, so a typo (a JOIN without ON) costs nothing."""
    global _PARSER
    with _PARSER_LOCK:
        if _PARSER is None:
            _PARSER = duckdb.connect()
        _PARSER.extract_statements(query)


# ---------------------------------------------------------------------------
# DuckAPI
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Nested values (ADX `dynamic`, JSON APIs): kept as STRUCT / LIST when every
# row has the same shape, JSON text when they don't
# ---------------------------------------------------------------------------

class _Mixed(Exception):
    pass


def _is_container(v: Any) -> bool:
    return isinstance(v, (dict, list, tuple)) or type(v).__name__ == "ndarray"


def _shape_of(v: Any) -> Any:
    """A value's shape: None (a null — fits anything), a scalar kind, ("list", elem) or ("dict", {key: shape})."""
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, dict):
        return ("dict", {str(k): _shape_of(x) for k, x in v.items()})
    if _is_container(v):
        elem = None
        for x in v:
            elem = _merge_shapes(elem, _shape_of(x))
        return ("list", elem)
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)) or type(v).__name__.startswith(("int", "float")):
        return "number"
    return "text" if isinstance(v, str) else type(v).__name__


def _merge_shapes(a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, tuple) and isinstance(b, tuple) and a[0] == b[0]:
        if a[0] == "list":
            return ("list", _merge_shapes(a[1], b[1]))
        if set(a[1]) != set(b[1]):
            raise _Mixed()
        return ("dict", {k: _merge_shapes(a[1][k], b[1][k]) for k in a[1]})
    if a != b:
        raise _Mixed()
    return a


def _json_text(v: Any) -> Any:
    if not _is_container(v):
        return v
    return json.dumps(v, default=lambda x: x.tolist() if hasattr(x, "tolist") else str(x), ensure_ascii=False)


def json_for_mixed_objects(df: pd.DataFrame) -> pd.DataFrame:
    """
    A column of nested values (dicts, lists) whose rows don't share one
    shape — different keys, a number here and a string there, a dict next to
    plain text: typical of ADX ``dynamic`` columns — becomes JSON text.
    DuckDB can't give it one type and would otherwise store Python's
    ``repr`` (``{'a': 1}``); JSON text still answers ``col->>'key'``.
    Columns whose rows all share one shape stay STRUCT / LIST.
    """
    changed = {}
    for col in df.columns[df.dtypes == object]:
        values = df[col].tolist()
        if not any(_is_container(v) for v in values):
            continue
        try:
            shape = None
            for v in values:
                shape = _merge_shapes(shape, _shape_of(v))
        except _Mixed:
            changed[col] = [_json_text(v) for v in values]
    if changed:
        df = df.copy()
        for col, values in changed.items():
            df[col] = pd.Series(values, index=df.index, dtype=object)
    return df

class DuckAPI:
    """
    SQL engine that lets you query Python functions as if they were tables.

    Basic usage
    -----------
    ::

        duck = DuckAPI()

        duck.register_api_function("assets", insightvm.assets)
        duck.register_api_function("vulns",  insightvm.vulnerabilities)

        # LIMIT push-down
        duck.sql("SELECT * FROM assets LIMIT 10").df()

        # Column filter via WHERE → automatic push-down
        duck.sql("SELECT * FROM assets WHERE hostname = 'web' LIMIT 50").df()

        # Simple WHERE push-down
        duck.sql("SELECT * FROM vulns WHERE severity = 'critical'").df()

        # Structural parameter (builds the URL) inline + column filter in WHERE
        duck.sql('''
            SELECT a.ip, v.title
            FROM assets AS a
            JOIN asset_vulns(asset_id=42) AS v ON true
            WHERE a.hostname = 'web' AND v.severity = 'critical'
        ''').df()

    Convention: inline vs WHERE
    ---------------------------
    The ``func(param=val)`` syntax should be used **only** for structural
    parameters that don't correspond to columns in the result (e.g.
    ``asset_id``, which determines the ``/assets/{id}/vulns`` endpoint).

    Regular column filters belong in the ``WHERE`` clause and are
    injected automatically as kwargs when the function accepts that
    parameter.

    DuckDB applies the filter on the result either way, guaranteeing
    correctness even when the API returns extra data.
    """

    def __init__(self, database: str = ":memory:", verbose=None, stream_pages: bool = True, cache: Any = None,
                 join_pushdown: bool = True, join_values_max: int = 1000, join_calls_max: int = 20,
                 parallel: bool = True):
        """
        Parameters
        ----------
        database : str
            DuckDB database (default in-memory).
        stream_pages : bool
            In ``sql()``: a table whose LIMIT can't go to the source and that
            has a streaming function is read page by page, each page filtered
            by the WHERE conditions that are its own and cut to the columns
            the query uses — the whole API result is never one DataFrame
            (see ``_materialize_pages``). ``False``: always fetch it whole.
        verbose : bool or str, optional
            ``True``/``"info"``: log, per query, how each table was called,
            which WHERE/LIMIT went to the source (and why not, when not),
            every HTTP request, pagination progress with elapsed/remaining
            time. ``"debug"`` adds request bodies and query parameters.
            Defaults to the ``DUCKDUCK_VERBOSE`` environment variable; off
            when neither is set. See ``duckduck.logs``.
        cache : SourceCache, bool or dict, optional
            Reuse what a source returned for the same call (function,
            arguments, push-down) for a while instead of calling it again
            — ``True`` (10 minutes), ``{"ttl": "10m", "max_rows": …}`` or a
            ``duckduck.cache.SourceCache``. Off by default. See
            ``duckduck.cache`` (``refreshing()`` reads again).
        join_pushdown : bool
            In a JOIN between two sources, read one side first and send its
            key values to the other (``ON b.id = a.b_id`` → ``b`` gets
            ``id IN (…)``, through ``where`` / ``id_in``, or one call per
            value on an ``id`` parameter). See ``_join_plan``.
        join_values_max : int
            More distinct key values than this: the other side isn't
            narrowed (read as without the join).
        join_calls_max : int
            At most this many calls to narrow one side — one per value on an
            ``id`` parameter, or the values split in chunks of the
            connector's ``IN_MAX``.
        parallel : bool
            Read an API's pages several at a time — as many as its table
            declares (``@spark_plan("partitioned", by="pages",
            max_parallel=N)``: what the API's rate limit allows), once the
            first page told the total — and a join's calls several at once.
            ``False``: one request after another.
        """
        if verbose is None:
            verbose = verbose_from_env()
        if verbose is not None:
            set_verbose(verbose)
        self.conn = duckdb.connect(database)
        self.functions: Dict[str, Any] = {}
        #: Registered table name (lowercase) → the auto_register service that registered it.
        self.service_of: Dict[str, str] = {}
        #: ``auto_register`` service → the prefix of its tables ("" = bare names): ``service.table`` addresses.
        self.service_prefix: Dict[str, str] = {}
        #: ``auto_register(on_error="warn")`` services that failed: name → {error, prefix, connector}.
        self.failed_services: Dict[str, Dict[str, str]] = {}
        self._streaming_functions: Dict[str, Any] = {}
        #: Saved tables (``duckduck.views``): name → definition ({table, args} or {sql}).
        self.views: Dict[str, Dict[str, Any]] = {}
        #: registered table → the saved table it is (``s3_data_a_b`` → ``s3_data.a.b``)
        self.view_key: Dict[str, str] = {}
        #: Saved tables that couldn't be registered (``on_error="warn"``): name → why.
        self.failed_views: Dict[str, str] = {}
        self._config_views: Optional[Dict[str, Any]] = None
        #: Every service ``auto_register`` was given (name → config as written) — ``duckduck.services`` lists them.
        self.configured_services: Dict[str, Dict[str, Any]] = {}
        self._config_system_tables: Optional[bool] = None
        self._table_counter = 0
        self.stream_pages = stream_pages
        self.join_pushdown = join_pushdown
        self.join_values_max = int(join_values_max)
        self.join_calls_max = int(join_calls_max)
        self.parallel = bool(parallel)
        from .cache import SourceCache

        #: Called ``(table, [(column, DuckDB type), ...])`` whenever a read gives a table's full set of columns
        #: (the PostgreSQL server's catalog learns them this way). None = nobody listens.
        self.column_listener: Optional[Callable[[str, List[Tuple[str, str]]], None]] = None
        #: What sources returned, reused within its ttl (None = off). See ``duckduck.cache``.
        self.cache: Optional[SourceCache] = (cache if isinstance(cache, SourceCache) or cache is None
                                             else SourceCache.from_config(cache))

    # ------------------------------------------------------------------
    # Function registration
    # ------------------------------------------------------------------

    def register_api_function(self, name: str, fetch_function) -> None:
        """
        Registers a Python function as a SQL "table".

        Parameters
        ----------
        name : str
            Table name in SQL (case-insensitive).
        fetch_function : callable
            Function that returns list[dict], dict, or pd.DataFrame.
            Parameters with the same names as result columns / ``limit``
            get automatic push-down.
        """
        self.functions[name.lower()] = fetch_function

    def register_streaming_function(self, name: str, iter_function) -> None:
        """
        Registers a generator function for use with ``stream()``.

        Parameters
        ----------
        name : str
            Same name used in ``register_api_function``.
        iter_function : callable
            Generator that accepts the same filter kwargs as the regular
            function and ``yield``s one ``pd.DataFrame`` per page.
            Doesn't need to accept ``limit`` — stream iterates every page.
        """
        self._streaming_functions[name.lower()] = iter_function

    # ------------------------------------------------------------------
    # Saved tables (duckduck.views)
    # ------------------------------------------------------------------

    def register_view(self, name: str, definition: Dict[str, Any], replace: bool = False) -> Dict[str, Any]:
        """
        Registers a saved table: ``{"table": "sn_table", "args": {"table_name": "incident"}}``
        (a table function with its arguments fixed — push-down unchanged) or
        ``{"sql": "SELECT ..."}`` (a read query, run each time). See ``duckduck.views``.
        """
        from . import views

        return views.register(self, name, definition, replace=replace)

    def unregister_view(self, name: str) -> None:
        from . import views

        views.unregister(self, name)

    def resolve_addresses(self, query: str) -> str:
        """``FROM service.table`` / ``service.database.table`` rewritten into the table or call it names (``duckduck.addresses``)."""
        from . import addresses

        return addresses.resolve_addresses(self, query)

    def address_of(self, table: str, args: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """``nvd_cves`` → ``nvd.cves``; ``s3_data_table`` + {database, table_name} → ``s3_data.security.proxy_logs``."""
        from . import addresses

        return addresses.address_of(self, table, args)

    def view_from_sql(self, sql: str) -> Dict[str, Any]:
        """The saved-table definition a query stands for (bound when it's just a table function's arguments)."""
        from . import views

        return views.view_from_sql(self, sql)

    # ------------------------------------------------------------------
    # Auto-registration of known wrappers (SharePoint, InsightVM, ...)
    # ------------------------------------------------------------------

    #: Default JSON config file name, looked up in the current directory
    #: when auto_register() is called with no ``services`` and no
    #: ``DUCKDUCK_CONFIG`` environment variable is set.
    DEFAULT_CONFIG_PATH = "duckduck.json"

    #: Prefix marking a field in an ``authentication`` block as a
    #: reference into the fetched secret, e.g. ``"$secret.client_secret"``.
    _SECRET_REF_PREFIX = "$secret."

    def auto_register(
        self,
        services: Optional[Dict[str, Dict[str, Any]]] = None,
        secrets: Optional[Dict[str, Any]] = None,
        config_path: Optional[str] = None,
        on_error: Optional[str] = None,
        views: Optional[Dict[str, Any]] = None,
        system_tables: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Instantiates and registers known API wrappers automatically
        (see ``duckduck.registry.SERVICE_REGISTRY``), without having to
        call ``register_api_function`` by hand for every method.

        Can be called as just ``duck.auto_register()``: when ``services``
        is omitted, the config is loaded from a JSON file instead (see
        "Loading from a JSON file" below).

        Parameters
        ----------
        services : dict, optional
            ``{name: config}``. ``name`` becomes the prefix of the
            registered tables (``{name}_{table}``) — allowing multiple
            instances of the same wrapper (e.g. ``insightvm_prod`` and
            ``insightvm_dev``).

            ``config`` accepts:

            - ``connector`` : str, optional
                Key in ``SERVICE_REGISTRY`` (``"sharepoint"``,
                ``"insightvm"``) — which wrapper this is. Default:
                ``name`` itself.
            - ``authentication`` : dict, required
                How to build the credentials dict passed to the
                connector's ``from_secret``. Always has a ``type``:

                - ``"local"``: every other field in the block is used
                  exactly as written — hardcoded, no secret store
                  involved. E.g. ``{"type": "local", "username": "a",
                  "password": "b"}``.
                - ``"aws"``: fetches a JSON secret from AWS Secrets
                  Manager. Requires ``secret_id`` (plus optional
                  ``region_name``). The secret's keys become the base
                  credentials; any other field in the block overrides or
                  adds to that, either as a literal value or, written as
                  ``"$secret.<key>"``, pulled from that key in the fetched
                  secret instead of being duplicated by hand.
                - ``"azure"``: same idea via Azure Key Vault. Requires
                  ``secret_id`` (the secret's name) and ``vault_url``.
                  The secret's value must be a JSON object, same shape as
                  the AWS case.
            - any other keys
                Extra kwargs passed through to the connector's
                constructor (e.g. ``hostname``, ``site_path``,
                ``default_page_size``) — anything that isn't a
                credential.

            When omitted, loaded from a JSON file — see below.
        secrets : dict, optional
            Advanced override: ``{"aws": <SecretsManager instance>,
            "azure": <AzureKeyVaultSecrets instance>}``. When a service's
            ``authentication.type`` has a matching entry here, that
            instance is used as-is instead of building one from
            ``region_name``/``vault_url`` — handy for tests, or to reuse
            one client across many ``auto_register()`` calls. Backends
            not overridden this way are still built and cached
            automatically per distinct ``region_name``/``vault_url``.
        config_path : str, optional
            Path to the JSON config file, used only when ``services`` is
            omitted. Defaults to the ``DUCKDUCK_CONFIG`` environment
            variable, or ``"duckduck.json"`` in the current directory.
        on_error : {"raise", "warn"}, optional
            What to do when a single service fails to initialize (bad
            credentials, a missing optional dependency, an unreachable
            host, a config mistake specific to that service, ...):

            - ``"raise"`` (the default): the exception propagates and
              ``auto_register()`` stops immediately — nothing gets
              registered from that call.
            - ``"warn"``: the exception is caught, turned into a
              ``RuntimeWarning`` naming the service and what went wrong
              (always displayed, even on a repeat of the exact same
              failure — Python's own default filter would otherwise
              silently show it only once per process), and that service
              is skipped — every other service is still registered
              normally, so one dead connector doesn't take down the
              rest. Check the return value's keys (or
              ``list_tables()``) to see what actually made it.

            When ``services`` is loaded from a JSON file, a top-level
            ``"on_error"`` key in that file is used as the default —
            still overridden by explicitly passing this parameter.
        views : dict, optional
            Saved tables to register after the services (``{name:
            {"table": ..., "args": {...}} | {"sql": ...}}`` — see
            ``duckduck.views``); from a JSON file, its top-level ``views``.
            ``on_error`` applies to them too.
        system_tables : bool, optional
            Registers ``duckduck.services`` / ``tables`` / ``saved_tables`` /
            ``settings`` / ``pipeline_runs`` (``duckduck.system``) after the
            services — default on; a JSON file's top-level
            ``"system_tables": false`` turns them off.

        Returns
        -------
        dict
            ``{name: instance}`` — to access wrapper methods that didn't
            become a table (e.g. ``instances["sharepoint"].site_by_path``).
            Only successfully-initialized services are present; with
            ``on_error="warn"``, that may be a subset of ``services``.

        Loading from a JSON file
        -------------------------
        ``duck.auto_register()`` with no arguments reads a JSON file
        shaped like::

            {
                "services": {
                    "sharepoint": {
                        "connector": "sharepoint",
                        "hostname": "company.sharepoint.com",
                        "site_path": "/teams/myteam",
                        "authentication": {
                            "type": "aws",
                            "region_name": "us-east-1",
                            "secret_id": "prod/sharepoint"
                        }
                    },
                    "insightvm_dev": {
                        "connector": "insightvm",
                        "authentication": {
                            "type": "local",
                            "host": "dev.local",
                            "username": "a",
                            "password": "b"
                        }
                    }
                }
            }

        The file is looked up, in order: ``config_path`` argument →
        ``DUCKDUCK_CONFIG`` environment variable → ``duckduck.json`` in
        the current directory. See ``duckduck.example.json`` in the repo
        root for a fuller example covering all three ``authentication``
        types.

        Examples
        --------
        Inline, from Python code, pulling from AWS Secrets Manager::

            from duckduck import DuckAPI

            duck = DuckAPI()
            instances = duck.auto_register({
                "sharepoint": {
                    "connector": "sharepoint",
                    "hostname": "company.sharepoint.com",
                    "site_path": "/teams/myteam",
                    "authentication": {
                        "type": "aws",
                        "region_name": "us-east-1",
                        "secret_id": "prod/sharepoint/duckduck",
                    },
                },
                "insightvm": {
                    "authentication": {  # local — fully hardcoded, offline
                        "type": "local",
                        "host": "console.local",
                        "username": "a",
                        "password": "b",
                    },
                },
            })

            duck.sql("SELECT * FROM sharepoint_list_items WHERE list_name = 'Tasks'")
            duck.sql("SELECT * FROM insightvm_assets WHERE hostname = 'web-prod'")

        From a JSON file (``duckduck.json`` in the current directory, or
        ``$DUCKDUCK_CONFIG``)::

            duck = DuckAPI()
            duck.auto_register()
        """
        from .registry import SERVICE_REGISTRY

        file_on_error = None
        base_dir = None  # relative paths in a JSON config resolve against its directory
        if services is None:
            services, file_on_error = self._load_auto_register_config(config_path)
            base_dir = self._config_base_dir

        if on_error is None:
            on_error = file_on_error or "raise"
        if on_error not in ("raise", "warn"):
            raise ValueError(f"on_error must be 'raise' or 'warn' (got '{on_error}').")

        overrides = dict(secrets) if secrets else {}
        self.configured_services.update({n: dict(c) if isinstance(c, dict) else {} for n, c in services.items()})
        backend_cache: Dict[Any, Any] = {}
        instances: Dict[str, Any] = {}
        registered_by: Dict[str, str] = {}  # table name → service, to catch collisions

        for name, raw_config in services.items():
            try:
                self.failed_services.pop(name, None)
                config = dict(raw_config)
                connector = config.pop("connector", name)
                spec = SERVICE_REGISTRY.get(connector)
                if spec is None:
                    raise ValueError(
                        f"Service '{name}' (connector='{connector}') is not recognized. "
                        f"Available: {', '.join(SERVICE_REGISTRY)}. (duckduck loaded from "
                        f"{os.path.dirname(os.path.abspath(__file__))} — if the connector should "
                        f"exist, that copy may be outdated: update it and reinstall with "
                        f"`pip install -e .`, then restart the Python process/kernel.)"
                    )

                # "" registers tables under their bare names (handy for local
                # sources); anything else replaces the service name as prefix.
                prefix = config.pop("table_prefix", name)

                auth = config.pop("authentication", None)
                if not auth:
                    if spec.requires_authentication:
                        raise ValueError(f"'{name}': missing 'authentication' block.")
                    auth = {"type": "local"}
                credentials = self._resolve_authentication(name, auth, overrides, backend_cache)

                for option in spec.path_options:
                    value = config.get(option)
                    if not (isinstance(value, str) and self._looks_like_path(option, value)):
                        continue
                    resolved = os.path.join(base_dir or os.getcwd(), value)
                    if not os.path.exists(resolved):
                        origin = (
                            f"the folder of the config file {self._config_path}"
                            if base_dir else f"the current directory {os.getcwd()}"
                        )
                        raise ValueError(
                            f"'{name}': {option} '{value}' resolves to '{resolved}', which doesn't exist "
                            f"(relative paths resolve against {origin}). Point it at the right place, "
                            f"e.g. relative to that folder or as an absolute path."
                        )
                    config[option] = resolved

                instance = spec.factory(credentials, **config)

                tables = {t: getattr(instance, m) for t, m in spec.tables.items()}
                if spec.dynamic_tables:
                    for t, fn in getattr(instance, spec.dynamic_tables)().items():
                        if t in tables:
                            raise ValueError(f"'{name}': data table '{t}' clashes with the built-in '{t}' table")
                        tables[t] = fn
                streaming = {t: getattr(instance, m) for t, m in spec.streaming_tables.items()}

                full = (lambda t: f"{prefix}_{t}") if prefix else (lambda t: t)
                clashes = [
                    f"{full(t)} (already from '{registered_by[full(t)]}')"
                    for t in tables if full(t) in registered_by
                ]
                if clashes:
                    raise ValueError(
                        f"'{name}': table name(s) already registered by this auto_register() call: "
                        f"{', '.join(clashes)} — give one of the services a table_prefix"
                    )
                self.service_prefix[name] = prefix
                for t, fn in tables.items():
                    self.register_api_function(full(t), fn)
                    self.service_of[full(t).lower()] = name
                    registered_by[full(t)] = name
                for t, fn in streaming.items():
                    self.register_streaming_function(full(t), fn)
            except Exception as exc:
                if on_error == "warn":
                    # Every warning here shares the same call site (this line), so
                    # Python's default filter — "show the first occurrence per
                    # (message, category, module, lineno)" — would silently
                    # swallow a repeat of the *same* failure on a later call (e.g.
                    # retrying a script/notebook cell against the same bad
                    # connector). Force this one to always display regardless of
                    # what's already in __warningregistry__ or the caller's own
                    # filters.
                    raw = raw_config if isinstance(raw_config, dict) else {}
                    self.failed_services[name] = {
                        "error": f"{exc.__class__.__name__}: {exc}",
                        "prefix": str(raw.get("table_prefix", name)),
                        "connector": str(raw.get("connector", name)),
                    }
                    with warnings.catch_warnings():
                        warnings.simplefilter("always", RuntimeWarning)
                        warnings.warn(
                            f"auto_register: service '{name}' failed to initialize "
                            f"({exc.__class__.__name__}: {exc}) — skipping.",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                    continue
                raise
            instances[name] = instance

        if views is None and base_dir is not None:
            views = self._config_views
        if views:
            from . import views as saved

            saved.register_all(self, views, on_error=on_error)
        if system_tables is None:
            system_tables = self._config_system_tables if base_dir is not None else None
        if system_tables is not False:
            self.register_system_tables()
        return instances

    def register_system_tables(self) -> None:
        """
        ``duckduck.services`` / ``tables`` / ``saved_tables`` / ``settings`` /
        ``pipeline_runs`` — this process's own state as read-only tables,
        secrets masked (``duckduck.system``). Skipped, with a warning, when a
        service is itself named ``duckduck``.
        """
        from . import system

        if system.SERVICE in self.configured_services:
            with warnings.catch_warnings():
                warnings.simplefilter("always", RuntimeWarning)
                warnings.warn(f"a service is named '{system.SERVICE}': the duckduck.* system tables aren't "
                              "registered (rename the service to get them)", RuntimeWarning, stacklevel=2)
            return
        system.register(self)

    def resolve_credentials(self, auth: Dict[str, Any], name: str = "credentials") -> Dict[str, Any]:
        """
        Resolves a standalone ``authentication`` block (same ``local`` /
        ``aws`` / ``azure`` shapes as ``auto_register``) into a plain
        credentials dict — for things configured next to the connectors
        that aren't connectors themselves (e.g. ``duckduck.semantic``'s
        Jev and LLM API keys).
        """
        return self._resolve_authentication(name, auth, {}, {})

    def _resolve_authentication(
        self,
        name: str,
        auth: Dict[str, Any],
        overrides: Dict[str, Any],
        backend_cache: Dict[Any, Any],
    ) -> Dict[str, Any]:
        """
        Turns a service's ``authentication`` block into the credentials
        dict passed to the connector's ``from_secret``. See
        ``auto_register``'s docstring for the shape of each ``type``.
        """
        auth = dict(auth)
        auth_type = auth.pop("type", "local")

        if auth_type == "local":
            return auth

        if auth_type not in ("aws", "azure"):
            raise ValueError(
                f"'{name}': authentication.type must be 'local', 'aws' or "
                f"'azure' (got '{auth_type}')."
            )

        secret_id = auth.pop("secret_id", None)
        if not secret_id:
            raise ValueError(
                f"'{name}': authentication.type='{auth_type}' requires 'secret_id'."
            )

        if auth_type == "aws":
            # profile_name selects a specific AWS account (~/.aws/credentials)
            # when more than one is configured locally.
            backend_key = ("aws", auth.pop("region_name", None), auth.pop("profile_name", None))
        else:
            vault_url = auth.pop("vault_url", None)
            if not vault_url:
                raise ValueError(
                    f"'{name}': authentication.type='azure' requires 'vault_url'."
                )
            # tenant_id pins DefaultAzureCredential to a specific Azure AD
            # tenant when the caller has access to more than one.
            backend_key = ("azure", vault_url, auth.pop("tenant_id", None))

        backend = self._get_secrets_backend(auth_type, backend_key, overrides, backend_cache)
        secret = backend.get_secret(secret_id)

        credentials = dict(secret)
        for key, val in auth.items():
            if isinstance(val, str) and val.startswith(self._SECRET_REF_PREFIX):
                secret_key = val[len(self._SECRET_REF_PREFIX):]
                if secret_key not in secret:
                    raise ValueError(
                        f"'{name}': '{val}' references missing key "
                        f"'{secret_key}' in secret '{secret_id}'."
                    )
                credentials[key] = secret[secret_key]
            else:
                credentials[key] = val
        return credentials

    def _get_secrets_backend(
        self,
        auth_type: str,
        backend_key: tuple,
        overrides: Dict[str, Any],
        backend_cache: Dict[Any, Any],
    ) -> Any:
        """
        Returns the secrets backend for ``auth_type``: an explicit
        override from ``auto_register(secrets=...)`` if one was given for
        this ``auth_type``, otherwise a cached instance built from
        ``backend_key`` — ``("aws", region_name, profile_name)`` or
        ``("azure", vault_url, tenant_id)`` — one instance per distinct
        combination, so services in different regions/vaults/accounts
        don't share a client.
        """
        if auth_type in overrides:
            return overrides[auth_type]

        if backend_key not in backend_cache:
            if auth_type == "aws":
                from .secrets import SecretsManager

                _, region_name, profile_name = backend_key
                backend_cache[backend_key] = SecretsManager(
                    region_name=region_name, profile_name=profile_name
                )
            else:
                from .azure_secrets import AzureKeyVaultSecrets

                _, vault_url, tenant_id = backend_key
                backend_cache[backend_key] = AzureKeyVaultSecrets(
                    vault_url=vault_url, tenant_id=tenant_id
                )

        return backend_cache[backend_key]

    @staticmethod
    def _looks_like_path(option: str, value: str) -> bool:
        """A relative filesystem path (not absolute, not a dotted module name)."""
        if os.path.isabs(value):
            return False
        return option == "path" or value.endswith(".py") or "/" in value or os.sep in value

    def _find_default_config_file(self) -> Optional[str]:
        """
        Walks from the current directory up to the filesystem root
        looking for ``DEFAULT_CONFIG_PATH`` ("duckduck.json") — so
        ``auto_register()`` finds the project's config even when called
        from a subdirectory of the project, not just its root.
        """
        directory = os.path.abspath(os.getcwd())
        while True:
            candidate = os.path.join(directory, self.DEFAULT_CONFIG_PATH)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(directory)
            if parent == directory:
                return None
            directory = parent

    def _load_auto_register_config(
        self,
        config_path: Optional[str],
    ) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
        """
        Resolves the ``services`` dict (and an optional default
        ``on_error``) for ``auto_register()`` from a JSON file when no
        ``services`` dict was passed in code.

        Path lookup order: ``config_path`` argument → ``DUCKDUCK_CONFIG``
        env var → ``DEFAULT_CONFIG_PATH`` ("duckduck.json"), searched from
        the current directory upward through its parents (see
        ``_find_default_config_file``) — so it's found regardless of
        which subdirectory of the project ``auto_register()`` is called
        from.
        """
        if config_path:
            path = config_path
        else:
            path = os.environ.get("DUCKDUCK_CONFIG") or self._find_default_config_file()

        if not path or not os.path.isfile(path):
            raise ValueError(
                f"auto_register() got no 'services' dict and found no config "
                f"file{f' at {path!r}' if path else ''}. Pass services=..., "
                f"pass config_path=..., set the DUCKDUCK_CONFIG environment "
                f"variable, or create '{self.DEFAULT_CONFIG_PATH}' in the "
                f"project directory."
            )

        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
        self._config_path = os.path.abspath(path)
        self._config_base_dir = os.path.dirname(self._config_path)

        self._config_views = config.get("views")
        self._config_system_tables = config.get("system_tables")
        file_services = config.get("services")
        if not file_services:
            raise ValueError(f"Config file '{path}' has no 'services' key.")

        return file_services, config.get("on_error")

    # ------------------------------------------------------------------
    # Inline kwargs parsing:  func(x=1, y="a")
    # ------------------------------------------------------------------

    def _parse_kwargs(self, text: str) -> Dict[str, Any]:
        """
        Converts the inline argument string into a dict.

        ``pr_id=123, limit=100``  →  ``{"pr_id": 123, "limit": 100}``
        """
        if not text.strip():
            return {}

        expr = ast.parse(f"_f_({self._sql_strings_as_python(text)})", mode="eval")
        call = expr.body

        if call.args:
            raise ValueError(
                "Use only named parameters. "
                "Example: assets(hostname='web', limit=50)"
            )

        kwargs: Dict[str, Any] = {}
        for kw in call.keywords:
            if kw.arg is None:
                raise ValueError("**kwargs expansion is not allowed in SQL")
            kwargs[kw.arg] = ast.literal_eval(kw.value)

        return kwargs

    @staticmethod
    def _sql_strings_as_python(text: str) -> str:
        """
        SQL single-quoted strings as Python literals: ``'O''Brien'`` is one
        string with an apostrophe in SQL, but two adjacent strings
        (``'O' 'Brien'`` → ``OBrien``) to Python. Everything else is kept.
        """
        out, i, n = [], 0, len(text)
        while i < n:
            ch = text[i]
            if ch == '"':  # a double-quoted string: copied as written
                j = i + 1
                while j < n and text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
                out.append(text[i:j + 1]); i = j + 1
            elif ch == "'":
                j, buf = i + 1, []
                while j < n:
                    if text[j] == "'" and j + 1 < n and text[j + 1] == "'":
                        buf.append("'"); j += 2
                    elif text[j] == "'":
                        break
                    else:
                        buf.append(text[j]); j += 1
                out.append(repr("".join(buf))); i = j + 1
            else:
                out.append(ch); i += 1
        return "".join(out)

    # ------------------------------------------------------------------
    # Push-down extraction from SQL
    # ------------------------------------------------------------------

    def _extract_pushdown(self, query: str) -> PushDownContext:
        """Parses the SQL with sqlglot and extracts LIMIT and simple WHERE conditions."""
        ctx = PushDownContext()

        try:
            parsed = sqlglot.parse_one(query, dialect="duckdb")
        except Exception:
            return ctx

        limit_node = parsed.find(exp.Limit)
        if limit_node is not None:
            try:
                ctx.limit = int(limit_node.expression.this)
            except (ValueError, AttributeError, TypeError):
                pass

        selects = list(parsed.find_all(exp.Select))
        if isinstance(parsed, exp.Select) and len(selects) == 1:
            where_node = parsed.args.get("where")
            if where_node is not None:
                _extract_filters(where_node, ctx)
        else:
            # subqueries, CTEs, set operations: each WHERE applies to its own SELECT's tables only
            _extract_scoped(selects, ctx, self.ARG_QUALIFIERS)

        ctx.limit_blocker = _limit_blocker(parsed)
        ctx.limit_safe = ctx.limit_blocker is None
        return ctx

    # ------------------------------------------------------------------
    # Merging explicit kwargs + push-down
    # ------------------------------------------------------------------

    def _merge_kwargs(
        self,
        fetch_function,
        pushdown: PushDownContext,
        explicit: Dict[str, Any],
        names: Optional[set] = None,
        allow_limit: bool = True,
    ) -> Dict[str, Any]:
        """
        Builds the final kwargs dict for the function call.

        Priority (highest → lowest):
        1. Explicit kwargs from the SQL call  ``func(x=1)``
        2. WHERE/LIMIT push-down from the SQL (see ``duckduck.pushdown``
           for how each operator maps onto parameters)

        ``names`` — the function's name and its alias in the query: a
        qualified condition (``a.col = 1``) only reaches the function it
        qualifies. LIMIT is pushed only when it can't change the answer:
        the query shape allows it (``limit_safe``), every WHERE condition
        was extractable, and the function consumes all of them.
        """
        return self._plan_call(fetch_function, pushdown, explicit, names, allow_limit)[0]

    def _plan_call(
        self,
        fetch_function,
        pushdown: PushDownContext,
        explicit: Dict[str, Any],
        names: Optional[set] = None,
        allow_limit: bool = True,
    ) -> Tuple[Dict[str, Any], List[str]]:
        """``_merge_kwargs`` plus a human-readable push-down report (one line per decision)."""
        accepted = set(inspect.signature(fetch_function).parameters.keys())
        # WHERE arg.table_name = 'x': explicitly an argument, never a column — fills that parameter, nothing else
        arg_conditions = self._arg_conditions(pushdown)
        applicable = [
            c for c in pushdown.conditions
            if c not in arg_conditions and (c.table is None or names is None or c.table in names)
        ]
        blocker = blocker_of(fetch_function)
        # a parameter set by arg.x is taken: a bare x = … next to it is a column filter, not that argument
        by_arg = {c.column for c in arg_conditions if c.op == "eq"}
        where_ops = where_ops_of(fetch_function)
        merged, consumed = map_conditions(accepted - by_arg, applicable, blocker, where_ops)
        targets = assign_conditions(accepted - by_arg, applicable, blocker, where_ops)

        report: List[str] = []
        args_taken = 0
        for c in arg_conditions:
            if c.op == "eq" and c.column in accepted and c.column not in ("where", "limit"):
                merged[c.column] = c.value
                args_taken += 1
                report.append(f"✓ arg.{c.column} = {c.value!r} → {c.column}")
            else:
                report.append(f"✗ arg.{c.column} — " + ("an argument is given with =" if c.op != "eq"
                                                         else "not an argument of this table"))
        for c in applicable:
            if c in targets:
                report.append(f"✓ {_describe_condition(c)} → {targets[c]}")
            elif blocker is not None and "where" in accepted and c.op in where_ops:
                report.append(f"✗ {_describe_condition(c)} — {blocker(c)}, DuckDB filters")
            else:
                report.append(f"✗ {_describe_condition(c)} — {_why_not_pushed(c, accepted)}")
        if not pushdown.complete:
            report.append("✗ part of the WHERE (OR / NOT / IN / functions...) — DuckDB only")

        if pushdown.limit is not None:
            blocker = None
            if not allow_limit:
                blocker = "stream() reads every page"
            elif "limit" not in accepted:
                blocker = "function has no limit parameter"
            elif not pushdown.limit_safe:
                blocker = f"{pushdown.limit_blocker} in the query"
            elif not pushdown.complete:
                blocker = "WHERE has conditions DuckDB must apply first"
            elif len(consumed) + args_taken != len(pushdown.conditions):
                blocker = "not every WHERE condition reached the source"
            if blocker is None:
                merged["limit"] = pushdown.limit
                report.append(f"✓ LIMIT {pushdown.limit} → limit")
            else:
                report.append(f"✗ LIMIT {pushdown.limit} — {blocker}")

        merged.update(explicit)
        return merged, report

    #: ``WHERE arg.<param> = 'x'`` — the qualifier that marks a table function's argument (never a column).
    ARG_QUALIFIERS = ("arg", "args")

    @classmethod
    def _arg_conditions(cls, pushdown: PushDownContext) -> List[Condition]:
        return [c for c in pushdown.conditions if (c.table or "").lower() in cls.ARG_QUALIFIERS]

    def _check_arguments(self, query: str, pushdown: PushDownContext) -> None:
        """Every ``arg.x`` must be an argument of a table the query reads — else it'd be silently ignored."""
        from .kinds import required_params

        for c in self._arg_conditions(pushdown):
            takers = [n for n, fn in self.functions.items()
                      if re.search(rf"\b{re.escape(n)}\b", query, re.IGNORECASE)
                      and c.column in inspect.signature(fn).parameters and c.column not in ("where", "limit")]
            if c.op != "eq":
                raise ValueError(f"arg.{c.column}: an argument is given with = (arg.{c.column} = 'value')")
            if not takers:
                reads = [n for n in self.functions if re.search(rf"\b{re.escape(n)}\b", query, re.IGNORECASE)]
                hint = "; ".join(f"{n} takes {', '.join(p.name for p in required_params(self.functions[n]))}"
                                 for n in reads if required_params(self.functions[n]))
                raise ValueError(f"arg.{c.column}: no table in this query takes an argument '{c.column}'"
                                 + (f" ({hint})" if hint else ""))

    @staticmethod
    def _structural_names(fn, explicit: Dict[str, Any]) -> set:
        """Inline arguments + required parameters: never result columns by convention."""
        required = {
            name for name, p in inspect.signature(fn).parameters.items()
            if p.default is inspect.Parameter.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        }
        return {k.lower() for k in explicit} | {k.lower() for k in required} | {"where", "limit"}

    def _log_call(self, fn_name: str, kwargs: Dict[str, Any], report: List[str]) -> None:
        if not logger.isEnabledFor(logging.INFO):
            return
        args = ", ".join(
            f"{k}=[{len(v)} conditions]" if k == "where" and isinstance(v, list) else f"{k}={short(v, 60)}"
            for k, v in kwargs.items()
        )
        logger.info("▶ %s(%s)", fn_name, args)
        for line in report:
            logger.info("    %s", line)

    #: Words that can follow a table reference but are never its alias.
    _NOT_ALIASES = {
        "where", "join", "inner", "left", "right", "full", "cross", "outer", "on", "using",
        "group", "order", "limit", "offset", "union", "except", "intersect", "having",
        "natural", "window", "qualify", "lateral", "positional", "asof", "anti", "semi",
    }

    def _alias_at(self, text: str, pos: int) -> Optional[str]:
        """The alias written right after a table reference ending at ``pos``, if any."""
        quoted = re.match(r'\s+(?:AS\s+)?"((?:[^"]|"")+)"', text[pos:], re.IGNORECASE)
        if quoted:
            return quoted.group(1).replace('""', '"').lower()  # FROM f AS "table" (a SQL client's quoting)
        m = re.match(r"\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)", text[pos:], re.IGNORECASE)
        if m and m.group(1).lower() not in self._NOT_ALIASES:
            return m.group(1).lower()
        return None

    def _has_alias(self, text: str, pos: int) -> bool:
        """Whether an alias — plain or quoted (``AS "table"``) — follows a table reference ending at ``pos``."""
        return self._alias_at(text, pos) is not None

    # ------------------------------------------------------------------
    # Signature validation
    # ------------------------------------------------------------------

    def _validate_arguments(
        self,
        function_name: str,
        fetch_function,
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Validates kwargs against the function's actual signature."""
        sig = inspect.signature(fetch_function)
        try:
            bound = sig.bind(**kwargs)
        except TypeError as err:
            raise ValueError(
                f"Invalid call to '{function_name}': {err}. "
                f"Expected signature: {function_name}{sig}"
            ) from err
        bound.apply_defaults()
        return bound.arguments

    # ------------------------------------------------------------------
    # Conversion to DataFrame
    # ------------------------------------------------------------------

    def _to_dataframe(
        self, data: Any, function_name: str, allow_empty: bool = False
    ) -> pd.DataFrame:
        """
        Normalizes the function's return value into a DataFrame.

        Accepts:
        - list[dict]
        - dict with a ``resources``, ``items``, ``data`` or ``results`` key
        - pd.DataFrame
        - None  (treated as empty, raises an error unless ``allow_empty``)
        """
        if data is None:
            data = []

        if isinstance(data, dict):
            for key in ("resources", "items", "data", "results"):
                if key in data:
                    data = data[key]
                    break
            else:
                data = [data]

        if isinstance(data, pd.DataFrame):
            df = data
        else:
            df = pd.json_normalize(data)

        if (df.empty or len(df.columns) == 0) and not allow_empty:
            raise ValueError(
                f"Table '{function_name}' returned no data. "
                "Cannot determine the columns."
            )

        df.columns = [c.replace(".", "_") for c in df.columns]
        return json_for_mixed_objects(df)

    # ------------------------------------------------------------------
    # Materialization
    # ------------------------------------------------------------------

    def _materialize(
        self,
        function_name: str,
        fetch_function,
        kwargs: Dict[str, Any],
        fallback_columns: Optional[List[str]] = None,
    ) -> tuple:
        """
        Calls the function, converts the result to a DataFrame and
        registers it in DuckDB.

        Returns
        -------
        (table_name, df_columns) : (str, list[str])
        """
        from . import progress

        from . import cache as source_cache

        validated = self._validate_arguments(function_name, fetch_function, kwargs)
        key = (source_cache.key_of("call", function_name, id(fetch_function), validated)
               if self.cache is not None else None)
        hit = self.cache.get(key) if key is not None else None
        if hit is not None:
            df, age = hit
            progress.step("fetching", f"{function_name}: from the cache (read {_ago(age)})")
            logger.info("  %s: %s rows from the cache, read %s", function_name, f"{len(df):,}", _ago(age))
            source_cache.note(function_name, True, len(df), age)
        else:
            progress.step("fetching", f"Reading {function_name}…")  # a paused / cancelled run stops here
            started = time.perf_counter()
            with slicing.parallel(self._parallel_of(function_name)):
                data = fetch_function(**validated)
            progress.checkpoint()
            df = self._to_dataframe(data, function_name, allow_empty=True)
            if key is not None:
                self.cache.put(key, df, len(df))
            source_cache.note(function_name, False, len(df))
            logger.info("  %s: %s rows × %s columns in %.2fs", function_name, f"{len(df):,}", len(df.columns),
                        time.perf_counter() - started)
        shaped = len(df.columns) == 0  # columns made up from the query: not the table's
        if shaped:
            # No rows and nothing to infer columns from (e.g. an empty JSON
            # list): shape an empty table from the columns the query itself
            # uses, so it runs and returns nothing instead of failing.
            # object dtype → DuckDB accepts it in comparisons, LIKE, sums...
            columns = list(fallback_columns or []) or [self.EMPTY_PLACEHOLDER_COLUMN]
            df = pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
            logger.info("  %s: no rows returned — empty result with columns %s", function_name, columns)

        progress.note_item("fetched", function_name, {"rows": len(df), "columns": len(df.columns),
                                                      **({"cached": True} if hit is not None else {})})
        self._table_counter += 1
        table_name = f"_api_{function_name}_{self._table_counter}"
        self.conn.register(table_name, df)
        if not shaped:
            self._learn_columns(function_name, table_name, kwargs)
        return table_name, list(df.columns)

    def _learn_columns(self, function_name: str, table_name: str, kwargs: Optional[Dict[str, Any]] = None) -> None:
        """Tells ``column_listener`` the columns (and DuckDB types) a table came back with. Never fails a query.

        A table function's columns belong to the call, not the function: they're told under its address
        (``servicenow.incident``) when it has one — ``servicenow_table`` reads a different table each time."""
        listener = self.column_listener
        if listener is None:
            return
        from .kinds import required_params

        try:
            key = function_name
            required = [p.name for p in required_params(self.functions[function_name])] \
                if function_name in self.functions else []
            if required:
                if not all(r in (kwargs or {}) for r in required):
                    return
                key = self.address_of(function_name, {r: kwargs[r] for r in required})
                if not key:
                    return
            described = self.conn.execute(f'DESCRIBE "{table_name}"').fetchall()
            listener(key, [(str(r[0]), str(r[1])) for r in described])
        except Exception as exc:  # noqa: BLE001
            logger.debug("  couldn't note the columns of %s: %s", function_name, exc)

    def _pages_instead(self, fn_name: str, kwargs: Dict[str, Any]) -> bool:
        """Read this table page by page: streaming on, a streaming function, and no LIMIT reached the source."""
        return self.stream_pages and "limit" not in kwargs and fn_name in self._streaming_functions

    def _materialize_pages(
        self,
        fn_name: str,
        pushdown: PushDownContext,
        explicit: Dict[str, Any],
        names: set,
        parsed: Optional[exp.Expression],
        fallback_columns: Optional[List[str]] = None,
    ) -> tuple:
        """
        ``_materialize`` page by page, through the table's streaming
        function (whose own signature decides what reaches the API; it
        never takes a limit). Each page keeps only:

        - the rows that pass this table's own WHERE conditions — the ANDed
          simple ones qualified with its name/alias, or unqualified on a
          column the page has (an unqualified column of the other table
          isn't in this page's columns, and one in both would be ambiguous
          SQL anyway). DuckDB applies them exactly, LIKE patterns included,
          and re-applies the whole WHERE afterwards regardless;
        - the columns the query uses from it (every column under ``*``).

        A single-table query whose LIMIT can't change the answer
        (``limit_safe``, a complete WHERE, every condition applied here)
        stops asking for pages once it has ``LIMIT`` rows.

        Returns ``(table_name, df_columns, kwargs)``.
        """
        iter_fn = self._streaming_functions[fn_name]
        kwargs, report = self._plan_call(iter_fn, pushdown, explicit, names, allow_limit=False)
        self._log_call(f"{fn_name} (page by page)", kwargs, report)
        conditions = [c for c in pushdown.conditions if c.table is None or c.table in names]
        star = parsed is None or parsed.find(exp.Star) is not None
        used = {c.lower() for c in (fallback_columns or [])}
        tables = len(list(parsed.find_all(exp.Table))) if parsed is not None else 2
        stop_at = (pushdown.limit if pushdown.limit is not None and pushdown.limit_safe and pushdown.complete
                   and tables == 1 else None)

        from . import cache as source_cache
        from . import progress

        validated = self._validate_arguments(fn_name, iter_fn, kwargs)
        key = (source_cache.key_of("pages", fn_name, id(iter_fn), validated, conditions,
                                   "*" if star else sorted(used), stop_at)
               if self.cache is not None else None)
        hit = self.cache.get(key) if key is not None else None
        if hit is not None:
            (df, count, scanned, pages), age = hit
            progress.step("fetching", f"{fn_name}: from the cache (read {_ago(age)})")
            logger.info("  %s: %s rows from the cache (kept of %s read page by page), read %s", fn_name,
                        f"{count:,}", f"{scanned:,}", _ago(age))
            source_cache.note(fn_name, True, len(df), age)
            progress.note_item("fetched", fn_name, {"rows": count, "rows_scanned": scanned, "pages": pages,
                                                    "cached": True})
            self._table_counter += 1
            table_name = f"_api_{fn_name}_{self._table_counter}"
            self.conn.register(table_name, df)
            return table_name, list(df.columns), kwargs
        progress.step("fetching", f"Reading {fn_name} page by page…")
        started = time.perf_counter()
        kept: List[pd.DataFrame] = []
        count = pages = scanned = 0
        last_columns: List[str] = []
        pages_iter = iter_fn(**validated)
        requests_at_once = slicing.parallel(self._parallel_of(fn_name))
        requests_at_once.__enter__()  # read by the connector's pager at its first page, below
        try:
            for page in pages_iter:
                progress.checkpoint()  # pause / cancel between pages
                df = self._to_dataframe(page, fn_name, allow_empty=True)
                if df.empty:
                    if len(df.columns) and not last_columns:  # no rows, but the columns: an empty result keeps them
                        last_columns = list(df.columns) if star else (
                            [c for c in df.columns if c.lower() in used] or list(df.columns[:1]))
                    continue
                pages += 1
                scanned += len(df)
                columns = list(df.columns) if star else ([c for c in df.columns if c.lower() in used]
                                                         or list(df.columns[:1]))
                last_columns = columns
                where = conditions_to_sql(conditions, df.columns)
                every = all(c.column.lower() in {x.lower() for x in df.columns} for c in conditions)
                self._table_counter += 1
                view = f"_page_{fn_name}_{self._table_counter}"
                self.conn.register(view, df)
                try:
                    select = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
                    out = self.conn.sql(f"SELECT {select} FROM {view}" + (f" WHERE {where}" if where else "")).df()
                finally:
                    self.conn.unregister(view)
                if len(out):
                    kept.append(out)
                    count += len(out)
                progress.update(f"Reading {fn_name}: page {pages} · kept {count:,} of {scanned:,} rows")
                if stop_at is not None and every and count >= stop_at:
                    break  # enough rows for the answer: no more pages asked
        finally:
            close = getattr(pages_iter, "close", None)
            if close is not None:
                close()
            requests_at_once.__exit__(None, None, None)
        if kept:
            df = json_for_mixed_objects(pd.concat(kept, ignore_index=True))
        else:
            columns = last_columns or list(fallback_columns or []) or [self.EMPTY_PLACEHOLDER_COLUMN]
            df = pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
        if key is not None:
            self.cache.put(key, (df, count, scanned, pages), len(df))
        source_cache.note(fn_name, False, len(df))
        progress.note_item("fetched", fn_name, {"rows": count, "rows_scanned": scanned, "pages": pages})
        logger.info("  %s: kept %s of %s rows from %s page(s), %s column(s), in %.2fs", fn_name, f"{count:,}",
                    f"{scanned:,}", pages, len(df.columns), time.perf_counter() - started)
        self._table_counter += 1
        table_name = f"_api_{fn_name}_{self._table_counter}"
        self.conn.register(table_name, df)
        if star and kept:  # every column of the pages, not just the ones the query used
            self._learn_columns(fn_name, table_name, kwargs)
        return table_name, list(df.columns), kwargs

    #: Sole column of an empty result when neither the source nor the query
    #: says which columns there are (``SELECT *`` over zero rows).
    EMPTY_PLACEHOLDER_COLUMN = "_no_rows"

    @staticmethod
    def _referenced_columns(parsed: Optional[exp.Expression], names: set, exclude: set) -> List[str]:
        """
        Columns the query uses from the table called ``names`` (its name or
        alias) — qualified with one of them, or unqualified — minus
        ``exclude`` (structural parameters, which aren't result columns).
        """
        if parsed is None:
            return []
        found: List[str] = []
        for col in parsed.find_all(exp.Column):
            name = col.name
            qualifier = col.table.lower() if col.table else None
            if not name or name == "*" or name.lower() in exclude:
                continue
            if qualifier is None or qualifier in names:
                if name not in found:
                    found.append(name)
        return found

    def fetch(self, name: str, **kwargs) -> pd.DataFrame:
        """
        Calls a registered table's function directly with explicit
        ``kwargs`` — no SQL parsing, no push-down inference — and returns
        its result as a DataFrame (column names normalized the same way
        ``sql()`` does, ``.`` → ``_``).

        Unlike ``sql()``, an empty result is not an error: it comes back
        as an empty DataFrame (with no columns when the function gave no
        way to infer them). Meant for callers that do their own push-down
        planning per source, e.g. ``duckduck.semantic``'s executor.
        """
        fn = self.functions.get(name.lower())
        if fn is None:
            raise KeyError(f"No table registered as '{name}'.")
        validated = self._validate_arguments(name, fn, kwargs)
        started = time.perf_counter()
        with slicing.parallel(self._parallel_of(name)):
            data = fn(**validated)
        df = self._to_dataframe(data, name, allow_empty=True)
        logger.info("  %s: %s rows in %.2fs", name, f"{len(df):,}", time.perf_counter() - started)
        return df

    def streaming_function(self, name: str):
        """The generator registered for ``name`` with ``register_streaming_function``, or ``None``."""
        return self._streaming_functions.get(name.lower())

    def fetch_pages(self, name: str, **kwargs):
        """
        ``fetch`` page by page: calls the table's streaming function with
        explicit ``kwargs`` and yields each page as a DataFrame (normalized
        like ``fetch``; empty pages skipped). Only one page is in memory at
        a time — for callers that filter as they go (the semantic executor).
        """
        iter_fn = self.streaming_function(name)
        if iter_fn is None:
            raise KeyError(f"No streaming function registered for '{name}'.")
        validated = self._validate_arguments(name, iter_fn, kwargs)
        pages = iter_fn(**validated)
        try:
            for page in pages:
                df = self._to_dataframe(page, name, allow_empty=True)
                if not df.empty:
                    yield df
        finally:
            close = getattr(pages, "close", None)
            if close is not None:  # stopped early (enough rows): let the connector stop paging
                close()

    def _strip_where_conditions(self, query: str, keys: set, qualifiers: Tuple[str, ...] = (),
                                qualified: Optional[Dict[str, set]] = None) -> str:
        """
        Removes WHERE conditions that reference columns in ``keys``.

        Used to discard push-down filters that were consumed by the
        function but don't exist as columns in the resulting
        DataFrame — typically structural parameters like ``site_name``,
        ``list_name`` — and every condition qualified by one of
        ``qualifiers`` (``arg.table_name = 'x'``: an argument, never a column).
        ``qualified``: key → the names (function, alias) of the tables it was
        an argument of — ``t.table_name = 'x'`` is removed too when ``t`` is one.
        """
        if not keys and not qualifiers:
            return query
        try:
            tree = sqlglot.parse_one(query, dialect="duckdb")
        except Exception:
            return query

        where = tree.find(exp.Where)

        def _should_keep(node: exp.Expression) -> bool:
            if isinstance(node, (exp.EQ, exp.Like, exp.ILike, exp.GT, exp.LT, exp.GTE, exp.LTE, exp.NEQ)):
                if isinstance(node.this, exp.Column):
                    if (node.this.table or "").lower() in qualifiers:
                        return False
                    name, table = node.this.name.lower(), (node.this.table or "").lower()
                    if name in keys and (not table or table in (qualified or {}).get(name, ())):
                        return False
            return True

        def _rebuild(node: exp.Expression):
            if isinstance(node, exp.And):
                left = _rebuild(node.this)
                right = _rebuild(node.expression)
                if left is None:
                    return right
                if right is None:
                    return left
                return exp.And(this=left, expression=right)
            return node if _should_keep(node) else None

        if where is not None:
            new_condition = _rebuild(where.this)
            if new_condition is None:
                where.pop()
            else:
                where.set("this", new_condition)

        # a join's ON constant that filled an argument (JOIN sn_table t ON t.table_name = 'incident' AND …)
        def _on_keep(node: exp.Expression) -> bool:
            if isinstance(node, exp.EQ) and isinstance(node.this, exp.Column) \
                    and not isinstance(node.expression, exp.Column):
                name, table = node.this.name.lower(), (node.this.table or "").lower()
                if name in keys and table and table in (qualified or {}).get(name, ()):
                    return False
            return True

        changed_on = False
        for join in tree.find_all(exp.Join):
            on = join.args.get("on")
            if on is None:
                continue
            kept = [c for c in _conjuncts(on) if _on_keep(c)]
            if len(kept) == len(_conjuncts(on)):
                continue
            changed_on = True
            join.set("on", exp.and_(*kept) if kept else exp.true())
        if where is None and not changed_on:
            return query
        return tree.sql(dialect="duckdb")

    # ------------------------------------------------------------------
    # Main SQL entry point
    # ------------------------------------------------------------------

    #: Matches "SHOW TABLES", "LIST TABLES", "SHOW ALL TABLES", "LIST ALL
    #: TABLES" (any case, optional trailing ";") — the shortcut ``sql()``
    #: recognizes for ``list_tables()``.
    _LIST_TABLES_RE = re.compile(r"^\s*(SHOW|LIST)(\s+ALL)?\s+TABLES\s*;?\s*$", re.IGNORECASE)

    #: Maps a bundled connector's module name to a human-readable label,
    #: used by ``list_tables()``'s ``source`` column. A function registered
    #: from outside these modules (a hand-rolled wrapper, a lambda in a
    #: notebook, ...) falls back to its own ``__module__``.
    _CONNECTOR_SOURCE_LABELS = {
        "duckduck.sharepoint": "SharePoint (HTTP API)",
        "duckduck.rapid7": "InsightVM (HTTP API)",
        "duckduck.servicenow": "ServiceNow (HTTP API)",
        "duckduck.axonius": "Axonius (HTTP API)",
        "duckduck.nvd": "NVD — National Vulnerability Database (HTTP API)",
        "duckduck.restcountries": "REST Countries (HTTP API)",
        "duckduck.database": "SQL database",
        "duckduck.glue": "S3 / Glue Data Catalog",
        "duckduck.athena": "Amazon Athena (SQL on S3)",
        "duckduck.airflow": "Apache Airflow on MWAA (REST API)",
        "duckduck.blob_storage": "Azure Blob Storage",
        "duckduck.adx": "Azure Data Explorer (KQL)",
        "duckduck.local_files": "Local files",
        "duckduck.python_source": "Python module",
        "duckduck.semantic.takeover": "Taken over (answer rows, in memory)",
        "duckduck.views": "Saved query",
    }

    @classmethod
    def _describe_source(cls, fn: Any) -> str:
        """Human-readable label for what kind of thing a registered function is."""
        module = getattr(fn, "__module__", None) or ""
        for prefix, label in cls._CONNECTOR_SOURCE_LABELS.items():
            if module == prefix or module.startswith(prefix + "."):
                return label
        return module or "custom function"

    @staticmethod
    def _describe_endpoint(fn: Any) -> Optional[str]:
        """
        Best-effort "where does this actually point at" for a registered
        function, by inspecting the bound instance behind it (``fn.__self__``
        for a bound method) for the attributes the bundled connectors
        expose. Returns ``None`` when nothing recognizable is found —
        never raises, since this is purely informational.
        """
        instance = getattr(fn, "__self__", None)
        if instance is None:
            # a callable object (e.g. a local FileTable) may carry it itself
            own = getattr(fn, "base_url", None)
            return str(own) if isinstance(own, str) else None

        base_url = getattr(instance, "base_url", None)
        if base_url:
            return str(base_url)

        engine = getattr(instance, "engine", None)
        url = getattr(engine, "url", None) if engine is not None else None
        if url is not None:
            render = getattr(url, "render_as_string", None)
            return render(hide_password=True) if render else str(url)

        return None

    @staticmethod
    def _describe_function(fn: Any) -> str:
        """First line of the function's docstring, or "" if it has none."""
        doc = inspect.getdoc(fn)
        if not doc:
            return ""
        return doc.strip().splitlines()[0].strip()

    def list_tables(self, kind=None, details: bool = False) -> pd.DataFrame:
        """
        Lists everything registered via ``register_api_function()`` /
        ``auto_register()`` — and, crucially, *what each one is*, since not
        every name is a plain table (see ``duckduck.kinds``):

        - ``table``: data you can ``SELECT`` as-is.
        - ``table function``: data behind required arguments —
          ``SELECT * FROM glue_table(database='…', table_name='…')``.
        - ``catalog``: lists what a source contains (tables, columns...),
          which is how you find those arguments.
        - ``raw query``: runs a query written in the source's own language.

        ``SHOW TABLES`` / ``LIST TABLES`` in ``sql()`` return the same.

        Parameters
        ----------
        kind : str or list of str, optional
            Keep only these kinds, e.g. ``kind="table"`` or
            ``kind=["table", "table function"]``.
        details : bool
            Add ``streaming`` (whether ``stream()`` works for it) and
            ``signature`` (the raw Python signature).

        Returns
        -------
        pd.DataFrame
            One row per registered name, ordered by kind then name:

            - ``name``: what goes after ``FROM``.
            - ``kind``: one of the four above.
            - ``usage``: a ready-to-edit example query, required arguments
              included as ``'<placeholders>'``.
            - ``pushdown``: which WHERE conditions / LIMIT the source applies
              itself (everything else still works: DuckDB filters after
              fetching).
            - ``source``: the connector (``"ServiceNow (HTTP API)"``,
              ``"Local files"``...) or, for anything else, the function's
              module.
            - ``endpoint``: where it points — base URL, connection string
              (password redacted), file path — when there's one to show.
            - ``description``: first line of the function's docstring.
        """
        from .kinds import ORDER, describe

        wanted = None
        if kind is not None:
            wanted = {kind} if isinstance(kind, str) else set(kind)
            unknown = wanted - set(ORDER)
            if unknown:
                raise ValueError(f"unknown kind(s) {sorted(unknown)} — use: {', '.join(ORDER)}")

        rows = []
        for name, fn in self.functions.items():
            info = describe(name, fn)
            if wanted is not None and info["kind"] not in wanted:
                continue
            row = {
                "name": name,
                **info,
                "source": self._describe_source(fn),
                "endpoint": self._describe_endpoint(fn),
                "description": self._describe_function(fn),
            }
            if details:
                row["streaming"] = name in self._streaming_functions
                try:
                    row["signature"] = str(inspect.signature(fn))
                except (TypeError, ValueError):
                    row["signature"] = None
            rows.append(row)

        columns = ["name", "kind", "usage", "pushdown", "source", "endpoint", "description"]
        if details:
            columns += ["streaming", "signature"]
        df = pd.DataFrame(rows, columns=columns)
        if not df.empty:
            rank = {k: i for i, k in enumerate(ORDER)}
            df = df.sort_values(["kind", "name"], key=lambda col: col.map(rank) if col.name == "kind" else col)
            df = df.reset_index(drop=True)
        return df

    def nested_tables(
        self, catalog: Optional[str] = None, service: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        The tables *behind* connectors: every catalog that declares
        ``lists=`` (``glue_tables`` → ``glue_table``, ``adx_tables`` →
        ``adx_table``, ``<db>_tables`` → ``<db>_table``) is read, and each
        row becomes a call of that table function, its required arguments
        taken from the row's columns. ``catalog``: read only that one;
        ``service``: only the catalogs of that ``auto_register`` service.

        Returns ``(tables, notes)``: one dict per nested table — ``catalog``
        (where it was listed), ``table`` (the table function), ``args``,
        ``label`` (the joined arguments, ``security.proxy_logs``), ``usage``
        (``SELECT * FROM glue_table(database='security', …) LIMIT 100``) and
        ``service`` — plus a note per catalog that couldn't be read. Every
        catalog read is a call to its source.
        """
        from .kinds import CATALOG, drafts_of, kind_of, lists_of, required_params

        tables: List[Dict[str, Any]] = []
        notes: List[str] = []
        for catalog_name, catalog_fn in self.functions.items():
            if catalog is not None and catalog_name != catalog.lower():
                continue
            if service is not None and self.service_of.get(catalog_name) != service:
                continue
            method = lists_of(catalog_fn)
            if kind_of(catalog_fn) != CATALOG or not method:
                continue
            target = self.sibling_function(catalog_fn, method)
            if target is None:
                notes.append(f"catalog '{catalog_name}' lists '{method}', which isn't registered — skipped")
                continue
            required = [p.name for p in required_params(self.functions[target])]
            try:
                listing = self.fetch(catalog_name)
            except Exception as exc:
                notes.append(f"catalog '{catalog_name}' failed ({exc.__class__.__name__}: {exc}) — its tables skipped")
                continue
            missing = [r for r in required if r not in listing.columns]
            if missing:
                notes.append(f"catalog '{catalog_name}' has no column(s) {missing} for '{target}' — skipped")
                continue
            for row in listing[required].to_dict(orient="records"):
                args = {r: (v.item() if hasattr(v, "item") else v) for r, v in row.items()}
                call = ", ".join(f"{k}={self._sql_literal(v)}" for k, v in args.items())
                tables.append({
                    "catalog": catalog_name, "table": target, "args": args,
                    "label": ".".join(str(v) for v in args.values()),
                    "usage": f"SELECT * FROM {target}({call}) LIMIT 100",
                    "service": self.service_of.get(catalog_name),
                    "address": self.address_of(target, args),  # s3_data.security.proxy_logs
                    "drafts": drafts_of(catalog_fn),
                })
        return tables, notes

    def sibling_function(self, fn: Any, method: str) -> Optional[str]:
        """The registered name of ``method`` on the same connector instance as ``fn`` (a bound method)."""
        owner = getattr(fn, "__self__", None)
        if owner is None:
            return None
        for name, other in self.functions.items():
            if getattr(other, "__self__", None) is owner and getattr(other, "__name__", None) == method:
                return name
        return None

    @staticmethod
    def _sql_literal(value: Any) -> str:
        if isinstance(value, bool) or value is None:
            return {True: "true", False: "false", None: "NULL"}[value]
        if isinstance(value, (int, float)):
            return repr(value)
        return "'" + str(value).replace("'", "''") + "'"

    def sql(self, query: str):
        """
        Executes a SQL query, replacing references to registered
        functions with the corresponding DataFrames.

        Supports:
        - ``FROM func``                → automatic WHERE/LIMIT push-down
        - ``FROM func(struct_id=1)``   → structural parameter (builds URL/path)
                                         + WHERE/LIMIT push-down
        - ``JOIN func(struct_id=1)``   → same as above
        - Multiple references to the same function or different functions
        - ``SHOW TABLES`` / ``LIST TABLES`` (optionally ``ALL``) → shortcut
          for ``list_tables()``, listing every registered table

        Structural parameters (``site_name``, ``list_name``, etc.) can
        appear either inline or in the ``WHERE`` clause. When they're in
        the WHERE clause and aren't columns of the result, they're
        automatically removed from the query before DuckDB executes it.

        Returns
        -------
        duckdb.DuckDBPyRelation
            DuckDB relation. Use ``.df()`` to get a DataFrame.
        """
        if self._LIST_TABLES_RE.match(query):
            self.conn.register("_duckduck_tables", self.list_tables())
            return self.conn.sql("SELECT * FROM _duckduck_tables")

        written, query = query, self.resolve_addresses(query)
        if query != written:
            logger.debug("  addresses → %s", " ".join(query.split()))

        check_syntax(self._calls_blanked(query))  # a typo (a JOIN without ON) fails here, before any source is read
        pushdown = self._extract_pushdown(query)
        self._check_arguments(query, pushdown)
        rewritten = query
        structural_used: set = set()  # WHERE filters consumed that aren't columns
        structural_of: Dict[str, set] = {}  # … and the names (function, alias) of the tables that took them
        started = time.perf_counter()
        sources = 0
        try:
            parsed = sqlglot.parse_one(query, dialect="duckdb")
        except Exception:
            parsed = None

        # Joins between two sources: read one side first, its key values narrow the other (see _join_plan)
        edges: List[JoinEdge] = []
        on_conditions: List[Condition] = []
        if self.join_pushdown and parsed is not None:
            refs = self._function_refs(parsed)
            edges, on_conditions = _join_plan(parsed)
            edges = [e for e in edges if e.probe in refs and e.build in refs]
            on_conditions = [c for c in on_conditions if c.table in refs]
        single_select = isinstance(parsed, exp.Select) and len(list(parsed.find_all(exp.Select))) == 1
        read: Dict[str, Tuple[str, List[str]]] = {}  # a table's ref → (its temp table, its columns)
        force = False
        while True:
            waited = progressed = False
            for fn_name, fn in list(self.functions.items()):
                patterns = (
                    (False, re.compile(rf"\b{re.escape(fn_name)}\s*\((.*?)\)", flags=re.IGNORECASE | re.DOTALL)),
                    (True, re.compile(rf"\b(FROM|JOIN)\s+{re.escape(fn_name)}\b(?!\s*\()", flags=re.IGNORECASE)),
                )
                for bare, pattern in patterns:
                    pos = 0
                    while m := pattern.search(rewritten, pos):
                        names = {fn_name, self._alias_at(rewritten, m.end())} - {None}
                        if not force and any(e.probe in names and e.build not in read and e.build not in names
                                             for e in edges):
                            waited = True  # the other side of its join isn't read yet: after it
                            pos = m.end()
                            continue
                        explicit = {} if bare else self._parse_kwargs(m.group(1))
                        tname, df_cols, used = self._read_source(fn_name, fn, pushdown, explicit, names, parsed,
                                                                 edges, on_conditions, read, single_select)
                        sources += 1
                        progressed = True
                        for n in names:
                            read[n] = (tname, df_cols)
                        # WHERE / ON filters that reached the function but aren't result columns
                        for k in used:
                            if k not in df_cols:
                                structural_used.add(k)
                                structural_of.setdefault(k, set()).update(n.lower() for n in names)
                        if bare:
                            # no alias written: the temp table keeps the function's name (SELECT nvd_cves.id …)
                            keep = "" if self._has_alias(rewritten, m.end()) else f" AS {fn_name}"
                            replacement = f"{m.group(1)} {tname}{keep}"
                        else:
                            replacement = tname
                        rewritten = rewritten[: m.start()] + replacement + rewritten[m.end():]
                        pos = m.start() + len(replacement)
            if not waited:
                break
            if not progressed:
                force = True  # a cycle, or a side that never shows up: read the rest as they are

        if structural_used or self._arg_conditions(pushdown):
            rewritten = self._strip_where_conditions(rewritten, structural_used, self.ARG_QUALIFIERS, structural_of)

        if sources:
            logger.info("%d source(s) fetched in %.2fs — DuckDB runs the rest of the query", sources,
                        time.perf_counter() - started)
            logger.debug("    %s", " ".join(rewritten.split()))
        return self.conn.sql(rewritten)

    # ------------------------------------------------------------------
    # One source of a query, and what a join sends it
    # ------------------------------------------------------------------

    def _calls_at_once(self, fn_name: str) -> int:
        """How many separate calls of this table may run at once (a join's values split up): its declared
        ``max_parallel`` unless it's read on the driver only (a rate limit, a cursor) — then one."""
        if not self.parallel:
            return 1
        plan = self._plan_of(fn_name)
        if plan is None or plan.strategy == "driver":
            return 1
        return plan.max_parallel

    def _plan_of(self, fn_name: str) -> Any:
        from .sparkplan import plan_of
        from .views import VIEW_ATTR

        fn = self.functions.get(fn_name)
        view = getattr(fn, VIEW_ATTR, None)
        if isinstance(view, dict) and view.get("table"):
            fn = self.functions.get(str(view["table"]).lower(), fn)
        return plan_of(fn) if fn is not None else None

    def _parallel_of(self, fn_name: str) -> int:
        """How many of this table's pages may be requested at once: its declared ``max_parallel`` when its API
        reads any page on its own (``@spark_plan("partitioned", by="pages")``), else 1 — a saved table's is its
        base table's."""
        if not self.parallel:
            return 1
        plan = self._plan_of(fn_name)
        return plan.max_parallel if plan and plan.strategy == "partitioned" and plan.by == "pages" else 1

    def _calls_blanked(self, query: str) -> str:
        """The query with each registered function's inline arguments emptied (``assets(limit=3)`` → ``assets()``):
        ``k=v`` isn't SQL DuckDB's parser takes when ``k`` is a keyword, and they're ours to read anyway."""
        masked = _mask_strings(query)
        for fn_name in sorted(self.functions, key=len, reverse=True):
            pattern = re.compile(rf"\b{re.escape(fn_name)}\s*\((.*?)\)", flags=re.IGNORECASE | re.DOTALL)
            spans = [(m.start(1), m.end(1)) for m in pattern.finditer(masked)]
            for start, end in reversed(spans):
                query = query[:start] + " " * (end - start) + query[end:]
                masked = masked[:start] + " " * (end - start) + masked[end:]
        return query

    def _function_refs(self, parsed: exp.Expression) -> set:
        """The refs (alias, else name) of the tables in the query that are registered functions."""
        refs = set()
        for t in parsed.find_all(exp.Table):
            name = (t.this.name if isinstance(t.this, exp.Anonymous) else t.name).lower()
            if name in self.functions:
                ref = _table_ref(t)
                if ref:
                    refs.add(ref)
        return refs

    def _read_source(self, fn_name: str, fn: Any, pushdown: PushDownContext, explicit: Dict[str, Any],
                     names: set, parsed: Optional[exp.Expression], edges: List[JoinEdge],
                     on_conditions: List[Condition], read: Dict[str, Tuple[str, List[str]]],
                     single_select: bool) -> Tuple[str, List[str], set]:
        """Reads one function reference of the query: (temp table, its columns, the eq filters it was given)."""
        extra, notes = self._join_conditions(names, edges, on_conditions, read, pushdown, single_select)
        fallback = self._referenced_columns(parsed, names, self._structural_names(fn, explicit))
        if extra is None:  # the other side of its join has no rows: nothing of this one can be in the answer
            self._log_call(fn_name, {}, notes)
            columns = list(fallback or []) or [self.EMPTY_PLACEHOLDER_COLUMN]
            df = pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
            self._table_counter += 1
            tname = f"_api_{fn_name}_{self._table_counter}"
            self.conn.register(tname, df)
            takes = set(inspect.signature(fn).parameters)  # its arguments in the WHERE / ON aren't columns
            return tname, list(df.columns), {k for k in pushdown.filters if k in takes} | {
                c.column for c in on_conditions if c.table in names and c.op == "eq" and c.column in takes}
        call = replace(pushdown, conditions=pushdown.conditions + extra) if extra else pushdown
        calls, report, fanned = self._plan_calls(fn, call, explicit, names, [c for c in extra if c.op == "in"])
        report = notes + report
        used = {k for k in list(pushdown.filters) + [c.column for c in extra if c.op == "eq"]
                if any(k in kw for kw in calls)}
        if len(calls) == 1 and self._pages_instead(fn_name, calls[0]):
            for line in notes:
                logger.info("    %s", line)
            tname, df_cols, kwargs = self._materialize_pages(fn_name, call, explicit, names, parsed, fallback)
            return tname, df_cols, {k for k in used if k in kwargs}
        if len(calls) == 1:
            self._log_call(fn_name, calls[0], report)
            tname, df_cols = self._materialize(fn_name, fn, calls[0], fallback)
            return tname, df_cols, used
        self._log_call(f"{fn_name} × {len(calls)} calls", calls[0], report)
        tname, df_cols = self._materialize_calls(fn_name, fn, calls, fallback, fanned)
        return tname, df_cols, used

    def _join_conditions(self, names: set, edges: List[JoinEdge], on_conditions: List[Condition],
                         read: Dict[str, Tuple[str, List[str]]], pushdown: PushDownContext,
                         single_select: bool) -> Tuple[Optional[List[Condition]], List[str]]:
        """
        What joins add to a table's conditions: its ON constants, and
        ``col IN (values)`` for each join whose other side was read — the
        distinct, non-null values of that side's key, after that side's own
        WHERE conditions (a single SELECT's; else unfiltered, a superset).
        None: the other side has no such values, so this one needn't be read.
        """
        extra = [c for c in on_conditions if c.table in names]
        notes = [f"✓ ON {_describe_condition(c)} — a join condition on this table only" for c in extra]
        for e in edges:
            if e.probe not in names or e.build not in read:
                continue
            tname, columns = read[e.build]
            column = {c.lower(): c for c in columns}.get(e.build_column.lower())
            what = f"{e.join} {e.probe}.{e.probe_column} = {e.build}.{e.build_column}"
            if column is None:
                notes.append(f"✗ {what} — {e.build} came back without {e.build_column}")
                continue
            own = [c for c in pushdown.conditions if single_select and c.table == e.build and c.op != "in"]
            where = conditions_to_sql(own, columns)
            ident = '"' + column.replace('"', '""') + '"'
            sql = (f'SELECT DISTINCT {ident} FROM "{tname}" WHERE {ident} IS NOT NULL'
                   + (f" AND {where}" if where else "") + f" LIMIT {self.join_values_max + 1}")
            try:
                values = [_join_value(r[0]) for r in self.conn.execute(sql).fetchall()]
            except Exception as exc:  # noqa: BLE001 — narrowing is an optimisation: the join still runs
                notes.append(f"✗ {what} — couldn't read {e.build}'s values ({exc.__class__.__name__})")
                continue
            if any(v is _UNUSABLE for v in values):
                notes.append(f"✗ {what} — {e.build}.{e.build_column} isn't a plain value (list/struct)")
                continue
            if len(values) > self.join_values_max:
                notes.append(f"✗ {what} — more than {self.join_values_max:,} values (join_values_max): not narrowed")
                continue
            if not values:
                notes.append(f"✓ {what} — {e.build} has none: not read")
                return None, notes
            notes.append(f"  {what}: {len(values):,} value(s) from {e.build}")
            extra.append(Condition(e.probe_column, "in", tuple(values), e.probe))
        return extra, notes

    def _plan_calls(self, fn: Any, pushdown: PushDownContext, explicit: Dict[str, Any], names: set,
                    in_conditions: List[Condition]) -> Tuple[List[Dict[str, Any]], List[str], Optional[str]]:
        """
        ``_plan_call``, then a join's ``IN`` made to fit: split into chunks
        of the connector's ``IN_MAX`` (several calls), or — when the function
        takes no ``col_in`` and its ``where`` no ``in`` — one call per value
        on its ``col`` parameter (a lookup join; ``asset_id`` of
        ``asset_vulnerabilities``). At most ``join_calls_max`` calls, and
        only one condition expands; beyond that DuckDB joins what's read.
        Returns (the calls' kwargs, the report, the parameter fanned out on).
        """
        kwargs, report = self._plan_call(fn, pushdown, explicit, names)
        calls = [kwargs]
        fanned: Optional[str] = None
        if not in_conditions:
            return calls, report, fanned
        accepted = set(inspect.signature(fn).parameters)
        owner = getattr(fn, "__self__", None) or (fn if not inspect.isfunction(fn) else None)
        in_max = getattr(owner, "IN_MAX", None)
        expanded = False
        for c in in_conditions:
            values = list(c.value)
            said = _describe_condition(c)
            param = f"{c.column}_in"
            in_where = any(x is c for x in (kwargs.get("where") or []))
            if param in kwargs or in_where:
                size = int(in_max) if in_max else len(values)
                if len(values) <= size:
                    continue
                chunks = [values[i:i + size] for i in range(0, len(values), size)]
                if expanded or len(chunks) > self.join_calls_max:
                    calls = [_without_in(kw, c, param) for kw in calls]
                    report.append(f"✗ {said} — {len(chunks)} calls of ≤ {size} (join_calls_max "
                                  f"{self.join_calls_max}): DuckDB joins")
                    continue
                calls = [_with_in(kw, c, param, chunk) for kw in calls for chunk in chunks]
                expanded = True
                report.append(f"✓ {said} → {len(chunks)} calls of ≤ {size} values (IN_MAX)")
                continue
            target = c.column if (c.column in accepted and c.column not in ("where", "limit")
                                  and c.column not in kwargs) else None
            if target is None:
                continue
            if expanded or len(values) > self.join_calls_max:
                report.append(f"✗ {said} — one call per value would be {len(values):,} calls "
                              f"(join_calls_max {self.join_calls_max}): DuckDB joins")
                continue
            calls = [dict(kw, **{target: v}) for kw in calls for v in values]
            expanded, fanned = True, target
            report = [line for line in report if not line.startswith(f"✗ {said}")]
            report.append(f"✓ {said} → one call per value on {target} ({len(values)} calls)")
        return calls, report, fanned

    def _materialize_calls(self, fn_name: str, fn: Any, calls: List[Dict[str, Any]],
                           fallback: Optional[List[str]], fanned: Optional[str]) -> Tuple[str, List[str]]:
        """Several calls of one table (a join's values split up): read — several at once when the table allows it
        (``_calls_at_once``) —, kept as one table. A call per value on a parameter that isn't a result column
        gets it as one, so the join's ON still binds."""
        from . import cache as source_cache
        from . import progress

        def one(kwargs: Dict[str, Any]) -> pd.DataFrame:
            validated = self._validate_arguments(fn_name, fn, kwargs)
            key = (source_cache.key_of("call", fn_name, id(fn), validated) if self.cache is not None else None)
            hit = self.cache.get(key) if key is not None else None
            if hit is not None:
                df = hit[0]
            else:
                progress.checkpoint()
                with slicing.parallel(1):  # the calls are the parallelism: each reads its pages in turn
                    df = self._to_dataframe(fn(**validated), fn_name, allow_empty=True)
                if key is not None:
                    self.cache.put(key, df, len(df))
            if fanned and fanned.lower() not in {c.lower() for c in df.columns}:
                df = df.copy()
                df[fanned] = kwargs[fanned]
            return df

        at_once = self._calls_at_once(fn_name)
        progress.step("fetching", f"Reading {fn_name}: {len(calls)} calls, {at_once} at a time…")
        started = time.perf_counter()
        if at_once > 1:
            with ThreadPoolExecutor(max_workers=min(at_once, len(calls)), thread_name_prefix="duckduck-call") as pool:
                frames = list(pool.map(lambda kw: contextvars.copy_context().run(one, kw), calls))
        else:
            frames = [one(kw) for kw in calls]
        frames = [f for f in frames if len(f.columns)]
        df = json_for_mixed_objects(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame(
            {c: pd.Series(dtype="object") for c in (list(fallback or []) or [self.EMPTY_PLACEHOLDER_COLUMN])})
        self._table_counter += 1
        tname = f"_api_{fn_name}_{self._table_counter}"
        self.conn.register(tname, df)
        logger.info("  %s: %s rows from %d calls (%d at a time) in %.2fs", fn_name, f"{len(df):,}", len(calls),
                    at_once, time.perf_counter() - started)
        return tname, list(df.columns)

    # ------------------------------------------------------------------
    # Streaming (incremental pagination)
    # ------------------------------------------------------------------

    def stream(self, query: str):
        """
        Executes the query page by page, ``yield``ing a ``pd.DataFrame``
        per page as each request completes.

        Unlike ``sql()``, it doesn't wait for all the data before
        returning the first result — useful for large datasets or for
        showing progress in Jupyter.

        Requires that the function has also been registered via
        ``register_streaming_function()``.

        Limitations
        -----------
        - Supports only one table per query (no JOINs between functions).
        - ``LIMIT N`` and ``WHERE`` are applied **per page** (not globally).
          For a global LIMIT, use ``sql()`` with the desired LIMIT.
        - ``ORDER BY`` and aggregations operate per chunk, not over the total.

        Example
        -------
        ::

            duck.register_api_function("assets", r7.assets)
            duck.register_streaming_function("assets", r7.iter_assets)

            for chunk in duck.stream("SELECT * FROM assets WHERE severity = 'critical'"):
                display(chunk)   # shows up as each page arrives

        Yields
        ------
        pd.DataFrame
            Query result applied on top of each page from the API.
        """
        query = self.resolve_addresses(query)
        pushdown = self._extract_pushdown(query)
        self._check_arguments(query, pushdown)

        for fn_name, iter_fn in self._streaming_functions.items():
            if not re.search(rf"\b{re.escape(fn_name)}\b", query, re.IGNORECASE):
                continue

            # Explicit kwargs from the inline call
            explicit: Dict[str, Any] = {}
            inline_pat = re.compile(
                rf"\b{re.escape(fn_name)}\s*\((.*?)\)",
                flags=re.IGNORECASE | re.DOTALL,
            )
            if m := inline_pat.search(query):
                explicit = self._parse_kwargs(m.group(1))

            # WHERE push-down onto the generator's own parameters (no limit:
            # stream iterates every page)
            kwargs, report = self._plan_call(iter_fn, pushdown, explicit, {fn_name}, allow_limit=False)
            self._log_call(fn_name, kwargs, report)

            # Rewrites the query, replacing func(...) / func with the chunk table name
            chunk_table = f"_stream_{fn_name}"
            chunk_query = inline_pat.sub(chunk_table, query)
            bare_pat = re.compile(
                rf"\b(FROM|JOIN)\s+{re.escape(fn_name)}\b(?!\s*\()",
                flags=re.IGNORECASE,
            )
            chunk_query = bare_pat.sub(rf"\1 {chunk_table}", chunk_query)
            if self._arg_conditions(pushdown):  # arg.x = … are arguments, not columns of the chunk
                chunk_query = self._strip_where_conditions(chunk_query, set(), self.ARG_QUALIFIERS)

            started, chunks, rows_in, rows_out = time.perf_counter(), 0, 0, 0
            for chunk_df in iter_fn(**kwargs):
                if chunk_df.empty:
                    continue
                self.conn.register(chunk_table, chunk_df)
                result = self.conn.sql(chunk_query).df()
                chunks += 1
                rows_in += len(chunk_df)
                rows_out += len(result)
                logger.info("  %s: chunk %d · %s rows read · %s kept · %.1fs elapsed", fn_name, chunks,
                            f"{rows_in:,}", f"{rows_out:,}", time.perf_counter() - started)
                yield result

            return

        raise ValueError(
            f"No streaming function registered for this query.\n"
            f"Use register_streaming_function() to register a generator."
        )

    # ------------------------------------------------------------------

    def close(self) -> None:
        """Closes the DuckDB connection."""
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

"""
Operator-aware push-down: how a ``WHERE`` condition reaches a registered
function's parameters.

The convention extends "parameter name = column name" with suffixes, so
what a connector can do server-side is visible in its signature:

=====================  ============================  ==========================
SQL                    parameter                     connector receives
=====================  ============================  ==========================
``col = v``            ``col``                       ``v`` (equality)
``col LIKE 'p'``       ``col_like`` or ``col_ilike``  the SQL pattern ``'p'``
``col ILIKE 'p'``      ``col_ilike``                 the SQL pattern ``'p'``
``col > v`` (etc.)     ``col_gt`` / ``_gte`` /       ``v``
                       ``_lt`` / ``_lte``
any of the above       ``where``                     ``List[Condition]`` (all)
a join's key values    ``col_in`` / ``where``        a list (``op="in"``): the
                       (``WHERE_OPS`` has ``"in"``)  values the other side has
=====================  ============================  ==========================

- ``col_like``: the server matches *case-sensitively* (exact LIKE).
- ``col_ilike``: the server matches *case-insensitively* — exact for ILIKE,
  a superset for LIKE (DuckDB re-applies the real predicate anyway).
- A LIKE is pushed to a ``_like``/``_ilike`` param only when
  ``parse_like`` can translate it exactly (``'x'``, ``'x%'``, ``'%x'``,
  ``'%x%'`` — no ``_`` wildcard, no inner ``%``, no escapes); anything
  else stays with DuckDB. Connectors turn the pattern into their own
  operator with ``parse_like``/``require_like``.
- ``where`` is for sources that can apply arbitrary conditions natively
  (SQL databases): it gets every simple condition aimed at the function.

DuckDB always re-applies the full ``WHERE`` on what comes back, so a
connector may return a *superset* (looser match) but never a subset.
"""

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

#: Comparison operators and their parameter suffixes.
COMPARISON_SUFFIXES = {"gt": "_gt", "gte": "_gte", "lt": "_lt", "lte": "_lte"}

#: Name of the catch-all parameter receiving every condition.
WHERE_PARAM = "where"


@dataclass(frozen=True)
class Condition:
    """One simple ``WHERE`` condition: ``column <op> value``."""

    column: str
    #: ``eq`` / ``like`` / ``ilike`` / ``gt`` / ``gte`` / ``lt`` / ``lte``; ``in`` (``value`` a tuple) only
    #: comes from a join (``DuckAPI`` pushes the other side's key values) and only reaches a ``col_in``
    #: parameter or the ``where`` of a connector whose ``WHERE_OPS`` include it.
    op: str
    value: Any
    #: Table qualifier as written (``a`` in ``a.col``), lowercased; None if bare.
    table: Optional[str] = None


@dataclass(frozen=True)
class LikePattern:
    #: ``equals`` / ``startswith`` / ``endswith`` / ``contains``.
    kind: str
    text: str


def parse_like(pattern: Any) -> Optional[LikePattern]:
    """
    Translates a SQL LIKE pattern into a plain string operation, or
    ``None`` when that can't be done exactly (``_`` single-char wildcard,
    ``%`` in the middle, backslash escapes, or a match-everything ``%``).
    """
    if not isinstance(pattern, str) or "_" in pattern or "\\" in pattern:
        return None
    starts, ends = pattern.startswith("%"), pattern.endswith("%")
    text = pattern.strip("%")
    if not text or "%" in text:
        return None
    if starts and ends:
        return LikePattern("contains", text)
    if starts:
        return LikePattern("endswith", text)
    if ends:
        return LikePattern("startswith", text)
    return LikePattern("equals", text)


def require_like(pattern: str, param: str = "pattern") -> LikePattern:
    """``parse_like`` for connector code: a pattern it can't push is a caller error."""
    parsed = parse_like(pattern)
    if parsed is None:
        raise ValueError(
            f"{param}={pattern!r}: only 'x', 'x%', '%x' and '%x%' patterns can be pushed "
            f"down (no '_' wildcard or inner '%') — filter in WHERE instead."
        )
    return parsed


_SQL_OPS = {"eq": "=", "like": "LIKE", "ilike": "ILIKE", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "in": "IN"}

#: The operators a ``where`` parameter receives unless its connector declares ``WHERE_OPS``: ``in`` is newer
#: than most ``where`` implementations, so it only goes to one that says it applies it.
DEFAULT_WHERE_OPS = frozenset({"eq", "like", "ilike", "gt", "gte", "lt", "lte"})


def where_ops_of(fetch_function: Any) -> frozenset:
    """The operators a function's ``where`` applies: its connector's ``WHERE_OPS``, else the default ones."""
    owner = getattr(fetch_function, "__self__", None)
    if owner is None and not inspect.isfunction(fetch_function):
        owner = fetch_function  # a callable object (FileTable): the table is the object
    ops = getattr(owner, "WHERE_OPS", None) if owner is not None else None
    return frozenset(ops) if ops else DEFAULT_WHERE_OPS


def _sql_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def conditions_to_sql(conditions: Iterable[Condition], columns: Iterable[str]) -> Optional[str]:
    """
    Renders conditions as a DuckDB ``WHERE`` body over ``columns`` (the
    scanned relation's real columns, matched case-insensitively);
    conditions on other columns are skipped. Identifiers are quoted and
    literals escaped — values never reach the SQL raw.
    """
    by_lower = {c.lower(): c for c in columns}
    parts = []
    for c in conditions:
        column = by_lower.get(c.column.lower())
        if column is None or c.op not in _SQL_OPS:
            continue
        ident = '"' + column.replace('"', '""') + '"'
        if c.op == "in":
            values = list(c.value or ())
            parts.append(f"{ident} IN ({', '.join(_sql_literal(v) for v in values)})" if values else "FALSE")
            continue
        parts.append(f"{ident} {_SQL_OPS[c.op]} {_sql_literal(c.value)}")
    return " AND ".join(parts) if parts else None


#: Optional connector hook: ``blocker(condition) -> None`` when the
#: connector can apply that condition through ``where``, else a short
#: reason it can't (it then stays with DuckDB). Looked up as a method named
#: ``pushdown_blocker`` on the registered bound method's instance.
BLOCKER_HOOK = "pushdown_blocker"


def blocker_of(fetch_function: Any) -> Optional[Callable[[Condition], Optional[str]]]:
    return getattr(getattr(fetch_function, "__self__", None), BLOCKER_HOOK, None)


def assign_conditions(
    params: Iterable[str],
    conditions: Iterable[Condition],
    blocker: Optional[Callable[[Condition], Optional[str]]] = None,
    where_ops: Iterable[str] = DEFAULT_WHERE_OPS,
) -> Dict[Condition, str]:
    """
    Which parameter each condition goes to; conditions left out stay with
    DuckDB. Named parameters come first — an ``eq`` on a structural
    parameter (``WHERE table_name = 'x'``) must fill that parameter even
    when the function also takes ``where`` — and ``where`` gets whatever's
    left, minus any condition the connector's ``blocker`` refuses.
    """
    params = set(params)
    conditions = list(conditions)
    assigned: Dict[Condition, str] = {}
    used: Set[str] = set()
    for c in conditions:
        target = None
        if c.op == "eq":
            target = c.column
        elif c.op in ("like", "ilike") and parse_like(c.value) is not None:
            candidates = [f"{c.column}_like", f"{c.column}_ilike"] if c.op == "like" else [f"{c.column}_ilike"]
            target = next((p for p in candidates if p in params), None)
        elif c.op in COMPARISON_SUFFIXES:
            target = c.column + COMPARISON_SUFFIXES[c.op]
        elif c.op == "in":
            target = c.column + "_in"
        if target and target != WHERE_PARAM and target in params and target not in used:
            assigned[c] = target
            used.add(target)
    if WHERE_PARAM in params:
        where_ops = set(where_ops)
        for c in conditions:
            if c not in assigned and c.op in where_ops and (blocker is None or blocker(c) is None):
                assigned[c] = WHERE_PARAM
    return assigned


def map_conditions(
    params: Iterable[str],
    conditions: Iterable[Condition],
    blocker: Optional[Callable[[Condition], Optional[str]]] = None,
    where_ops: Iterable[str] = DEFAULT_WHERE_OPS,
) -> Tuple[Dict[str, Any], List[Condition]]:
    """
    Maps conditions onto a function's parameters.

    Returns ``(kwargs, consumed)`` — ``consumed`` are the conditions the
    function will apply server-side (every one, when it has ``where``).
    Structural params declared in the signature are just more ``eq``
    targets here; the caller decides what else to pass.
    """
    conditions = list(conditions)
    assigned = assign_conditions(params, conditions, blocker, where_ops)
    consumed = [c for c in conditions if c in assigned]
    kwargs: Dict[str, Any] = {}
    for c, target in assigned.items():
        if target == WHERE_PARAM:
            kwargs[WHERE_PARAM] = [x for x in consumed if assigned[x] == WHERE_PARAM]
        else:
            kwargs[target] = list(c.value) if c.op == "in" else c.value
    return kwargs, consumed


SORTABLE_ATTR = "__duckduck_sortable__"


@dataclass(frozen=True)
class Sorting:
    """What ``@sortable`` declared: the columns (None = any, the connector checks), how many keys, the mode."""

    columns: Optional[frozenset]
    keys: int
    exact: bool

    def takes(self, order_by: List[Tuple[str, bool]]) -> bool:
        return len(order_by) <= self.keys and (self.columns is None
                                               or all(c.lower() in self.columns for c, _ in order_by))


def sortable(*columns: str, keys: Optional[int] = None, exact: bool = False) -> Callable:
    """
    Declares that a table sorts at the source, through an ``order_by: Optional[List[Tuple[str, bool]]] = None``
    parameter receiving ``[(column, descending)]`` (at most ``keys`` of them: 1 for an API, any for exact). Two ways:

    - **pages** (default, an API): pages come back in that order, nothing more promised — DuckAPI reads them
      (``_materialize_pages``) until it holds the top N, NULLs wherever the server puts them. Name the
      ``columns``: only ones the server orders exactly as DuckDB does — dates, timestamps, numbers (text
      follows the database's collation: case, accents, punctuation).
    - **exact** (a query engine): ``order_by`` and ``limit`` arrive together and the source applies both
      exactly as DuckDB would — NULLs last, the same order — or neither (it drops the limit too, e.g. a text
      column whose collation it can't vouch for). No ``columns``: the connector decides per call.
    """
    names = frozenset(c.lower() for c in columns) if columns else None
    keys = keys or (16 if exact else 1)  # a query engine takes a whole ORDER BY; an API one field
    if not exact and names is None:
        raise ValueError("sortable: name the columns the API sorts by (or exact=True for a query engine)")

    def mark(fn: Callable) -> Callable:
        setattr(fn, SORTABLE_ATTR, Sorting(names, int(keys), bool(exact)))
        return fn

    return mark


def sortable_of(fn: Any) -> Optional[Sorting]:
    """What ``@sortable`` says of a table function: a bound method, a saved table, or a callable object."""
    for owner in (fn, getattr(fn, "__func__", None), getattr(type(fn), "__call__", None)):
        found = getattr(owner, SORTABLE_ATTR, None) if owner is not None else None
        if isinstance(found, Sorting):
            return found
    return None


def order_sql(order_by: Optional[List[Tuple[str, bool]]], columns: Iterable[str]) -> Optional[str]:
    """``"a" DESC NULLS LAST, …`` for DuckDB-flavoured SQL (columns matched case-insensitively); None when a
    column isn't there — then neither the order nor the limit may be applied."""
    by_name = {c.lower(): c for c in columns}
    parts = []
    for column, descending in order_by or []:
        real = by_name.get(column.lower())
        if real is None:
            return None
        parts.append('"' + real.replace('"', '""') + '"' + (" DESC" if descending else "") + " NULLS LAST")
    return ", ".join(parts) or None

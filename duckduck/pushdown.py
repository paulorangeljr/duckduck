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
    #: ``eq`` / ``like`` / ``ilike`` / ``gt`` / ``gte`` / ``lt`` / ``lte``.
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


_SQL_OPS = {"eq": "=", "like": "LIKE", "ilike": "ILIKE", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


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
        if target and target != WHERE_PARAM and target in params and target not in used:
            assigned[c] = target
            used.add(target)
    if WHERE_PARAM in params:
        for c in conditions:
            if c not in assigned and (blocker is None or blocker(c) is None):
                assigned[c] = WHERE_PARAM
    return assigned


def map_conditions(
    params: Iterable[str],
    conditions: Iterable[Condition],
    blocker: Optional[Callable[[Condition], Optional[str]]] = None,
) -> Tuple[Dict[str, Any], List[Condition]]:
    """
    Maps conditions onto a function's parameters.

    Returns ``(kwargs, consumed)`` — ``consumed`` are the conditions the
    function will apply server-side (every one, when it has ``where``).
    Structural params declared in the signature are just more ``eq``
    targets here; the caller decides what else to pass.
    """
    conditions = list(conditions)
    assigned = assign_conditions(params, conditions, blocker)
    consumed = [c for c in conditions if c in assigned]
    kwargs: Dict[str, Any] = {}
    for c, target in assigned.items():
        if target == WHERE_PARAM:
            kwargs[WHERE_PARAM] = [x for x in consumed if assigned[x] == WHERE_PARAM]
        else:
            kwargs[target] = c.value
    return kwargs, consumed

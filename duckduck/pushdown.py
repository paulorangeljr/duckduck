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
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

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


def map_conditions(
    params: Iterable[str], conditions: Iterable[Condition]
) -> Tuple[Dict[str, Any], List[Condition]]:
    """
    Maps conditions onto a function's parameters.

    Returns ``(kwargs, consumed)`` — ``consumed`` are the conditions the
    function will apply server-side (every one, when it has ``where``).
    Structural params declared in the signature are just more ``eq``
    targets here; the caller decides what else to pass.
    """
    params: Set[str] = set(params)
    kwargs: Dict[str, Any] = {}
    consumed: List[Condition] = []
    conditions = list(conditions)
    for c in conditions:
        target = None
        if c.op == "eq":
            target = c.column
        elif c.op in ("like", "ilike") and parse_like(c.value) is not None:
            candidates = [f"{c.column}_like", f"{c.column}_ilike"] if c.op == "like" else [f"{c.column}_ilike"]
            target = next((p for p in candidates if p in params), None)
        elif c.op in COMPARISON_SUFFIXES:
            target = c.column + COMPARISON_SUFFIXES[c.op]
        if target and target in params and target not in kwargs:
            kwargs[target] = c.value
            consumed.append(c)
    if WHERE_PARAM in params and conditions:
        kwargs[WHERE_PARAM] = conditions
        consumed = conditions
    return kwargs, consumed

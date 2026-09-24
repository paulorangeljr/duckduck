"""
What each registered name *is*, for ``DuckAPI.list_tables()`` / ``SHOW TABLES``.

Everything registered in DuckAPI is queryable with ``SELECT ... FROM``, but
not everything is a plain table:

- ``table``          — data, selectable as-is: ``SELECT * FROM alerts``.
- ``table function`` — data behind required arguments that say *which*
                       data: ``SELECT * FROM glue_table(database='…', table_name='…')``.
- ``catalog``        — lists what a source contains (tables, columns,
                       databases...) — how you find the arguments for its
                       table functions.
- ``raw query``      — runs a query you write in the source's own
                       language (SQL, KQL).

``table`` vs ``table function`` is inferred from the signature (required
parameters or not). ``catalog`` and ``raw query`` can't be, so connectors
mark those methods with the decorators below.
"""

import inspect
from typing import Any, Callable, List, Optional

TABLE = "table"
TABLE_FUNCTION = "table function"
CATALOG = "catalog"
RAW_QUERY = "raw query"

#: Display order in ``list_tables()``.
ORDER = [TABLE, TABLE_FUNCTION, CATALOG, RAW_QUERY]

_ATTR = "__duckduck_kind__"

#: Optional parameters that take a raw query fragment, not a column value.
_RAW_PARAMS = {"query", "filter"}

_SUFFIXES = [("_ilike", "LIKE/ILIKE"), ("_like", "LIKE"), ("_gte", ">="), ("_gt", ">"), ("_lte", "<="), ("_lt", "<")]


_LISTS_ATTR = "__duckduck_lists__"


def catalog(fn: Optional[Callable] = None, *, lists: Optional[str] = None):
    """
    Marks a method that lists what its source contains. Used bare
    (``@catalog``) or with ``lists=`` — the name of the sibling *table
    function* its rows are arguments for (``@catalog(lists="table")`` on
    ``GlueTable.tables``: each row's ``database``/``table_name`` columns
    are ``GlueTable.table``'s required parameters). That link is what lets
    catalog generation discover every table behind a connector.
    """
    def mark(f: Callable) -> Callable:
        setattr(f, _ATTR, CATALOG)
        if lists:
            setattr(f, _LISTS_ATTR, lists)
        return f

    return mark(fn) if fn is not None else mark


def lists_of(fn: Callable) -> Optional[str]:
    """The table-function method name a catalog's rows feed, if declared."""
    return getattr(fn, _LISTS_ATTR, None)


def raw_query(fn: Callable) -> Callable:
    """Marks a method that runs a query written in the source's own language."""
    setattr(fn, _ATTR, RAW_QUERY)
    return fn


def _params(fn: Callable) -> List[inspect.Parameter]:
    try:
        return [
            p for p in inspect.signature(fn).parameters.values()
            if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return []


def required_params(fn: Callable) -> List[inspect.Parameter]:
    return [p for p in _params(fn) if p.default is inspect.Parameter.empty]


def kind_of(fn: Callable) -> str:
    marked = getattr(fn, _ATTR, None)
    if marked:
        return marked
    return TABLE_FUNCTION if required_params(fn) else TABLE


def _placeholder(p: inspect.Parameter) -> str:
    if p.annotation in (int, float, "int", "float"):
        return f"<{p.name}>"
    return f"'<{p.name}>'"


def usage(name: str, fn: Callable, kind: Optional[str] = None) -> str:
    """A ready-to-edit example query."""
    kind = kind or kind_of(fn)
    required = required_params(fn)
    target = f"{name}({', '.join(f'{p.name}={_placeholder(p)}' for p in required)})" if required else name
    example = f"SELECT * FROM {target}"
    return example + " LIMIT 10" if kind in (TABLE, TABLE_FUNCTION) else example


def pushdown_summary(fn: Callable) -> str:
    """
    Which WHERE conditions / LIMIT the source applies itself, in words
    (everything else still works — DuckDB filters it after fetching).
    """
    params = _params(fn)
    names = {p.name for p in params}
    parts: List[str] = []
    if "where" in names:
        # any condition can reach the source; named params add nothing to say
        parts.append("any column (=, LIKE, <, >...)")
        params = []
    for p in params:
        if p.default is not None or p.name in ("where", "limit") or p.name in _RAW_PARAMS:
            continue  # required/structural, defaulted options, or raw fragments
        for suffix, label in _SUFFIXES:
            if p.name.endswith(suffix):
                parts.append(f"{p.name[: -len(suffix)]} {label}")
                break
        else:
            parts.append(f"{p.name} =")
    if "limit" in names:
        parts.append("LIMIT")
    return ", ".join(parts) if parts else "none — DuckDB filters after fetching"


def describe(name: str, fn: Any) -> dict:
    kind = kind_of(fn)
    return {"kind": kind, "usage": usage(name, fn, kind), "pushdown": pushdown_summary(fn)}

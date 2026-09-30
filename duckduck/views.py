"""
Saved tables: a query you found turns into a table that stays.

Some tables are only known when you query — ``sn_table(table_name='incident')``,
``glue_table(database='security', table_name='proxy_logs')``. A saved table
gives one a name, kept in ``duckduck.json`` under ``views`` and registered by
``auto_register`` like any connector table, so it's queried (and drafted into
the semantic catalog) without knowing what's behind it::

    "views": {
      "sn_incident": {"table": "sn_table", "args": {"table_name": "incident"},
                      "description": "ServiceNow incidents"},
      "open_p1": {"sql": "SELECT number, short_description FROM sn_incident WHERE priority = '1'"}
    }

Two kinds:

- **bound** (``table`` + ``args``): a table function with its structural
  arguments fixed. It *is* that function underneath — WHERE / LIMIT
  push-down, page-by-page reading and the connector's own checks work as
  before.
- **query** (``sql``): any read query over the registered tables, run each
  time the table is read, on a connection with no file or network access.
  A WHERE on it is applied by DuckDB after the query runs.

``view_from_sql`` turns a query into the right kind: ``SELECT * FROM f(a=1)``
or ``SELECT * FROM f WHERE a = 1`` (only the function's required
arguments, all of them) become bound; anything else is a query.
"""

import contextvars
import inspect
import json
import os
import re
import shutil
from typing import Any, Callable, Dict, List, Optional, Tuple

#: A saved table's name: lowercase letters, digits and _.
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
#: What ``service_of`` / the SQL tab call a query-kind saved table's system.
SERVICE = "saved tables"
#: Marks a registered function as a saved table (its definition).
VIEW_ATTR = "__duckduck_view__"

_READ_STATEMENTS = ("select", "with", "from", "show", "describe", "summarize", "explain", "values", "table",
                    "list", "pivot", "unpivot", "(")


def read_only_reason(query: str) -> Optional[str]:
    """Why ``query`` isn't accepted (not a single read statement), or ``None``."""
    text = re.sub(r"(--[^\n]*\n?|/\*.*?\*/)", " ", query or "", flags=re.S).strip()
    if not text:
        return "empty query"
    first = re.match(r"\(|[A-Za-z]+", text)
    if not first or first.group(0).lower() not in _READ_STATEMENTS:
        return "only read queries: SELECT, WITH, FROM, SHOW, DESCRIBE, SUMMARIZE, EXPLAIN, VALUES"
    if len(statements(text)) > 1:
        return "one statement at a time"
    if first.group(0).lower() == "with" and re.search(r"\)\s*(insert|update|delete|merge)\b", text, re.I):
        return "only read queries: this WITH writes"
    return None


def statements(text: str) -> List[str]:
    """Splits on ``;`` outside quotes."""
    parts, buf, quote = [], [], None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


def clean_definition(raw: Any) -> Dict[str, Any]:
    """A definition as stored: ``{table, args}`` or ``{sql}``, plus an optional ``description``. Raises ``ValueError``."""
    if not isinstance(raw, dict):
        raise ValueError("a saved table is an object: {\"table\": ..., \"args\": {...}} or {\"sql\": \"SELECT ...\"}")
    unknown = sorted(set(raw) - {"table", "args", "sql", "description"})
    if unknown:
        raise ValueError(f"unknown key(s) {unknown}: use table + args, or sql (and description)")
    has_table, has_sql = bool(raw.get("table")), bool(raw.get("sql"))
    if has_table == has_sql:
        raise ValueError("give either table (+ args) or sql, not both")
    out: Dict[str, Any] = {}
    if has_table:
        if not isinstance(raw["table"], str):
            raise ValueError("table must be the name of a registered table")
        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("args must be an object: {\"param\": value}")
        out = {"table": raw["table"].lower(), "args": dict(args)}
    else:
        if not isinstance(raw["sql"], str):
            raise ValueError("sql must be text")
        sql = raw["sql"].strip().rstrip(";").strip()
        reason = read_only_reason(sql)
        if reason:
            raise ValueError(f"sql: {reason}")
        out = {"sql": sql}
    if raw.get("description"):
        if not isinstance(raw["description"], str):
            raise ValueError("description must be text")
        out["description"] = raw["description"].strip()
    return out


def kind_of(definition: Dict[str, Any]) -> str:
    return "bound" if definition.get("table") else "query"


_SIMPLE = re.compile(
    r"^\s*select\s+\*\s+from\s+([a-z_][a-z0-9_]*)\s*(?:\((.*)\))?\s*(?:where\s+(.*?))?\s*;?\s*$",
    re.I | re.S,
)


def view_from_sql(duck: Any, sql: str) -> Dict[str, Any]:
    """
    The saved-table definition a query stands for: bound when it's
    ``SELECT * FROM f(args)`` / ``SELECT * FROM f WHERE a = 'x' AND …`` over a
    registered table and fills exactly its required arguments (nothing
    else in the WHERE), else a query.
    """
    sql = (sql or "").strip().rstrip(";").strip()
    m = _SIMPLE.match(sql)
    if m and m.group(1).lower() in duck.functions:
        name = m.group(1).lower()
        fn = duck.functions[name]
        try:
            args = duck._parse_kwargs(m.group(2) or "")
            if m.group(3):
                pushdown = duck._extract_pushdown(sql)
                if not pushdown.complete or any(c.op != "eq" or c.table not in (None, name) for c in pushdown.conditions):
                    raise ValueError
                for c in pushdown.conditions:
                    if c.column in args:
                        raise ValueError
                    args[c.column] = c.value
        except Exception:
            args = None
        if args is not None:
            from .kinds import required_params

            required = {p.name for p in required_params(fn)}
            if args and set(args) == required and not re.search(r"\blimit\b", m.group(3) or "", re.I):
                return {"table": name, "args": args}
    return {"sql": sql}


def suggested_name(duck: Any, definition: Dict[str, Any]) -> str:
    """``sn_table(table_name='incident')`` → ``sn_incident``; a query → ``saved_query``."""
    if definition.get("table"):
        base = re.sub(r"_(table|query)$", "", definition["table"])
        parts = [base] + [str(v) for v in (definition.get("args") or {}).values()]
        name = re.sub(r"[^a-z0-9_]+", "_", "_".join(parts).lower()).strip("_")
    else:
        name = "saved_query"
    name = (name if re.match(r"^[a-z_]", name) else "t_" + name)[:60] or "saved_table"
    candidate, i = name, 2
    while candidate in duck.functions:
        candidate, i = f"{name}_{i}", i + 1
    return candidate


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def check_name(duck: Any, name: str, replace: bool = False) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError(f"{name!r} isn't a table name: letters, digits and _, starting with a letter")
    try:
        reserved = duck.conn.execute(
            "SELECT 1 FROM duckdb_keywords() WHERE keyword_name = ? AND keyword_category = 'reserved'",
            [name]).fetchone()
    except Exception:
        reserved = None
    if reserved:
        raise ValueError(f"{name!r} is a reserved word in SQL — pick another name")
    if name in duck.functions:
        if name not in duck.views:
            raise ValueError(f"{name!r} is already a table ({duck.service_of.get(name) or 'registered'}) — pick another name")
        if not replace:
            raise ValueError(f"{name!r} is already a saved table — pick another name, or replace it")
    return name


def register(duck: Any, name: str, raw: Any, replace: bool = False) -> Dict[str, Any]:
    """Registers a saved table on ``duck`` (its functions, streaming function and service). Returns the definition."""
    definition = clean_definition(raw)
    name = check_name(duck, name, replace)
    if definition.get("table"):
        base_name = definition["table"]
        if base_name == name:
            raise ValueError(f"{name!r} can't be a saved table over itself")
        base = duck.functions.get(base_name)
        if base is None:
            raise LookupError(_missing(duck, base_name))
        fn = _bound(base, definition["args"], name, definition, base_name)
        iter_fn = duck._streaming_functions.get(base_name)
        streaming = None
        if iter_fn is not None and _accepts(iter_fn, definition["args"]):
            streaming = _bound(iter_fn, definition["args"], name, definition, base_name)
        service = duck.service_of.get(base_name) or SERVICE
    else:
        fn, streaming, service = _query(duck, name, definition), None, SERVICE
    duck.functions[name] = fn
    if streaming is not None:
        duck._streaming_functions[name] = streaming
    else:
        duck._streaming_functions.pop(name, None)
    duck.service_of[name] = service
    duck.views[name] = definition
    duck.failed_views.pop(name, None)
    return definition


def unregister(duck: Any, name: str) -> None:
    name = (name or "").lower()
    if name not in duck.views:
        raise KeyError(f"{name!r} isn't a saved table")
    duck.views.pop(name)
    duck.functions.pop(name, None)
    duck._streaming_functions.pop(name, None)
    duck.service_of.pop(name, None)


def register_all(duck: Any, views: Dict[str, Any], on_error: str = "raise") -> List[str]:
    """
    Registers every saved table of a config, in whatever order they
    resolve (a bound table over another saved table waits for it).
    ``on_error="warn"``: one that fails goes to ``duck.failed_views`` with
    the reason and an always-shown ``RuntimeWarning``; the others still
    register. Returns the names registered.
    """
    if not isinstance(views, dict):
        raise ValueError("'views' must be an object: {name: definition}")
    pending = dict(views)
    done: List[str] = []
    while pending:
        moved, waiting = False, {}
        for name, raw in list(pending.items()):
            try:
                register(duck, name, raw, replace=name.lower() in duck.views)
                done.append(name.lower())
            except LookupError as exc:  # the table it's over may be a saved table further down
                waiting[name] = exc
                continue
            except Exception as exc:
                _fail(duck, name, exc, on_error)
            pending.pop(name)
            moved = True
        if not moved:  # nothing resolved this round: the rest never will
            for name in pending:
                _fail(duck, name, waiting[name], on_error)
            break
    return done


def _fail(duck: Any, name: str, exc: Exception, on_error: str) -> None:
    import warnings

    if on_error != "warn":
        raise ValueError(f"saved table '{name}': {exc}") from exc
    duck.failed_views[name.lower()] = f"{exc.__class__.__name__}: {exc}"
    with warnings.catch_warnings():
        warnings.simplefilter("always", RuntimeWarning)
        warnings.warn(f"saved table '{name}' skipped: {exc}", RuntimeWarning, stacklevel=3)


def _missing(duck: Any, table: str) -> str:
    for service, info in (getattr(duck, "failed_services", None) or {}).items():
        prefix = info.get("prefix") or service
        if table == prefix.lower() or table.startswith(prefix.lower() + "_"):
            return f"table '{table}' isn't registered: service '{service}' didn't start ({info.get('error')})"
    return f"table '{table}' isn't registered"


def _accepts(fn: Callable, args: Dict[str, Any]) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return all(k in params for k in args) or any(p.kind == p.VAR_KEYWORD for p in params.values())


def _bound(base: Callable, args: Dict[str, Any], name: str, definition: Dict[str, Any], base_name: str) -> Callable:
    """``base`` with ``args`` fixed: same parameters otherwise, so push-down sees what it always saw."""
    sig = inspect.signature(base)
    unknown = [k for k in args if k not in sig.parameters]
    if unknown and not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        raise ValueError(f"'{base_name}' has no parameter(s) {unknown} — it takes {list(sig.parameters)}")

    def saved_table(**kwargs):
        return base(**args, **kwargs)

    saved_table.__signature__ = sig.replace(parameters=[p for n, p in sig.parameters.items() if n not in args])
    saved_table.__name__ = saved_table.__qualname__ = name
    saved_table.__module__ = getattr(base, "__module__", None) or __name__
    call = ", ".join(f"{k}={v!r}" for k, v in args.items())
    saved_table.__doc__ = definition.get("description") or f"Saved table: {base_name}({call})."
    owner = getattr(base, "__self__", None)
    if owner is not None:
        saved_table.__self__ = owner  # the connector: its pushdown_blocker and endpoint still apply
    setattr(saved_table, VIEW_ATTR, definition)
    return saved_table


#: The saved queries running in this context — one that reads itself would never end.
_RUNNING: contextvars.ContextVar[Tuple[str, ...]] = contextvars.ContextVar("duckduck_saved_running", default=())


def _query(duck: Any, name: str, definition: Dict[str, Any]) -> Callable:
    sql = definition["sql"]

    def saved_table(limit: Optional[int] = None):
        running = _RUNNING.get()
        if name in running:
            raise ValueError(f"saved table '{name}' reads itself ({' → '.join(running + (name,))})")
        token = _RUNNING.set(running + (name,))
        try:
            return run_query(duck, sql, limit)
        finally:
            _RUNNING.reset(token)

    saved_table.__name__ = saved_table.__qualname__ = name
    saved_table.__module__ = __name__
    saved_table.__doc__ = definition.get("description") or f"Saved query: {' '.join(sql.split())[:200]}"
    setattr(saved_table, VIEW_ATTR, definition)
    return saved_table


def run_query(duck: Any, sql: str, limit: Optional[int] = None) -> Any:
    """
    Runs a saved query on a connection of its own — the registered tables
    shared, no file or network access (the SQL tab's lock), so a saved
    table can't read what the console couldn't.
    """
    from .core import DuckAPI

    reason = read_only_reason(sql)
    if reason:
        raise ValueError(reason)
    runner = DuckAPI(stream_pages=getattr(duck, "stream_pages", True))
    runner.functions = duck.functions
    runner._streaming_functions = duck._streaming_functions
    runner.service_of = duck.service_of
    runner.failed_services = duck.failed_services
    runner.views, runner.failed_views = duck.views, duck.failed_views
    try:
        runner.conn.execute("SET enable_external_access = false")
        runner.conn.execute("SET lock_configuration = true")
        query = sql if limit is None else f"SELECT * FROM ({sql}) AS _saved LIMIT {int(limit)}"
        return runner.sql(query).df()
    finally:
        runner.conn.close()


# ---------------------------------------------------------------------------
# duckduck.json
# ---------------------------------------------------------------------------


def _read(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} isn't a JSON object")
    return data


def _write(path: str, data: Dict[str, Any]) -> None:
    shutil.copyfile(path, path + ".bak")
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def save(path: str, name: str, definition: Dict[str, Any]) -> None:
    """Writes one saved table into the config file's ``views`` (keeps ``<path>.bak``)."""
    data = _read(path)
    views = data.get("views") if isinstance(data.get("views"), dict) else {}
    data["views"] = {**views, name: definition}
    _write(path, data)


def remove(path: str, name: str) -> bool:
    """Removes one from the config file; False when it wasn't there."""
    data = _read(path)
    views = data.get("views")
    if not isinstance(views, dict) or name not in views:
        return False
    views.pop(name)
    if not views:
        data.pop("views")
    _write(path, data)
    return True


def statement(definition: Dict[str, Any]) -> str:
    """The query a saved table stands for — what the page shows and edits (``sn_table(table_name='incident')`` …)."""
    if definition.get("table"):
        call = ", ".join(f"{k}={_literal(v)}" for k, v in (definition.get("args") or {}).items())
        return f"SELECT * FROM {definition['table']}({call})"
    return definition.get("sql") or ""


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def describe(duck: Any, name: str) -> Dict[str, Any]:
    """One saved table for the page: name, kind, definition, its statement, service."""
    d = duck.views.get(name) or {}
    return {"name": name, "kind": kind_of(d), "table": d.get("table"), "args": d.get("args"), "sql": d.get("sql"),
            "description": d.get("description"), "service": duck.service_of.get(name), "statement": statement(d)}

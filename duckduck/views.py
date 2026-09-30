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

#: A saved table's name: lowercase letters, digits and _ …
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
#: … or an address, like every connector's tables: ``s3_data.accountable_cyber.sharepoint_lists``
#: (a part that isn't a plain name in double quotes: ``adls.raw."events 2026"``).
_PART = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")'
DOTTED_RE = re.compile(rf"^[A-Za-z_][A-Za-z0-9_]*(?:\.{_PART}){{1,4}}$")


def is_dotted(name: str) -> bool:
    return "." in (name or "")


def canonical_name(name: str) -> str:
    """How a name is kept: a plain one lowercased, an address as written (its connector lowercased)."""
    name = (name or "").strip()
    if not is_dotted(name):
        return name.lower()
    head, _, rest = name.partition(".")
    return f"{head.lower()}.{rest}"


def function_key(name: str) -> str:
    """The registered table behind a name: itself, or an address's parts joined by _ (``s3_data_accountable_cyber_sharepoint_lists``)."""
    name = canonical_name(name)
    if not is_dotted(name):
        return name
    parts = [p[1:-1].replace('""', '"') if p.startswith('"') else p for p in re.findall(_PART, name)]
    key = re.sub(r"[^a-z0-9_]+", "_", "_".join(parts).lower()).strip("_")
    return (key if re.match(r"^[a-z_]", key) else "t_" + key)[:120]


def name_of(duck: Any, key: str) -> Optional[str]:
    """The saved table a registered name is (its name as saved), or None."""
    return (getattr(duck, "view_key", None) or {}).get(key)
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
    r"^\s*select\s+\*\s+from\s+([a-z_][a-z0-9_]*)\s*(?:\((.*)\))?(?:\s+as\s+[a-z_][a-z0-9_]*)?\s*(?:where\s+(.*?))?\s*;?\s*$",
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
    m = _SIMPLE.match(duck.resolve_addresses(sql) if hasattr(duck, "resolve_addresses") else sql)
    if m and m.group(1).lower() in duck.functions:
        name = m.group(1).lower()
        fn = duck.functions[name]
        try:
            args = duck._parse_kwargs(m.group(2) or "")
            if m.group(3):
                pushdown = duck._extract_pushdown(m.group(0))
                if not pushdown.complete or any(c.op != "eq" or c.table not in (None, name, "arg", "args")
                                                for c in pushdown.conditions):
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
    """By address when the table has one, like every connector's tables — ``s3_data_table(database='a',
    table_name='b')`` → ``s3_data.a.b``, ``sn_table(table_name='incident')`` → ``sn.incident`` —; else a plain
    name (``sn_incident``); a query → ``saved_query``."""
    if definition.get("table") and hasattr(duck, "address_of"):
        address = duck.address_of(definition["table"], definition.get("args"))
        if address and address not in duck.views and function_key(address) not in duck.functions:
            return canonical_name(address)
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


def _split_address(name: str) -> List[str]:
    """``s3_data.db."a b"`` → ``['s3_data', 'db', 'a b']``."""
    return [p[1:-1].replace('""', '"') if p.startswith('"') else p
            for p in re.findall(r'"(?:[^"]|"")+"|[^.]+', name)]


def suggested_place(duck: Any, definition: Dict[str, Any], sql: str = "") -> Dict[str, Any]:
    """
    Where a new saved table goes: ``{connector, database, name}`` — joined, its
    name (``connector.database.name``, ``connector.name``, or a plain ``name``).
    A table over a table function → its address (``s3_data.security.proxy_logs``);
    a saved query → the connector and database it reads from (its first table
    written by address, else the connector of its first registered table),
    named after that table + ``_view`` (so it never takes the table's own address).
    """
    from .addresses import _REF, _masked, _parts, services

    known = services(duck)
    connector = database = None
    if definition.get("table"):
        name = suggested_name(duck, definition)
        parts = _split_address(name) if is_dotted(name) else [name]
        if len(parts) > 1 and parts[0].lower() in known:
            return {"connector": parts[0].lower(), "database": ".".join(parts[1:-1]) or None, "name": parts[-1]}
        return {"connector": None, "database": None, "name": name}
    base = "saved_query"
    masked = _masked(sql or "")
    for m in _REF.finditer(masked):
        parts = _parts(m.group(3))
        if parts[0].lower() in known:
            connector, base = parts[0].lower(), parts[-1]
            database = ".".join(parts[1:-1]) or None
            break
    else:
        try:
            import sqlglot
            from sqlglot import exp

            tables = [t for t in sqlglot.parse_one(sql, dialect="duckdb").find_all(exp.Table)] if sql else []
        except Exception:
            tables = []
        for t in tables:
            fn = (t.name or (t.this.name if isinstance(t.this, exp.Func) else "")).lower()
            svc = duck.service_of.get(fn)
            if fn in duck.functions:
                prefix = known.get(svc, "") if svc in known else ""
                connector = svc if svc in known else None
                base = fn[len(prefix) + 1:] if prefix and fn.startswith(prefix + "_") else fn
                break
    base = re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_") or "saved_query"
    base = (base if re.match(r"^[A-Za-z_]", base) else "t_" + base) + "_view"
    name, i = base, 2
    while True:
        full = ".".join(p for p in (connector, database, name) if p)
        if full not in getattr(duck, "views", {}) and function_key(full) not in duck.functions:
            break
        name, i = f"{base}_{i}", i + 1
    return {"connector": connector, "database": database, "name": name}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def check_name(duck: Any, name: str, replace: bool = False) -> str:
    name = canonical_name(name)
    if is_dotted(name):
        if not DOTTED_RE.match(name):
            raise ValueError(f"{name!r} isn't a table address: connector.table or connector.database.table "
                             f"(a part that isn't a plain name in double quotes)")
    elif not NAME_RE.match(name):
        raise ValueError(f"{name!r} isn't a table name: letters, digits and _, starting with a letter "
                         f"— or an address, connector.database.table")
    key = function_key(name)
    try:
        reserved = duck.conn.execute(
            "SELECT 1 FROM duckdb_keywords() WHERE keyword_name = ? AND keyword_category = 'reserved'",
            [key]).fetchone()
    except Exception:
        reserved = None
    if reserved:
        raise ValueError(f"{name!r} is a reserved word in SQL — pick another name")
    owner = name_of(duck, key)
    if key in duck.functions and owner is None:
        raise ValueError(f"{name!r} is already a table ({duck.service_of.get(key) or 'registered'}) — pick another name")
    if owner is not None and owner != name:
        raise ValueError(f"{name!r} would be the same table as the saved table {owner!r} — pick another name")
    if owner is not None and not replace:
        raise ValueError(f"{name!r} is already a saved table — pick another name, or replace it")
    return name


def register(duck: Any, name: str, raw: Any, replace: bool = False) -> Dict[str, Any]:
    """Registers a saved table on ``duck`` (its functions, streaming function and service). Returns the definition."""
    definition = clean_definition(raw)
    name = check_name(duck, name, replace)
    key = function_key(name)
    if definition.get("table"):
        base_name = definition["table"]
        if base_name == key:
            raise ValueError(f"{name!r} can't be a saved table over itself")
        base = duck.functions.get(base_name)
        if base is None:
            raise LookupError(_missing(duck, base_name))
        fn = _bound(base, definition["args"], key, definition, base_name)
        iter_fn = duck._streaming_functions.get(base_name)
        streaming = None
        if iter_fn is not None and _accepts(iter_fn, definition["args"]):
            streaming = _bound(iter_fn, definition["args"], key, definition, base_name)
        service = duck.service_of.get(base_name) or SERVICE
    else:
        fn, streaming, service = _query(duck, key, definition), None, SERVICE
    duck.functions[key] = fn
    if streaming is not None:
        duck._streaming_functions[key] = streaming
    else:
        duck._streaming_functions.pop(key, None)
    duck.service_of[key] = service
    duck.views[name] = definition
    duck.view_key[key] = name
    duck.failed_views.pop(name, None)
    return definition


def unregister(duck: Any, name: str) -> None:
    name = canonical_name(name)
    name = name if name in duck.views else (name_of(duck, function_key(name)) or name)
    if name not in duck.views:
        raise KeyError(f"{name!r} isn't a saved table")
    key = function_key(name)
    duck.views.pop(name)
    duck.view_key.pop(key, None)
    duck.functions.pop(key, None)
    duck._streaming_functions.pop(key, None)
    duck.service_of.pop(key, None)


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
                register(duck, name, raw, replace=canonical_name(name) in duck.views)
                done.append(canonical_name(name))
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
    duck.failed_views[canonical_name(name)] = f"{exc.__class__.__name__}: {exc}"
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
    runner.view_key = getattr(duck, "view_key", {})
    runner.service_prefix = getattr(duck, "service_prefix", {})
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


def save_many(path: str, definitions: Dict[str, Dict[str, Any]]) -> None:
    """Writes several saved tables at once (one ``.bak``, one write)."""
    data = _read(path)
    views = data.get("views") if isinstance(data.get("views"), dict) else {}
    data["views"] = {**views, **definitions}
    _write(path, data)


def rename_many(path: str, renames: Dict[str, str]) -> None:
    """Renames several saved tables in the config file at once, keeping their order (one ``.bak``, one write)."""
    data = _read(path)
    views = data.get("views") if isinstance(data.get("views"), dict) else {}
    data["views"] = {renames.get(k, k): v for k, v in views.items()}
    _write(path, data)


def moved_name(duck: Any, name: str, database: Optional[str], connector: Optional[str] = None) -> str:
    """
    The name a saved table gets in another database of its connector:
    ``s3_data.sec.proxy`` → ``s3_data.reports.proxy``; an empty database (or
    ``default`` where the connector has no databases) → ``connector.table``.
    A plain name needs ``connector``.
    """
    from .addresses import _ident, _required, default_database, generic_function, services

    parts = _split_address(name) if is_dotted(name) else [name]
    known = services(duck)
    service = (connector or (parts[0] if len(parts) > 1 else "")).lower()
    if service not in known:
        raise ValueError(f"{name}: pick a connector to put it in one of its databases")
    db = (database or "").strip()
    table = generic_function(duck, service)
    has_databases = table is not None and len(_required(duck, table)) >= 2
    if db.lower() == default_database(duck).lower() and not has_databases:
        db = ""
    segments = [service] + [_ident(duck, p) for p in db.split(".") if p.strip()] + [_ident(duck, parts[-1])]
    return canonical_name(".".join(segments))


def move(duck: Any, path: str, names: List[str], database: Optional[str], connector: Optional[str] = None,
         taken: Optional[Callable[[str, Dict[str, Any]], Optional[str]]] = None) -> Dict[str, Any]:
    """
    Moves saved tables to ``database`` (renames them there), one write to the file.
    ``taken(new_name, definition)``: why a name can't be used, or None (the page's
    check against the tables behind a catalog). Returns ``{moved: [{from, to}], skipped: [{name, why}]}``.
    """
    renames: Dict[str, str] = {}
    skipped: List[Dict[str, str]] = []
    for name in names:
        name = canonical_name(str(name))
        if name not in duck.views:
            skipped.append({"name": name, "why": "not a saved table"})
            continue
        try:
            new = moved_name(duck, name, database, connector)
        except ValueError as exc:
            skipped.append({"name": name, "why": str(exc)})
            continue
        if new == name:
            skipped.append({"name": name, "why": "already there"})
            continue
        definition = dict(duck.views[name])
        why = (taken(new, definition) if taken else None) or (
            f"{new} is already a saved table" if new in duck.views or new in renames.values() else None)
        if why:
            skipped.append({"name": name, "why": why})
            continue
        unregister(duck, name)
        try:
            register(duck, new, definition)
        except (ValueError, LookupError) as exc:
            register(duck, name, definition)
            skipped.append({"name": name, "why": str(exc).strip("'\"")})
            continue
        renames[name] = new
    if renames:
        try:
            rename_many(path, renames)
        except Exception:
            for old, new in renames.items():  # the file wasn't written: back as they were
                definition = dict(duck.views[new])
                unregister(duck, new)
                register(duck, old, definition)
            raise
    return {"moved": [{"from": a, "to": b} for a, b in renames.items()], "skipped": skipped}


def saved_as(duck: Any, table: str, args: Optional[Dict[str, Any]]) -> Optional[str]:
    """The saved table that is exactly this table with these arguments, if one is."""
    want = {k: str(v) for k, v in (args or {}).items()}
    for name, d in duck.views.items():
        if d.get("table") == table and {k: str(v) for k, v in (d.get("args") or {}).items()} == want:
            return name
    return None


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


def statement(definition: Dict[str, Any], duck: Any = None) -> str:
    """The query a saved table stands for — what the page shows and edits: by address when it has one
    (``SELECT * FROM sn.incident``), else its arguments as ``arg.`` (``SELECT * FROM sn_table WHERE
    arg.table_name = 'incident'``), or the saved query."""
    if definition.get("table"):
        address = duck.address_of(definition["table"], definition.get("args")) if duck is not None else None
        if address:
            return f"SELECT * FROM {address}"
        args = definition.get("args") or {}
        if not args:
            return f"SELECT * FROM {definition['table']}"
        # the arguments said as arguments: arg.x, never a column
        return (f"SELECT * FROM {definition['table']}\nWHERE "
                + "\n  AND ".join(f"arg.{k} = {_literal(v)}" for k, v in args.items()))
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
    """One saved table for the page: name, the registered table behind it, kind, definition, its statement, service."""
    d = duck.views.get(name) or {}
    key = function_key(name)
    return {"name": name, "key": key, "kind": kind_of(d), "table": d.get("table"), "args": d.get("args"),
            "sql": d.get("sql"), "description": d.get("description"), "service": duck.service_of.get(key),
            "statement": statement(d, duck)}

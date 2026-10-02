"""
Tables by address: ``connector.table`` or ``connector.database.table``.

Every table a connector gives can be written as ``<service>.<table>`` —
``nvd.cves`` is ``nvd_cves`` — and the tables *behind* a connector (the
ones its table function reads by name) as ``<service>.<part>[.<part>]``,
the parts filling that function's required arguments in order::

    SELECT * FROM s3_data.security.proxy_logs     -- s3_data_table(database='security', table_name='proxy_logs')
    SELECT * FROM sn.incident                     -- sn_table(table_name='incident')
    SELECT * FROM sqlserver.dbo.Customers         -- sqlserver_table(table_name='dbo.Customers')  (one argument: the rest, joined)
    SELECT * FROM adx.ProxyLogs                   -- adx_table(table_name='ProxyLogs')           (case kept)

``DuckAPI.sql()`` rewrites them into those calls before anything else, so
push-down is exactly the call's. Only the names after ``FROM`` / ``JOIN``
whose first part is an ``auto_register`` service are touched —
``information_schema.tables`` and the like stay DuckDB's. A reference with
no alias gets its last part as one (``… FROM nvd.cves WHERE cves.id = …``).
A part that isn't a plain identifier is double-quoted:
``adls.raw."events/2026.parquet"``.

The table function behind a service: the one its catalog lists (``@catalog(lists=...)``),
else ``<prefix>_table``, else its only table function.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_REF = re.compile(
    rf"\b(from|join)(\s+)({_IDENT}(?:\s*\.\s*{_IDENT}){{1,4}})(?![A-Za-z0-9_$\"]|\s*[.(])",
    re.IGNORECASE,
)
_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: services that aren't connectors: no address of their own
_NOT_SERVICES = {"saved tables", "taken over"}


def _masked(query: str) -> str:
    """The query with string literals and comments blanked out (same length), so they're never read as names."""
    out, i, n = list(query), 0, len(query)
    while i < n:
        ch = query[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if query[j] == "'" and j + 1 < n and query[j + 1] == "'":
                    j += 2
                    continue
                if query[j] == "'":
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                out[k] = " "
            i = j + 1
        elif query.startswith("--", i):
            j = query.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif query.startswith("/*", i):
            j = query.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _parts(ref: str) -> List[str]:
    return [p[1:-1].replace('""', '"') if p.startswith('"') else p
            for p in re.findall(_IDENT, ref)]


def services(duck: Any) -> Dict[str, str]:
    """Service name → its table prefix ("" for bare names), for every service that has (or had) tables."""
    out: Dict[str, str] = {}
    for svc in set(duck.service_of.values()) - _NOT_SERVICES:
        out[svc] = svc
    for svc, info in (getattr(duck, "failed_services", None) or {}).items():
        out[svc] = str(info.get("prefix", svc))
    out.update(getattr(duck, "service_prefix", None) or {})
    return out


def generic_function(duck: Any, service: str) -> Optional[str]:
    """The table function a service reads its tables by name with, or None."""
    from .common.kinds import CATALOG, TABLE_FUNCTION, kind_of, lists_of

    saved = getattr(duck, "view_key", {}) or {}
    mine = {n: f for n, f in duck.functions.items() if duck.service_of.get(n) == service and n not in saved}
    for fn in mine.values():
        method = lists_of(fn)
        if method and kind_of(fn) == CATALOG:
            sibling = duck.sibling_function(fn, method)
            if sibling:
                return sibling
    prefix = services(duck).get(service, service)
    own = f"{prefix}_table" if prefix else "table"
    if own in mine and kind_of(mine[own]) == TABLE_FUNCTION:
        return own
    functions = [n for n, f in mine.items() if kind_of(f) == TABLE_FUNCTION]
    return functions[0] if len(functions) == 1 else None


def _required(duck: Any, name: str) -> List[str]:
    from .common.kinds import required_params

    return [p.name for p in required_params(duck.functions[name])]


def resolve(duck: Any, parts: List[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """``[service, part, …]`` → (registered table, its arguments), None when the first part isn't a service. Raises ``ValueError`` when it is but the rest can't be read."""
    saved = _saved_named(duck, parts)  # a saved table named by this address is that table (as written…
    parts = _without_default(duck, parts)
    saved = saved or _saved_named(duck, parts)  # …or without its connection's own database)
    if saved is not None:
        return saved, {}
    known = services(duck)
    service = parts[0].lower()
    if service not in known or len(parts) < 2:
        return None
    written = ".".join(parts)
    failed = (getattr(duck, "failed_services", None) or {}).get(service)
    if failed:
        raise ValueError(f"'{written}': service '{service}' didn't start ({failed.get('error')})")
    prefix, rest = known[service], parts[1:]
    if len(rest) == 1:
        direct = f"{prefix}_{rest[0]}".lower() if prefix else rest[0].lower()
        if direct in duck.functions:
            required = _required(duck, direct)
            if not required:
                return direct, {}
            if len(required) > 1 or generic_function(duck, service) != direct:
                raise ValueError(f"'{written}' needs {', '.join(required)}: write {direct}({', '.join(r + '=…' for r in required)})")
    table = generic_function(duck, service)
    if table is None:
        tables = sorted(n for n, s in duck.service_of.items() if s == service)
        raise ValueError(f"'{written}': {service} has no table '{'.'.join(rest)}' "
                         f"(its tables: {', '.join(tables) or 'none'})")
    required = _required(duck, table)
    if len(rest) < len(required):
        raise ValueError(f"'{written}': a table of {service} is written "
                         f"{service}.{'.'.join('<' + r + '>' for r in required)}")
    head = required[:-1]
    args = dict(zip(head, rest[:len(head)]))
    args[required[-1]] = ".".join(rest[len(head):])  # the last one takes whatever's left: dbo.Customers
    return table, args


#: The database the SQL tab lists a connector's tables with no database of their own under — unless
#: ``duck.default_database`` says another (the config file's top-level ``"default_database"``).
#: Not "default": Glue and Hive have a real database called that.
DEFAULT_DATABASE = "duckdefault"


def default_database(duck: Any) -> str:
    """The name the tables with no database of their own are listed under: ``duck.default_database``, else ``duckdefault``."""
    name = getattr(duck, "default_database", None)
    return str(name).strip() if isinstance(name, str) and name.strip() else DEFAULT_DATABASE


def native_database(duck: Any, table: str) -> Optional[str]:
    """
    The database a connector's table function reads from when its arguments don't
    say one — the connection's own: ADX's ``database``, a SQL connection's
    (``engine.url.database``; an SQLite file's name). None when there's none.
    """
    import os

    fn = duck.functions.get(table)
    instance = getattr(fn, "__self__", None)
    if instance is None:
        return None
    db = getattr(instance, "database", None)
    if isinstance(db, str) and db.strip():
        return db.strip()
    url = getattr(getattr(instance, "engine", None), "url", None)
    name = getattr(url, "database", None)
    if not isinstance(name, str) or not name.strip() or name == ":memory:":
        return None
    if str(getattr(url, "drivername", "")).startswith("sqlite"):
        name = os.path.splitext(os.path.basename(name))[0]
    return name or None


def database_of_service(duck: Any, service: str) -> Optional[str]:
    """The native database of a service whose table function takes only the table's name (ADX, SQL)."""
    table = generic_function(duck, service)
    if table is None or len(_required(duck, table)) != 1:
        return None
    return native_database(duck, table)


def _without_default(duck: Any, parts: List[str]) -> List[str]:
    """
    ``svc.default.x`` → ``svc.x`` for a connector whose tables have no database
    (``sn.default.incident`` is ``sn.incident``, ``nvd.default.cves`` is ``nvd.cves``),
    and ``svc.<its database>.x`` → ``svc.x`` where the connection has one of its own
    (``adx.SecurityDb.ProxyLogs``, ``mysql.shop.orders``). Where the connector's tables
    do have databases (a table function taking one before the name — Glue, a
    lakehouse), ``default`` is read as a real database of that name.
    """
    if len(parts) < 3 or parts[0].lower() not in services(duck):
        return parts
    service = parts[0].lower()
    stripped = [parts[0]] + parts[2:]
    table = generic_function(duck, service)
    is_default = parts[1].lower() == default_database(duck).lower()
    if table is not None and len(_required(duck, table)) >= 2:
        # the connector's tables have databases: the default one only names its own tables (s3_data.duckdefault.tables),
        # anything else is a real database of that name
        if is_default and len(stripped) == 2:
            prefix = services(duck).get(service, service)
            direct = f"{prefix}_{stripped[1]}".lower() if prefix else stripped[1].lower()
            if direct in duck.functions and not _required(duck, direct):
                return stripped
        return parts
    native = database_of_service(duck, service)
    if is_default or (native and parts[1].lower() == native.lower()):
        return stripped
    return parts


def _saved_named(duck: Any, parts: List[str]) -> Optional[str]:
    """The registered table of a saved table whose name is this address (case aside), or None. The default
    database doesn't count on either side: ``sharepoint.duckdefault.x`` and ``sharepoint.x`` name one table."""
    default = default_database(duck).lower()

    def plain(p: List[str]) -> str:
        p = list(p)
        if len(p) >= 3 and p[1].lower() == default:
            p = [p[0]] + p[2:]
        return ".".join(p).lower()

    wanted = plain(parts)
    for key, name in (getattr(duck, "view_key", None) or {}).items():
        if "." in name and plain(_parts(name)) == wanted:
            return key
    return None


def resolve_addresses(duck: Any, query: str) -> str:
    """Every ``service.table`` / ``service.database.table`` after FROM / JOIN rewritten into its table (or call)."""
    if "." not in query:
        return query
    masked = _masked(query)
    edits: List[Tuple[int, int, str]] = []
    for m in _REF.finditer(masked):
        parts = _parts(m.group(3))
        found = resolve(duck, parts)
        if found is None:
            continue
        table, args = found
        text = table if not args else f"{table}({', '.join(f'{k}={_literal(v)}' for k, v in args.items())})"
        alias = parts[-1].lower()
        if (duck._alias_at(masked, m.end()) is None and _PLAIN.match(alias) and alias != table
                and alias not in duck._NOT_ALIASES and not _reserved(duck, alias)):
            text += f" AS {alias}"
        edits.append((m.start(3), m.end(3), text))
    for start, end, text in reversed(edits):
        query = query[:start] + text + query[end:]
    return query


def address_of(duck: Any, table: str, args: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """How a table (or a call of a service's table function) is written by address — only when it reads back the same."""
    saved = (getattr(duck, "view_key", None) or {}).get(table)
    if not args and saved and "." in saved:
        return saved  # a saved table named by its address
    service = duck.service_of.get(table)
    known = services(duck)
    if service is None or service not in known:
        return None
    prefix = known[service]
    if not args:
        if prefix and table.startswith(prefix + "_"):
            segments = [[table[len(prefix) + 1:]]]
        elif not prefix:
            segments = [[table]]
        else:
            return None
    else:
        if generic_function(duck, service) != table:
            return None
        required = _required(duck, table)
        if set(args) != set(required):
            return None
        values = [str(args[r]) for r in required]
        last = values[-1].split(".")  # the last part may be written dotted: sqlserver.dbo.Customers
        segments = [[v] for v in values[:-1]] + [last if all(last) else [values[-1]]]
        native = native_database(duck, table) if len(required) == 1 else None
        if native:  # the connection's own database, like every connector.database.table
            segments = [[native]] + segments
    address = ".".join([_ident(duck, service)] + [".".join(_ident(duck, x) for x in seg) for seg in segments])
    try:
        back = resolve(duck, _parts(address))
    except ValueError:
        return None
    expected = {k: str(v) for k, v in (args or {}).items()}
    if back is None or back[0] != table or {k: str(v) for k, v in back[1].items()} != expected:
        return None
    return address


def address_pattern(duck: Any, table: str) -> Optional[str]:
    """``s3_data.<database>.<table_name>`` for a service's table function; None otherwise."""
    service = duck.service_of.get(table)
    if service is None or service not in services(duck) or generic_function(duck, service) != table:
        return None
    required = _required(duck, table)
    native = native_database(duck, table) if len(required) == 1 else None
    return ".".join([_ident(duck, service)] + ([_ident(duck, native)] if native else []) + [f"<{r}>" for r in required])


def _ident(duck: Any, part: str) -> str:
    return part if _PLAIN.match(part) and not _reserved(duck, part.lower()) else _quoted(part)


def _quoted(part: str) -> str:
    return '"' + part.replace('"', '""') + '"'


def _literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


_KEYWORDS: Optional[set] = None


def _reserved(duck: Any, word: str) -> bool:
    global _KEYWORDS
    if _KEYWORDS is None:
        try:
            _KEYWORDS = {r[0] for r in duck.conn.execute(
                "SELECT keyword_name FROM duckdb_keywords() WHERE keyword_category = 'reserved'").fetchall()}
        except Exception:
            return False
    return word in _KEYWORDS

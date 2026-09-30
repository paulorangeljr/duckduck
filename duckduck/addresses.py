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
    from .kinds import CATALOG, TABLE_FUNCTION, kind_of, lists_of

    views = getattr(duck, "views", {}) or {}
    mine = {n: f for n, f in duck.functions.items() if duck.service_of.get(n) == service and n not in views}
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
    from .kinds import required_params

    return [p.name for p in required_params(duck.functions[name])]


def resolve(duck: Any, parts: List[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """``[service, part, …]`` → (registered table, its arguments), None when the first part isn't a service. Raises ``ValueError`` when it is but the rest can't be read."""
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
    return ".".join([_ident(duck, service)] + [f"<{r}>" for r in _required(duck, table)])


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

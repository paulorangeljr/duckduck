"""
KQL (Kusto Query Language) as another way to query DuckAPI's tables.

The translation is the ``kql`` DuckDB extension built from
https://github.com/saoc90/kql-to-sql (the official Kusto parser compiled to
a native extension): ``kql_to_sql(kql)`` gives DuckDB SQL, which then runs
through ``DuckAPI.sql()`` like any query — addresses, push-down, joins across
sources::

    adx.ProxyLogs
    | where Timestamp > ago(1d) and Host contains 'web'
    | join kind=inner (sn.incident | project Host = cmdb_ci, number) on Host
    | take 100

Before translating, the KQL is read for DuckAPI's own table references
(the Kusto parser doesn't know them) and each becomes a placeholder, put
back into the SQL afterwards:

- addresses — ``adx.ProxyLogs``, ``s3_data.security.proxy_logs`` — or the
  same bracket-quoted, ``['adx.Proxy Logs']``;
- calls of a table function — ``sn_table(table_name='incident')`` or
  positional, ``sn_table('incident')``;
- plain registered names (``nvd_cves``) need nothing.

and the SQL is adjusted where the translation would lose push-down or be
wrong: ``ago()``/``now()`` become the timestamp they mean (the extension
drops ``now(-1h)``'s offset), ``=~`` becomes an exact ``ILIKE``,
``arg.x`` / ``arg['x']`` stay ``arg.x`` (an argument, never a column), and a
table read twice gets an alias per read, so each read gets its own filters.

**Native ADX.** When every table the KQL reads is one Azure Data Explorer
connector's (``adx.X``), the KQL is sent to the cluster as it is (the
connector's ``*_query``), only the ``adx.`` prefixes taken off: the real
Kusto engine, nothing translated.

The extension is found at ``KqlTranslator(extension=)``, the
``DUCKDUCK_KQL_EXTENSION`` env var, the config file's ``"kql": {"extension": …}``
or ``~/.duckduck/extensions/kql.duckdb_extension``; ``scripts/build_kql_extension.sh``
builds it (.NET 10 SDK). It's loaded unsigned into a private DuckDB
connection used for nothing but translating.
"""

import inspect
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

EXTENSION_ENV = "DUCKDUCK_KQL_EXTENSION"
DEFAULT_EXTENSION = os.path.join("~", ".duckduck", "extensions", "kql.duckdb_extension")
BUILD_HELP = (
    "KQL needs the kql DuckDB extension: download it from the duckduck repo's Releases → \"kql-extension\" "
    "(kql-<platform>.duckdb_extension), rename it kql.duckdb_extension and put it in "
    f"{DEFAULT_EXTENSION} — or point the config's \"kql\": {{\"extension\": \"…\"}} or {EXTENSION_ENV} at it. "
    "Or build it with scripts/build_kql_extension.sh (needs the .NET 10 SDK)."
)
_PLACEHOLDER = "__duckref_"


class KqlError(ValueError):
    """The KQL can't be run: a syntax error, a management command, a table it can't read."""


class KqlUnavailable(RuntimeError):
    """No translator: the extension isn't built / can't be loaded."""


@dataclass
class Translation:
    kql: str
    sql: str
    #: "translated" (KQL → DuckDB SQL over DuckAPI's tables) or "native" (sent to ADX as KQL)
    route: str = "translated"
    #: the ADX connector a native query goes to
    service: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"kql": self.kql, "sql": self.sql, "route": self.route, "service": self.service, "notes": self.notes}


# ---------------------------------------------------------------------------
# reading KQL: strings and comments masked, so names are only read outside them
# ---------------------------------------------------------------------------

def _masked(kql: str) -> str:
    """The KQL with string contents and comments blanked (same length): '…', "…", @'…', ```…```, // …"""
    out, i, n = list(kql), 0, len(kql)
    while i < n:
        if kql.startswith("```", i):
            j = kql.find("```", i + 3)
            j = n if j < 0 else j
            for k in range(i + 3, j):
                out[k] = " "
            i = j + 3
        elif kql.startswith("//", i):
            j = kql.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif kql[i] in "'\"" or (kql[i] == "@" and i + 1 < n and kql[i + 1] in "'\""):
            verbatim = kql[i] == "@"
            q = kql[i + 1] if verbatim else kql[i]
            j = i + (2 if verbatim else 1)
            while j < n and kql[j] != q:
                j += 2 if (not verbatim and kql[j] == "\\") else 1
            for k in range(i + (2 if verbatim else 1), min(j, n)):
                out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _at_table_position(masked: str, start: int) -> bool:
    """Whether a name at ``start`` is where a table goes: the start, after ( ; , ``let x =``, join / lookup / union."""
    before = masked[:start].rstrip()
    if not before or before[-1] in "(;,":
        return True
    if re.search(r"\blet\s+[A-Za-z_]\w*\s*=$", before):
        return True
    return bool(re.search(r"\b(join|lookup|union)(\s+[A-Za-z_]\w*\s*=\s*[\w.]+)*$", before, re.IGNORECASE))


def _kql_string(text: str) -> str:
    """The value of a KQL string literal ('…', "…", @'…')."""
    if text.startswith("@"):
        return text[2:-1].replace(text[1] * 2, text[1])
    body = text[1:-1]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), body)


def _literal_arg(text: str) -> Any:
    text = text.strip()
    if re.fullmatch(r"@?(['\"]).*\1", text, re.S):
        return _kql_string(text)
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d*(e-?\d+)?", text, re.IGNORECASE):
        return float(text)
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    raise KqlError(f"{text!r}: a table's argument must be a literal ('text', a number, true/false)")


def _sql_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _split_args(masked: str, text: str) -> List[str]:
    """Top-level comma-separated pieces of ``text`` (``masked`` is the same span, strings blanked)."""
    pieces, depth, last = [], 0, 0
    for i, ch in enumerate(masked):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            pieces.append(text[last:i])
            last = i + 1
    pieces.append(text[last:])
    return [p for p in pieces if p.strip()]


def _closing(masked: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(masked)):
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise KqlError("unbalanced parentheses")


# ---------------------------------------------------------------------------
# time: ago() / now() → the datetime they mean
# ---------------------------------------------------------------------------

_UNITS = {"d": 86400.0, "day": 86400.0, "days": 86400.0, "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0,
          "hours": 3600.0, "m": 60.0, "min": 60.0, "minute": 60.0, "minutes": 60.0, "s": 1.0, "sec": 1.0,
          "second": 1.0, "seconds": 1.0, "ms": 0.001, "milli": 0.001, "millisecond": 0.001,
          "milliseconds": 0.001, "microsecond": 1e-6, "microseconds": 1e-6, "tick": 1e-7, "ticks": 1e-7}
_TIMESPAN = r"(-?\d+(?:\.\d+)?)\s*(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b"


def _timespan(text: str) -> Optional[timedelta]:
    m = re.fullmatch(r"\s*(?:time\(\s*" + _TIMESPAN + r"\s*\)|" + _TIMESPAN + r")\s*", text, re.IGNORECASE)
    if not m:
        return None
    amount, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return timedelta(seconds=float(amount) * _UNITS[unit.lower()])


def _datetime_literal(at: datetime) -> str:
    return "datetime(" + at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z)"


def resolve_time(kql: str, now: datetime) -> str:
    """``ago(1d)`` / ``now()`` / ``now(-1h)`` → ``datetime(…)``: one clock for the whole query, and a literal that reaches the source."""
    masked = _masked(kql)
    edits: List[Tuple[int, int, str]] = []
    for m in re.finditer(r"\b(ago|now)\s*\(", masked, re.IGNORECASE):
        if m.start() and (masked[m.start() - 1].isalnum() or masked[m.start() - 1] in "_."):
            continue
        close = _closing(masked, m.end() - 1)
        inside = kql[m.end():close]
        if m.group(1).lower() == "now" and not inside.strip():
            at = now
        else:
            span = _timespan(inside)
            if span is None:
                continue  # an expression: left to the translator
            at = now - span if m.group(1).lower() == "ago" else now + span
        edits.append((m.start(), close + 1, _datetime_literal(at)))
    for start, end, text in reversed(edits):
        kql = kql[:start] + text + kql[end:]
    return kql


# ---------------------------------------------------------------------------
# the translator
# ---------------------------------------------------------------------------

@dataclass
class _Ref:
    start: int
    end: int
    sql: str
    kind: str  # "address" | "call"
    parts: List[str] = field(default_factory=list)
    written: str = ""  # as the KQL has it


class KqlTranslator:
    """KQL → DuckDB SQL for a ``DuckAPI`` (see the module docstring)."""

    def __init__(self, extension: Optional[str] = None, translate: Optional[Callable[[str], str]] = None,
                 now: Optional[Callable[[], datetime]] = None):
        #: a kql.duckdb_extension path (else the env var, else the default path)
        self.extension = extension
        #: ``kql → sql`` instead of the extension (tests, another translator)
        self._translate = translate
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._conn: Any = None
        self._error: Optional[str] = None
        self._lock = threading.Lock()

    # -- availability -------------------------------------------------

    def path(self) -> Optional[str]:
        """The extension file it would load, or None when there's none."""
        # the first one given is the one used: a path that's set but wrong is an error, never a fallback
        candidate = self.extension or os.environ.get(EXTENSION_ENV) or DEFAULT_EXTENSION
        full = os.path.abspath(os.path.expanduser(candidate))
        if os.path.isfile(full):
            return full
        if candidate != DEFAULT_EXTENSION:
            self._error = f"no kql extension at {full}"
        return None

    def status(self) -> Dict[str, Any]:
        """``{available, reason, extension}`` — for the page (the KQL switch) and ``/api/meta``."""
        if self._translate is not None:
            return {"available": True, "reason": None, "extension": None}
        try:
            self._connection()
            return {"available": True, "reason": None, "extension": self.path(), "checks_syntax": self.checks_syntax}
        except KqlUnavailable as exc:
            return {"available": False, "reason": str(exc), "extension": None}

    @property
    def available(self) -> bool:
        return bool(self.status()["available"])

    def _connection(self) -> Any:
        if self._conn is not None:
            return self._conn
        import duckdb

        path = self.path()
        if path is None:
            raise KqlUnavailable((self._error + ". " if self._error else "") + BUILD_HELP)
        conn = duckdb.connect(config={"allow_unsigned_extensions": "true"})
        try:
            conn.execute(f"LOAD '{path.replace(chr(39), chr(39) * 2)}'")
        except Exception as exc:
            conn.close()
            hint = ""
            if "GLIBC" in str(exc):  # built on a newer Linux than this one
                hint = ("That file was built for a newer Linux (glibc) than this one: download kql-linux_amd64 "
                        "again from the \"kql-extension\" release (built for glibc 2.28+), or build it here. ")
            raise KqlUnavailable(f"couldn't load the kql extension at {path}: {exc}. {hint}{BUILD_HELP}") from exc
        conn.execute("SET enable_external_access = false")
        self._conn = conn
        return conn

    def syntax_errors(self, kql: str) -> Optional[List[Tuple[int, str]]]:
        """
        The Kusto parser's syntax errors as ``(offset, message)`` — ``kql_syntax_errors``, which
        scripts/build_kql_extension.sh adds to the extension. None when it can't check (an
        extension built without it, or an injected ``translate``): the translation alone
        would quietly run whatever the parser recovered (``T | where x == 1 | projct a`` →
        ``SELECT * FROM a``).
        """
        if self._translate is not None:
            return None
        conn = self._connection()
        with self._lock:
            try:
                text = conn.execute("SELECT kql_syntax_errors(?)", [kql]).fetchone()[0] or ""
            except Exception:
                return None
        out = []
        for line in text.splitlines():
            at, _, message = line.partition(": ")
            out.append((int(at) if at.strip().isdigit() else 0, message or line))
        return out

    @property
    def checks_syntax(self) -> bool:
        try:
            return self.syntax_errors("print 1") is not None
        except KqlUnavailable:
            return False

    def _check(self, marked: str, refs: List["_Ref"]) -> None:
        """Raises ``KqlError`` naming where the KQL doesn't parse (the snippet as the user wrote it)."""
        errors = self.syntax_errors(marked)
        if not errors:
            return
        messages = []
        for at, message in errors[:3]:
            line = marked[:at].count("\n") + 1
            snippet = marked[at:at + 40].split("\n")[0].strip()
            snippet = re.sub(rf"\['{_PLACEHOLDER}(\d+)'\]", lambda m: refs[int(m.group(1))].written or refs[int(m.group(1))].sql, snippet)
            messages.append(f"{message.rstrip('.')} (line {line}{f', at “{snippet}”' if snippet else ', at the end'})")
        raise KqlError("KQL doesn't parse: " + "; ".join(messages))

    def raw(self, kql: str) -> str:
        """The translation alone: ``kql_to_sql(kql)``."""
        if self._translate is not None:
            return self._translate(kql)
        conn = self._connection()
        with self._lock:
            try:
                return conn.execute("SELECT kql_to_sql(?)", [kql]).fetchone()[0]
            except Exception as exc:
                message = str(exc).split("\n")[0]
                message = re.sub(r"^\w+ Error: ", "", message)
                raise KqlError(f"KQL: {message}") from exc

    # -- KQL → SQL for a DuckAPI --------------------------------------

    def to_sql(self, kql: str, duck: Any, native: bool = True) -> Translation:
        """The SQL ``duck.sql()`` runs for this KQL (or the native ADX call when it only reads one ADX connector)."""
        text = (kql or "").strip().rstrip(";").strip()
        if not text:
            raise KqlError("no KQL to run")
        if text.startswith("."):
            if re.fullmatch(r"\.show\s+tables", text, re.IGNORECASE):
                return Translation(kql=kql, sql="SHOW TABLES")
            raise KqlError("management commands (.create, .set, .drop …) aren't run here — queries only")
        text = resolve_time(text, self._now())
        refs = self._refs(text, duck)
        if native:
            direct = self._native(text, refs, duck)
            if direct is not None:
                return direct
        marked = text
        for i, ref in sorted(enumerate(refs), key=lambda x: -x[1].start):
            marked = marked[:ref.start] + f"['{_PLACEHOLDER}{i}']" + marked[ref.end:]
        self._check(marked, refs)
        sql = self.raw(marked)
        missing = [refs[i].written for i in range(len(refs)) if not re.search(rf"\b{_PLACEHOLDER}{i}\b", sql)]
        if missing:  # the translation dropped a table the KQL reads: it didn't read the query as written
            raise KqlError(f"KQL: couldn't translate the query as written ({', '.join(missing)} got lost) — check its syntax")
        sql = self._post(sql, refs, duck)
        return Translation(kql=kql, sql=sql)

    def _refs(self, kql: str, duck: Any) -> List[_Ref]:
        """DuckAPI's own table references in the KQL: addresses (bare or ['…']) and table-function calls."""
        from .addresses import _ident, services

        masked = _masked(kql)
        known = services(duck)
        refs: List[_Ref] = []

        def taken(start: int) -> bool:
            return any(r.start <= start < r.end for r in refs)

        # ['svc.a.b'] — a whole address in brackets
        for m in re.finditer(r"\[\s*(['\"])", masked):
            close = masked.find(m.group(1), m.end())
            end_m = re.match(r"\s*\]", masked[close + 1:]) if close >= 0 else None
            if end_m is None:
                continue
            end = close + 1 + end_m.end()
            parts = _kql_string(kql[m.end() - 1:close + 1]).split(".")
            if len(parts) > 1 and parts[0].lower() in known and _at_table_position(masked, m.start()):
                refs.append(_Ref(m.start(), end, ".".join(_ident(duck, p) for p in parts), "address", parts, kql[m.start():end]))
        # svc.a.b — bare parts or ['…'] parts
        part = r"(?:[A-Za-z_]\w*|\[\s*['\"][^'\"]*['\"]\s*\])"
        for m in re.finditer(rf"(?<![\w.$\]])([A-Za-z_]\w*)((?:\s*\.\s*{part})+)(?![\w$]|\s*[.(\[])", masked):
            if taken(m.start()) or m.group(1).lower() not in known or not _at_table_position(masked, m.start()):
                continue
            raw_parts = re.findall(part, kql[m.start(2):m.end(2)])
            parts = [m.group(1)] + [_kql_string(p.strip()[1:-1].strip()) if p.startswith("[") else p for p in raw_parts]
            refs.append(_Ref(m.start(), m.end(), ".".join(_ident(duck, p) for p in parts), "address", parts,
                             kql[m.start():m.end()]))
        # fn(k='v') / fn('v') — a registered table function called
        names = {n.lower(): n for n in duck.functions}
        for m in re.finditer(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(", masked):
            name = names.get(m.group(1).lower())
            if name is None or taken(m.start()) or not _at_table_position(masked, m.start()):
                continue
            close = _closing(masked, m.end() - 1)
            args = self._call_args(name, duck, masked[m.end():close], kql[m.end():close])
            text = f"{name}({', '.join(f'{k}={_sql_literal(v)}' for k, v in args.items())})"
            refs.append(_Ref(m.start(), close + 1, text, "call", written=kql[m.start():close + 1]))
        return sorted(refs, key=lambda r: r.start)

    @staticmethod
    def _call_args(name: str, duck: Any, masked: str, text: str) -> Dict[str, Any]:
        try:
            params = [p for p in inspect.signature(duck.functions[name]).parameters.values()
                      if p.name not in ("where", "limit") and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
        except (TypeError, ValueError):
            params = []
        out: Dict[str, Any] = {}
        pieces, at = _split_args(masked, text), 0
        for piece in pieces:
            m = re.match(r"\s*([A-Za-z_]\w*)\s*=(?!=)(.*)$", piece, re.S)
            if m:
                out[m.group(1)] = _literal_arg(m.group(2))
                continue
            free = [p for p in params if p.name not in out]  # positional: the next argument in the signature's order
            if not free:
                raise KqlError(f"{name}: too many arguments (it takes {', '.join(p.name for p in params) or 'none'})")
            out[free[0].name] = _literal_arg(piece)
        return out

    def _post(self, sql: str, refs: List[_Ref], duck: Any) -> str:
        from .addresses import _masked as sql_masked

        # the references back
        sql = re.sub(rf'"?\b{_PLACEHOLDER}(\d+)\b"?', lambda m: refs[int(m.group(1))].sql, sql)
        # arg.x — the extension reads it as a dynamic property
        sql = re.sub(r"json_extract(?:_string)?\(\s*(args?)\s*,\s*'\$\.([A-Za-z_]\w*)'\s*\)", r"\1.\2", sql)
        # has / has_cs / hasprefix / hassuffix → a term regex, which no source takes: ANDed with the LIKE it implies
        # (a superset — "contains the term"), which reaches the source; DuckDB still applies the exact regex
        sql = re.sub(r"regexp_matches\(CAST\(((?:\"(?:[^\"]|\"\")+\"|[A-Za-z_][\w.]*)) AS VARCHAR\), "
                     r"'(\(\?i\))?(\(\?:\^\|\[\^\\p\{L\}\\p\{N\}\]\))?([A-Za-z0-9_ -]+)(\(\?:\[\^\\p\{L\}\\p\{N\}\]\|\$\))?'\)",
                     lambda m: f"({m.group(1)} {'ILIKE' if m.group(2) else 'LIKE'} '%{m.group(4)}%' AND {m.group(0)})", sql)
        # =~ 'x' → ILIKE 'x' (exact: no wildcard in it), which reaches the source
        sql = re.sub(r"""UPPER\(((?:"(?:[^"]|"")+"|[A-Za-z_][\w.]*))\)\s*=\s*UPPER\('((?:[^'%_\\]|'')*)'\)""",
                     r"\1 ILIKE '\2'", sql)
        # a table read twice: an alias per read, so each gets its own filters. Only table names count —
        # registered tables, the references, lets — never EXTRACT(… FROM col) or FROM TIMESTAMP '…'
        masked = sql_masked(sql)
        tables = {n.lower() for n in duck.functions} | {r.sql.lower().replace(" ", "") for r in refs}
        tables |= {m.group(1).lower() for m in re.finditer(r"\b([A-Za-z_]\w*)\s+AS\s+(?:NOT\s+)?(?:MATERIALIZED\s+)?\(",
                                                           masked, re.IGNORECASE)}
        ident = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
        reads: List[Tuple[int, str, bool]] = []
        for m in re.finditer(rf"\b(?:FROM|JOIN)\s+({ident}(?:\s*\.\s*{ident})*)", masked, re.IGNORECASE):
            end = m.end()
            rest = masked[end:]
            if rest.lstrip().startswith("("):
                end = _closing(masked, end + len(rest) - len(rest.lstrip())) + 1
                rest = masked[end:]
            aliased = bool(re.match(r"\s+(AS\s+|(?!(WHERE|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|OUTER|ON|GROUP|ORDER|LIMIT|"
                                    r"UNION|EXCEPT|INTERSECT|QUALIFY|HAVING|WINDOW|USING|NATURAL|ASOF|POSITIONAL|"
                                    r"ANTI|SEMI|LATERAL|OFFSET)\b)[A-Za-z_])", rest, re.IGNORECASE))
            name = sql[m.start(1):end].lower().replace(" ", "")
            if name.strip('"') in tables or name in tables:
                reads.append((end, name, aliased))
        counts = Counter(name for _, name, _ in reads)
        twice = [(end, name) for end, name, aliased in reads if counts[name] > 1 and not aliased]
        for n, (end, _) in enumerate(sorted(twice, reverse=True)):
            sql = sql[:end] + f" AS _k{len(twice) - n}" + sql[end:]
        return sql

    def _native(self, kql: str, refs: List[_Ref], duck: Any) -> Optional[Translation]:
        """The KQL as ADX runs it, when every table it reads is one ADX connector's ``adx.Table``."""
        from .addresses import services

        if not refs or any(r.kind != "address" or len(r.parts) != 2 for r in refs):
            return None
        service = refs[0].parts[0].lower()
        if any(r.parts[0].lower() != service for r in refs):
            return None
        query_fn = next((n for n, f in duck.functions.items() if duck.service_of.get(n) == service
                         and getattr(f, "__name__", "") == "query" and getattr(f, "__module__", "") == "duckduck.adx"), None)
        if query_fn is None or service not in services(duck):
            return None
        # nothing else read: every table the translation reads is one of these references (or a let)
        marked = kql
        for i, ref in sorted(enumerate(refs), key=lambda x: -x[1].start):
            marked = marked[:ref.start] + f"['{_PLACEHOLDER}{i}']" + marked[ref.end:]
        try:
            self._check(marked, refs)
            sql = self.raw(marked)
        except KqlUnavailable:
            sql = None  # no translator: the native route doesn't need one — ADX checks the KQL itself
        if sql is not None:
            from .addresses import _masked as sql_masked

            masked = sql_masked(sql)
            lets = {m.group(1).lower() for m in re.finditer(r"\b([A-Za-z_]\w*)\s+AS\s+(?:NOT\s+)?(?:MATERIALIZED\s+)?\(",
                                                             masked, re.IGNORECASE)}
            for m in re.finditer(r'\b(?:FROM|JOIN)\s+("?[A-Za-z_][\w$]*"?)', masked, re.IGNORECASE):
                name = m.group(1).strip('"').lower()
                if not name.startswith(_PLACEHOLDER) and name not in lets:
                    return None
        native = kql
        for ref in sorted(refs, key=lambda r: -r.start):
            table = ref.parts[1].replace("'", "\\'")
            native = native[:ref.start] + f"['{table}']" + native[ref.end:]
        # the KQL as an argument in the WHERE (read by the SQL parser — a call's inline text is cut at its first ')')
        return Translation(kql=kql, sql=f"SELECT * FROM {query_fn}\nWHERE arg.kql = {_sql_literal(native)}", route="native",
                           service=service, notes=[f"runs on {service} as KQL — nothing translated"])


_DEFAULT: Optional[KqlTranslator] = None


def default_translator(extension: Optional[str] = None) -> KqlTranslator:
    """One translator per process (the extension is loaded once); ``extension`` replaces it when it differs."""
    global _DEFAULT
    if _DEFAULT is None or (extension and extension != _DEFAULT.extension):
        _DEFAULT = KqlTranslator(extension=extension)
    return _DEFAULT


def run_kql(duck: Any, kql: str, translator: Optional[KqlTranslator] = None, native: bool = True) -> Any:
    """``duck.sql()`` of this KQL — the relation, like ``duck.sql``."""
    return duck.sql((translator or default_translator()).to_sql(kql, duck, native=native).sql)

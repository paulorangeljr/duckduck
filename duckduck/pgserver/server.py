"""
``PGServer``: duckduck's tables over the PostgreSQL wire protocol, so any
PostgreSQL client — DBeaver, DataGrip, psql, Power BI, Tableau, Metabase,
Superset, psycopg, JDBC — connects to it and queries them with SQL.

Each connection gets its own DuckDB connection (a cursor on one database:
the ``duckduck_pg`` catalog is shared) and its own ``DuckAPI`` over the same
registered tables, saved tables and source cache as the web app. It's the
SQL tab's guard: read queries only, no file or network access from SQL.

Sign-in: users and passwords from ``pg_server.authentication`` (a secret of
``{"user": "password"}``, resolved like any connector's block), else the
``DUCKDUCK_PG_PASSWORD`` environment variable (any user name), else no
password — allowed only on this machine (127.0.0.1). With ``tls``
(``cert`` + ``key`` files) clients can — and should, off this machine —
connect with ``sslmode=require``.
"""

from __future__ import annotations

import hmac
import logging
import os
import random
import re
import socket
import socketserver
import ssl
import struct
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .. import addresses, progress
from ..views import read_only_reason, statements
from . import catalog as pgcat
from . import protocol as pg

logger = logging.getLogger("duckduck.pgserver")

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
BATCH = 1000


class QueryError(Exception):
    def __init__(self, message: str, code: str = "XX000", hint: Optional[str] = None):
        super().__init__(message)
        self.code, self.hint = code, hint


class Result:
    """A statement's answer: its columns and a row iterator, or just a command tag."""

    def __init__(self, tag: str, fields: Optional[List[Tuple[str, int, int]]] = None,
                 rows: Optional[Iterator[Sequence[Any]]] = None, on_close: Any = None):
        self.tag, self.fields, self.rows = tag, fields, rows
        self.sent = 0
        self.done = rows is None
        self._on_close = on_close

    @property
    def oids(self) -> List[int]:
        return [f[1] for f in self.fields or []]

    def take(self, n: int) -> List[Sequence[Any]]:
        out: List[Sequence[Any]] = []
        if self.rows is None:
            return out
        for row in self.rows:
            out.append(row)
            if n and len(out) >= n:
                break
        else:
            self.close()
        self.sent += len(out)
        return out

    def close(self) -> None:
        self.done = True
        self.rows = None
        if self._on_close is not None:
            cb, self._on_close = self._on_close, None
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass

    def complete_tag(self) -> str:
        return f"SELECT {self.sent}" if self.tag == "SELECT" else self.tag


def _relation_rows(rel: Any) -> Iterator[Sequence[Any]]:
    while True:
        batch = rel.fetchmany(BATCH)
        if not batch:
            return
        yield from batch


class Session:
    """One client connection."""

    def __init__(self, server: "PGServer", sock: socket.socket, address: Any):
        self.server, self.sock, self.address = server, sock, address
        self.pid = random.randint(1000, 2 ** 31 - 1)
        self.secret = random.randint(1, 2 ** 31 - 1)
        self.user = "duckduck"
        self.params: Dict[str, str] = {}
        self.status = "I"  # I idle / T in a transaction / E failed transaction
        self.statements: Dict[str, Tuple[str, List[int]]] = {}
        self.portals: Dict[str, Dict[str, Any]] = {}
        self.skip_until_sync = False
        self.progress: Optional[progress.Progress] = None
        self.cursor: Any = None
        self.duck: Any = None
        self._buffer = bytearray()

    # -- IO ----------------------------------------------------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        data = bytearray()
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("client went away")
            data.extend(chunk)
        return bytes(data)

    def send(self, data: bytes) -> None:
        self._buffer.extend(data)
        if len(self._buffer) > 256 * 1024:
            self.flush()

    def flush(self) -> None:
        if self._buffer:
            self.sock.sendall(bytes(self._buffer))
            self._buffer.clear()

    def read_message(self) -> Tuple[str, bytes]:
        head = self._recv_exact(5)
        kind, length = head[:1].decode(), struct.unpack("!i", head[1:])[0]
        return kind, self._recv_exact(length - 4) if length > 4 else b""

    # -- startup ----------------------------------------------------------------------------------------

    def startup(self) -> bool:
        while True:
            length = struct.unpack("!i", self._recv_exact(4))[0]
            body = self._recv_exact(length - 4)
            code = struct.unpack("!i", body[:4])[0]
            if code == pg.SSL_REQUEST:
                if self.server.ssl_context is not None:
                    self.sock.sendall(b"S")
                    self.sock = self.server.ssl_context.wrap_socket(self.sock, server_side=True)
                else:
                    self.sock.sendall(b"N")
                continue
            if code == pg.GSSENC_REQUEST:
                self.sock.sendall(b"N")
                continue
            if code == pg.CANCEL_REQUEST:
                pid, secret = struct.unpack("!ii", body[4:12])
                self.server.cancel(pid, secret)
                return False
            if code >> 16 != 3:
                self.sock.sendall(pg.error(f"unsupported protocol {code >> 16}.{code & 0xFFFF}", "0A000"))
                return False
            break
        fields = body[4:].split(b"\x00")
        self.params = {fields[i].decode(): fields[i + 1].decode() for i in range(0, len(fields) - 1, 2) if fields[i]}
        self.user = self.params.get("user") or "duckduck"
        if self.server.ssl_required and not isinstance(self.sock, ssl.SSLSocket):
            self.sock.sendall(pg.error("this server needs an encrypted connection (sslmode=require)", "28000"))
            return False
        if not self._authenticate():
            return False
        self._open()
        self.send(pg.authentication(0))
        for name, value in (("server_version", pgcat.SERVER_VERSION), ("server_encoding", "UTF8"),
                            ("client_encoding", "UTF8"), ("DateStyle", "ISO, MDY"), ("TimeZone", "UTC"),
                            ("integer_datetimes", "on"), ("standard_conforming_strings", "on"),
                            ("IntervalStyle", "postgres"), ("is_superuser", "off"),
                            ("session_authorization", self.user),
                            ("application_name", self.params.get("application_name", "")),
                            ("default_transaction_read_only", "on")):
            self.send(pg.parameter_status(name, value))
        self.send(pg.backend_key(self.pid, self.secret))
        self.send(pg.ready(self.status))
        self.flush()
        return True

    def _authenticate(self) -> bool:
        check = self.server.password_check
        if check is None:
            return True
        self.sock.sendall(pg.authentication(3))  # cleartext password (inside TLS when it's on)
        kind, body = self.read_message()
        password = body.rstrip(b"\x00").decode("utf-8", errors="replace") if kind == "p" else ""
        if not check(self.user, password):
            logger.warning("PostgreSQL: sign-in refused for %r from %s", self.user, self.address[0])
            self.sock.sendall(pg.error(f'password authentication failed for user "{self.user}"', "28P01"))
            return False
        return True

    def _open(self) -> None:
        self.cursor = self.server.db.cursor()
        self.cursor.execute("SET VARIABLE duckduck_user = ?", [self.user])
        self.duck = self.server.session_duck(self.cursor)

    # -- the loop ----------------------------------------------------------------------------------------

    def run(self) -> None:
        try:
            if not self.startup():
                return
            self.server.sessions[self.pid] = self
            logger.info("PostgreSQL: %s connected from %s (%s)", self.user, self.address[0],
                        self.params.get("application_name") or "client")
            while True:
                kind, body = self.read_message()
                if kind == "X":
                    return
                if self.skip_until_sync and kind not in ("S",):
                    continue
                handler = {"Q": self.simple_query, "P": self.parse, "B": self.bind, "D": self.describe,
                           "E": self.execute, "S": self.sync, "C": self.close, "H": self.flush_message}.get(kind)
                if handler is None:
                    self._error(QueryError(f"message type {kind!r} isn't supported", "0A000"), extended=False)
                    self.send(pg.ready(self.status))
                    self.flush()
                    continue
                handler(body)
        except (ConnectionError, OSError, ssl.SSLError):
            pass
        finally:
            self.server.sessions.pop(self.pid, None)
            for portal in self.portals.values():
                if portal.get("result"):
                    portal["result"].close()
            try:
                if self.cursor is not None:
                    self.cursor.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.sock.close()
            except OSError:
                pass

    def cancel(self) -> None:
        if self.progress is not None:
            self.progress.cancel()
        try:
            self.cursor.interrupt()
        except Exception:  # noqa: BLE001
            pass

    # -- simple query ---------------------------------------------------------------------------------------

    def simple_query(self, body: bytes) -> None:
        text = pg.Reader(body).cstr()
        parts = [p for p in statements(text) if p.strip()]
        if not parts:
            self.send(pg.EMPTY_QUERY)
        for part in parts:
            try:
                result = self.run_statement(part)
                if result.fields is not None:
                    self.send(pg.row_description(result.fields))
                    for row in result.take(0):
                        self.send(pg.data_row(row, result.oids))
                self.send(pg.command_complete(result.complete_tag()))
                result.close()
            except (QueryError, progress.Cancelled, Exception) as exc:  # noqa: BLE001
                self._error(exc, extended=False)
                break
        self.send(pg.ready(self.status))
        self.flush()

    # -- extended query -------------------------------------------------------------------------------------

    def parse(self, body: bytes) -> None:
        r = pg.Reader(body)
        name, query = r.cstr(), r.cstr()
        oids = [r.int32() for _ in range(r.int16())]
        self.statements[name] = (query, oids)
        self.send(pg.PARSE_COMPLETE)

    def bind(self, body: bytes) -> None:
        r = pg.Reader(body)
        portal, name = r.cstr(), r.cstr()
        if name not in self.statements:
            return self._error(QueryError(f'prepared statement "{name}" does not exist', "26000"), extended=True)
        query, oids = self.statements[name]
        formats = [r.int16() for _ in range(r.int16())]
        values = []
        for i in range(r.int16()):
            n = r.int32()
            raw = None if n < 0 else r.bytes(n)
            fmt = formats[0] if len(formats) == 1 else (formats[i] if i < len(formats) else 0)
            values.append(pg.decode_param(raw, fmt, oids[i] if i < len(oids) else 0))
        result_formats = [r.int16() for _ in range(r.int16())]
        old = self.portals.pop(portal, None)
        if old and old.get("result"):
            old["result"].close()
        self.portals[portal] = {"sql": pg.bind_params(query, values, oids, addresses._masked),
                                "formats": result_formats, "result": None}
        self.send(pg.BIND_COMPLETE)

    def describe(self, body: bytes) -> None:
        r = pg.Reader(body)
        what, name = r.byte(), r.cstr()
        try:
            if what == "S":
                if name not in self.statements:
                    raise QueryError(f'prepared statement "{name}" does not exist', "26000")
                query, oids = self.statements[name]
                n = pg.count_params(query, addresses._masked)
                declared = [(oids[i] if i < len(oids) and oids[i] else pg.TEXT) for i in range(n)]
                self.send(pg.parameter_description(declared))
                fields = self.describe_fields(pg.bind_params(query, [None] * n, declared, addresses._masked))
                self.send(pg.row_description(fields) if fields is not None else pg.NO_DATA)
                return
            portal = self.portals.get(name)
            if portal is None:
                raise QueryError(f'portal "{name}" does not exist', "34000")
            if portal["result"] is None:
                portal["result"] = self.run_statement(portal["sql"])
            result = portal["result"]
            self.send(pg.row_description(result.fields, portal["formats"]) if result.fields is not None
                      else pg.NO_DATA)
        except (QueryError, progress.Cancelled, Exception) as exc:  # noqa: BLE001
            self._error(exc, extended=True)

    def execute(self, body: bytes) -> None:
        r = pg.Reader(body)
        name, max_rows = r.cstr(), r.int32()
        portal = self.portals.get(name)
        try:
            if portal is None:
                raise QueryError(f'portal "{name}" does not exist', "34000")
            if portal["result"] is None:
                portal["result"] = self.run_statement(portal["sql"])
            result = portal["result"]
            if result.fields is not None:
                for row in result.take(max_rows):
                    self.send(pg.data_row(row, result.oids, portal["formats"]))
            if result.tag == "EMPTY":
                self.send(pg.EMPTY_QUERY)
            elif result.done:
                self.send(pg.command_complete(result.complete_tag()))
            else:
                self.send(pg.PORTAL_SUSPENDED)
        except (QueryError, progress.Cancelled, Exception) as exc:  # noqa: BLE001
            self._error(exc, extended=True)

    def sync(self, body: bytes) -> None:
        self.skip_until_sync = False
        unnamed = self.portals.pop("", None)
        if unnamed and unnamed.get("result"):
            unnamed["result"].close()
        self.send(pg.ready(self.status))
        self.flush()

    def close(self, body: bytes) -> None:
        r = pg.Reader(body)
        what, name = r.byte(), r.cstr()
        if what == "S":
            self.statements.pop(name, None)
        else:
            portal = self.portals.pop(name, None)
            if portal and portal.get("result"):
                portal["result"].close()
        self.send(pg.CLOSE_COMPLETE)

    def flush_message(self, body: bytes) -> None:
        self.flush()

    def _error(self, exc: BaseException, extended: bool) -> None:
        if isinstance(exc, QueryError):
            code, text, hint = exc.code, str(exc), exc.hint
        elif isinstance(exc, progress.Cancelled) or "INTERRUPT" in type(exc).__name__.upper():
            code, text, hint = "57014", "canceling statement due to user request", None
        else:
            code, text, hint = _sqlstate(exc), f"{type(exc).__name__}: {exc}", None
            logger.info("PostgreSQL: %s", text.splitlines()[0])
        if self.status == "T":
            self.status = "E"
        self.send(pg.error(text, code, hint=hint))
        if extended:
            self.skip_until_sync = True

    # -- statements -----------------------------------------------------------------------------------------

    def run_statement(self, sql: str) -> Result:
        sql = sql.strip().rstrip(";").strip()
        if not sql:
            return Result("EMPTY")
        command = self.session_command(sql)
        if command is not None:
            return command
        if self.status == "E":
            raise QueryError("current transaction is aborted, commands ignored until end of transaction block",
                             "25P02")
        reason = read_only_reason(sql)
        if reason:
            raise QueryError(reason, "25006", hint="duckduck is read-only: SELECT from its tables")
        self.progress = progress.Progress()
        try:
            with progress.tracking(self.progress):
                return self._query(sql)
        finally:
            self.progress = None

    def _query(self, sql: str) -> Result:
        masked = addresses._masked(sql)
        tables = self.server.reads_tables(masked)
        if pgcat.is_catalog_query(masked):
            self.server.catalog.refresh()
            rewritten = pgcat.rewrite(sql, self.server.catalog, self.user, addresses._masked)
            if rewritten != sql:
                logger.debug("  catalog query → %s", " ".join(rewritten.split()))
            if not tables:
                return self._result(self.cursor.sql(rewritten), [], names=pgcat.column_name)
            sql = rewritten  # version(), current_user… next to duckduck's tables
        before = self._temp_views()
        rel = self.duck.sql(self.server.client_names(sql))
        created = [v for v in self._temp_views() if v not in before]
        return self._result(rel, created, names=pgcat.column_name)

    def describe_fields(self, sql: str) -> Optional[List[Tuple[str, int, int]]]:
        """A statement's columns before it runs: DESCRIBE for the catalog, a run (reads kept by the cache) otherwise."""
        sql = sql.strip().rstrip(";").strip()
        if not sql:
            return None
        command = self.session_command(sql, dry=True)
        if command is not None:
            return command.fields
        masked = addresses._masked(sql)
        if pgcat.is_catalog_query(masked):
            self.server.catalog.refresh()
            rewritten = pgcat.rewrite(sql, self.server.catalog, self.user, addresses._masked)
            described = self.cursor.execute("DESCRIBE " + rewritten).fetchall()
            return [(pgcat.column_name(str(r[0])), pg.oid_of(r[1]), pg.typmod_of(r[1])) for r in described]
        result = self.run_statement(sql)
        fields = result.fields
        result.close()
        return fields

    def _result(self, rel: Any, created: List[str], names: Any = str) -> Result:
        def release() -> None:
            for view in created:
                try:
                    self.cursor.unregister(view)
                except Exception:  # noqa: BLE001
                    pass

        if rel is None:
            release()
            return Result("SELECT", [], iter(()))
        fields = [(names(str(n)), pg.oid_of(t), pg.typmod_of(t)) for n, t in zip(rel.columns, rel.types)]
        return Result("SELECT", fields, _relation_rows(rel), on_close=release)

    def _temp_views(self) -> List[str]:
        return [r[0] for r in self.cursor.execute(
            "SELECT view_name FROM duckdb_views() WHERE temporary AND "
            "(starts_with(view_name, '_api_') OR starts_with(view_name, '_page_'))").fetchall()]

    # -- what PostgreSQL clients send that isn't a query ------------------------------------------------------

    _SHOW = re.compile(r"^SHOW\s+(.+?)$", re.I | re.S)
    _SET = re.compile(r"^SET\s+(?:SESSION\s+|LOCAL\s+)?(.+?)\s*(?:=|\bTO\b)\s*(.+)$", re.I | re.S)

    def session_command(self, sql: str, dry: bool = False) -> Optional[Result]:
        words = sql.split(None, 3)
        first = words[0].upper() if words else ""
        second = words[1].upper() if len(words) > 1 else ""
        text_field = lambda name: [(name, pg.TEXT, -1)]  # noqa: E731
        if first in ("BEGIN", "START"):
            if not dry:
                self.status = "T"
            return Result("BEGIN" if first == "BEGIN" else "START TRANSACTION")
        if first in ("COMMIT", "END"):
            tag = "ROLLBACK" if self.status == "E" else "COMMIT"
            if not dry:
                self.status = "I"
            return Result(tag)
        if first in ("ROLLBACK", "ABORT"):
            if second == "TO" or (second == "TRANSACTION" and len(words) > 2 and words[2].upper() == "TO"):
                if not dry and self.status == "E":
                    self.status = "T"
                return Result("ROLLBACK")
            if not dry:
                self.status = "I"
            return Result("ROLLBACK")
        if first in ("SAVEPOINT", "RELEASE", "DEALLOCATE", "LISTEN", "UNLISTEN", "NOTIFY", "DISCARD", "RESET",
                     "CHECKPOINT", "LOAD", "PREPARE"):
            return Result({"DISCARD": "DISCARD ALL", "PREPARE": "PREPARE TRANSACTION"}.get(first, first))
        if first == "CLOSE":
            return Result("CLOSE CURSOR")
        if first == "SET":
            m = self._SET.match(sql)
            if m and not dry:
                self.params[m.group(1).strip().lower()] = m.group(2).strip().strip("'\"")
            return Result("SET")
        if first == "SHOW":
            m = self._SHOW.match(sql)
            name = (m.group(1) if m else "").strip().strip('"')
            low = re.sub(r"\s+", " ", name.lower())
            if low in ("tables", "all tables") or low.startswith(("tables ", "all tables")):
                return None  # duckduck's SHOW TABLES
            if low == "transaction isolation level":
                low = "transaction_isolation"
            if low == "all":
                rows = self.cursor.execute(f"SELECT name, setting, short_desc FROM {pgcat.SCHEMA}.pg_settings "
                                           f"ORDER BY name").fetchall()
                return Result("SHOW", [("name", pg.TEXT, -1), ("setting", pg.TEXT, -1),
                                       ("description", pg.TEXT, -1)], iter(rows))
            value = self.setting(low)
            if value is None:
                raise QueryError(f'unrecognized configuration parameter "{name}"', "42704")
            return Result("SHOW", text_field(low), iter([(value,)]))
        return None

    def setting(self, name: str) -> Optional[str]:
        if name in self.params:
            return self.params[name]
        row = self.cursor.execute(f"SELECT setting FROM {pgcat.SCHEMA}.pg_settings WHERE lower(name) = ?",
                                  [name]).fetchone()
        return row[0] if row else None


def _sqlstate(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc)
    if "Parser" in name or "syntax error" in text:
        return "42601"
    if "Catalog" in name and "does not exist" in text:
        return "42P01" if "Table" in text else "42883" if "Function" in text else "42704"
    if "Binder" in name:
        return "42703" if "column" in text.lower() else "42P01"
    if isinstance(exc, ValueError):
        return "22023"
    return "XX000"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        Session(self.server.pg, self.request, self.client_address).run()


class _TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class PGServer:
    """
    Serves ``duck``'s tables over the PostgreSQL protocol.

    ``password`` (any user) or ``users`` ({user: password}); neither → no
    password, only on a local ``host``. ``tls`` = (cert file, key file);
    ``ssl_required`` refuses unencrypted connections. ``cache``: a
    ``duckduck.cache.SourceCache`` to share (the web app's), else one from
    the config's ``sql_cache``. ``columns_file``: where the columns learned
    from queries are kept (the catalog shows them).
    """

    def __init__(self, duck: Any, host: str = "127.0.0.1", port: int = 5433, database: str = "duckduck",
                 password: Optional[str] = None, users: Optional[Dict[str, str]] = None,
                 tls: Optional[Tuple[str, str]] = None, ssl_required: bool = False, cache: Any = None,
                 columns_file: Optional[str] = None):
        import duckdb

        from ..cache import SourceCache

        self.source = duck
        self.host, self.port, self.database = host, int(port), database
        if password is None and not users and host not in LOCAL_HOSTS:
            raise ValueError(f"a PostgreSQL endpoint on {host} needs a password: set DUCKDUCK_PG_PASSWORD, or "
                             f"users in pg_server.authentication — or listen on 127.0.0.1 only")
        self.password_check = _password_check(password, users)
        self.ssl_context: Optional[ssl.SSLContext] = None
        if tls:
            self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            self.ssl_context.load_cert_chain(tls[0], tls[1])
        elif ssl_required:
            raise ValueError("ssl_required needs tls (a cert and a key)")
        self.ssl_required = ssl_required
        if self.password_check is not None and self.ssl_context is None and host not in LOCAL_HOSTS:
            logger.warning("PostgreSQL endpoint on %s without TLS: passwords cross the network in clear text — "
                           "set pg_server.tls (cert, key)", host)
        self.cache = cache if cache is not None else SourceCache()
        self.columns = pgcat.ColumnMemory(columns_file)
        self.db = duckdb.connect()
        self.catalog = pgcat.PgCatalog(self.db, duck, self.columns, database=database)
        self.db.execute("SET enable_external_access = false")
        self.db.execute("SET lock_configuration = true")
        self.sessions: Dict[int, Session] = {}
        self._tcp: Optional[_TCPServer] = None
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def from_config(cls, duck: Any, config_path: Optional[str] = None, **overrides: Any) -> "PGServer":
        """From ``duckduck.json``'s ``pg_server`` section (``overrides`` win): host, port, database, tls, users."""
        section = _section(config_path)
        base = os.path.dirname(os.path.abspath(config_path)) if config_path else os.getcwd()
        options: Dict[str, Any] = {k: section[k] for k in ("host", "port", "database", "ssl_required")
                                   if k in section}
        tls = section.get("tls")
        if tls:
            options["tls"] = (_path(tls["cert"], base), _path(tls["key"], base))
        if section.get("authentication"):
            users = duck.resolve_credentials(section["authentication"], "pg_server.authentication")
            options["users"] = {str(k): str(v) for k, v in users.items()}
        env = os.environ.get("DUCKDUCK_PG_PASSWORD")
        if env and "users" not in options:
            options["password"] = env
        options["columns_file"] = _path(section.get("columns_file") or os.path.join(
            os.path.expanduser("~"), ".duckduck", "pg_columns.json"), base)
        if "cache" not in overrides:
            from ..cache import SourceCache

            options["cache"] = SourceCache.from_config(_section(config_path, "sql_cache"))
        options.update({k: v for k, v in overrides.items() if v is not None})
        return cls(duck, **options)

    def use(self, duck: Any) -> None:
        """The web app reconnected (its config was saved): new sessions read these tables."""
        self.source = duck
        self.catalog.duck = duck

    # -- sessions -----------------------------------------------------------------------------------------

    def session_duck(self, cursor: Any) -> Any:
        """A ``DuckAPI`` over the same tables as ``source``, on this session's connection."""
        from ..core import DuckAPI

        src = self.source
        duck = DuckAPI(stream_pages=getattr(src, "stream_pages", True))
        duck.conn.close()
        duck.conn = cursor
        for attr in ("functions", "service_of", "service_prefix", "failed_services", "views", "view_key",
                     "failed_views", "_streaming_functions", "default_database", "_config_path"):
            if hasattr(src, attr):
                setattr(duck, attr, getattr(src, attr))
        duck.cache = self.cache
        duck.column_listener = self.columns.learn
        return duck

    def client_names(self, sql: str) -> str:
        """What a PostgreSQL client writes for our tables → duckduck's own names.

        ``"s3_data.security".proxy_logs`` (a schema with a dot, quoted) → ``s3_data.security.proxy_logs``;
        ``public.my_view`` → ``my_view``."""
        masked = addresses._masked(sql)
        services = addresses.services(self.source)
        edits: List[Tuple[int, int, str]] = []
        for m in re.finditer(r'"((?:[^"]|"")+)"(?=\s*\.)', masked):
            inner = sql[m.start() + 1:m.end() - 1].replace('""', '"')
            parts = inner.split(".")
            if len(parts) > 1 and parts[0].lower() in services:
                edits.append((m.start(), m.end(), ".".join(addresses._ident(self.source, p) for p in parts)))
        for m in re.finditer(r'(?<![\w."])(?:"?(?:public|main)"?)\s*\.\s*("?)([A-Za-z_]\w*)\1', masked):
            if m.group(2).lower() in self.source.functions:
                edits.append((m.start(), m.end(), m.group(2)))
        for start, end, text in sorted(edits, reverse=True):
            sql = sql[:start] + text + sql[end:]
        return sql

    _TARGET = re.compile(r'\b(?:FROM|JOIN)\s+((?:"(?:[^"]|"")+"|[\w$]+)(?:\s*\.\s*(?:"(?:[^"]|"")+"|[\w$]+))*)', re.I)

    def reads_tables(self, masked: str) -> bool:
        """Whether a query (strings and comments blanked) reads one of duckduck's tables — by name or address."""
        known = addresses.services(self.source)
        functions = self.source.functions
        for m in self._TARGET.finditer(masked):
            parts = [p.lower() for p in addresses._parts(m.group(1))] or [m.group(1).lower()]
            if len(parts) == 1:
                if parts[0] in functions or parts[0].split(".")[0] in known:
                    return True
            elif parts[0] in ("public", "main"):
                if parts[-1] in functions:
                    return True
            elif parts[0].split(".")[0] in known:
                return True
        return any(re.search(rf"\b{re.escape(name)}\s*\(", masked, re.I) for name in functions
                   if "(" in masked and len(name) > 2)

    def cancel(self, pid: int, secret: int) -> None:
        session = self.sessions.get(pid)
        if session is not None and hmac.compare_digest(str(session.secret), str(secret)):
            logger.info("PostgreSQL: cancelling the query of session %d", pid)
            session.cancel()

    # -- running ------------------------------------------------------------------------------------------

    def start(self) -> "PGServer":
        """Listens in a background thread (returns at once)."""
        self._bind()
        self._thread = threading.Thread(target=self._tcp.serve_forever, name="duckduck-pgserver", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._bind()
        self._tcp.serve_forever()

    def _bind(self) -> None:
        if self._tcp is None:
            self.catalog.refresh()
            self._tcp = _TCPServer((self.host, self.port), _Handler)
            self._tcp.pg = self
            self.port = self._tcp.server_address[1]
            logger.info("PostgreSQL endpoint on %s:%d, database %s", self.host, self.port, self.database)

    def shutdown(self) -> None:
        if self._tcp is not None:
            self._tcp.shutdown()
            self._tcp.server_close()
            self._tcp = None

    def url(self) -> str:
        return f"postgresql://{self.host}:{self.port}/{self.database}"


#: ``pg_server`` options in duckduck.json
OPTIONS = {"enabled", "host", "port", "database", "tls", "ssl_required", "authentication", "columns_file"}


def config_problems(section: Any) -> List[str]:
    """What's wrong with a ``pg_server`` section (empty when nothing is)."""
    if section is None:
        return []
    if not isinstance(section, dict):
        return ["pg_server must be an object: {\"enabled\": true, \"port\": 5433, ...}"]
    out = [f"pg_server.{k} isn't an option ({', '.join(sorted(OPTIONS))})" for k in sorted(set(section) - OPTIONS)]
    port = section.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535):
        out.append("pg_server.port must be a port number, e.g. 5433")
    for key in ("enabled", "ssl_required"):
        if key in section and not isinstance(section[key], bool):
            out.append(f"pg_server.{key} must be true or false")
    for key in ("host", "database", "columns_file"):
        if key in section and not (isinstance(section[key], str) and section[key].strip()):
            out.append(f"pg_server.{key} must be text")
    tls = section.get("tls")
    if tls is not None and not (isinstance(tls, dict) and isinstance(tls.get("cert"), str) and isinstance(tls.get("key"), str)):
        out.append("pg_server.tls must be {\"cert\": \"server.crt\", \"key\": \"server.key\"}")
    if section.get("ssl_required") and not tls:
        out.append("pg_server.ssl_required needs pg_server.tls")
    auth = section.get("authentication")
    if auth is not None:
        if not isinstance(auth, dict):
            out.append("pg_server.authentication must be an authentication block (local, aws or azure) "
                       "whose secret is {\"user\": \"password\"}")
        elif auth.get("type", "local") not in ("local", "aws", "azure"):
            out.append("pg_server.authentication.type must be local, aws or azure")
        elif auth.get("type") in ("aws", "azure") and not auth.get("secret_id"):
            out.append(f"pg_server.authentication: {auth.get('type')} needs secret_id")
    host = section.get("host", "127.0.0.1")
    if isinstance(host, str) and host not in LOCAL_HOSTS and not auth and not os.environ.get("DUCKDUCK_PG_PASSWORD"):
        out.append(f"pg_server.host {host} is reachable from other machines: set pg_server.authentication "
                   f"(or DUCKDUCK_PG_PASSWORD when starting) — and tls")
    return out


def _password_check(password: Optional[str], users: Optional[Dict[str, str]]):
    if not password and not users:
        return None

    def check(user: str, given: str) -> bool:
        expected = (users or {}).get(user) if users else password
        return expected is not None and hmac.compare_digest(str(expected).encode(), given.encode())

    return check


def _section(config_path: Optional[str], key: str = "pg_server") -> Any:
    import json

    if not config_path or not os.path.exists(config_path):
        return {} if key == "pg_server" else None
    with open(config_path, encoding="utf-8") as fh:
        data = json.load(fh)
    value = data.get(key) if isinstance(data, dict) else None
    return (value or {}) if key == "pg_server" else value


def _path(value: str, base: str) -> str:
    value = os.path.expanduser(str(value))
    return value if os.path.isabs(value) else os.path.join(base, value)

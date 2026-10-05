"""
The web app's admin side: a SQL console over the registered tables, and the
``duckduck.json`` editor (every option documented, secrets masked).

**SQL console** (``SQLConsole``). Runs ``duck.sql(...)`` — push-down and all
— on a *separate* ``DuckAPI`` that shares the registered functions but whose
own DuckDB connection is locked: ``enable_external_access = false`` (no
files, no network, no ``COPY`` / ``ATTACH`` / ``INSTALL``, no stored
secrets) and ``lock_configuration = true`` (the SQL can't turn it back on).
Only read statements are accepted (``SELECT`` / ``WITH`` / ``FROM`` /
``SHOW`` / ``DESCRIBE`` / ``SUMMARIZE`` / ``EXPLAIN`` / ``VALUES``, one at a
time). The ``duckduck`` log of that one call (which WHERE / LIMIT reached
each API) comes back with the rows.

**Config** (``config_reference``, ``mask``, ``unmask``, ``validate_config``,
``save_config``). The reference lists every option: each connector's
parameters (from ``SERVICE_REGISTRY`` and the wrapper's constructor), the
``authentication`` block, ``ai_providers`` entries and the whole
``semantic`` section (from the Pydantic models, descriptions from their
``#:`` comments). Secret values travel masked (``"***"``) and are put back
from the file on save; a save is validated first and keeps a ``.bak``.
"""

import datetime as dt
import inspect
import json
import logging
import os
import re
import shutil
import threading
import time
import typing
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from ..addresses import address_pattern, services
from ..views import read_only_reason, statements as _statements  # noqa: F401 — the SQL guard, shared with saved tables

from ..system import _NOT_SECRET_SUFFIX, _SECRETISH, MASK, is_secret_key as _is_secret_key, mask  # noqa: F401



# ---------------------------------------------------------------------------
# SQL console
# ---------------------------------------------------------------------------


class SQLConsole:
    """``duck.sql`` for the page — read-only, locked away from files and network."""

    def __init__(self, duck: Any, max_rows: int = 1000, timeout: float = 120.0):
        from duckduck import DuckAPI

        console = DuckAPI()
        self.source = duck  # where saved tables are registered (the console shares its tables)
        console.functions = duck.functions  # shared: what's registered later shows up here too
        console.service_of = duck.service_of
        console.failed_services = getattr(duck, "failed_services", {})  # why a configured connector has no tables
        console.views = getattr(duck, "views", {})
        console.view_key = getattr(duck, "view_key", {})
        console.service_prefix = getattr(duck, "service_prefix", {})  # service.table addresses read the same here
        console.failed_views = getattr(duck, "failed_views", {})
        self._config_path = getattr(duck, "_config_path", None)
        default_db = _config_value(self._config_path, "default_database")
        if isinstance(default_db, str) and default_db.strip():  # the file's name for tables with no database
            duck.default_database = default_db.strip()
        console.default_database = getattr(duck, "default_database", None)
        self._nested: Dict[Optional[str], Tuple[float, List[Dict[str, Any]], List[str], set]] = {}
        console._streaming_functions = getattr(duck, "_streaming_functions", {})
        from ..cache import SourceCache

        try:  # what each source returned, reused for a while — the config's "sql_cache" (on by default)
            console.cache = SourceCache.from_config(_config_value(self._config_path, "sql_cache"))
        except ValueError as exc:
            logging.getLogger("duckduck.admin").warning("sql_cache: %s — using the defaults", exc)
            console.cache = SourceCache()
        console.conn.execute("SET enable_external_access = false")
        console.conn.execute("SET lock_configuration = true")
        self.duck = console
        self.max_rows = max_rows
        self.timeout = timeout
        #: The last results, whole (``keep_results``): the result viewer pages, sorts, searches and exports them.
        self._results: "OrderedDict[str, Tuple[str, Any, List[str]]]" = OrderedDict()  # query, frame, types
        self.keep_results = 5
        self._results_lock = threading.Lock()  # not the query lock: a paused query mustn't hold up the viewer
        self._lock = threading.Lock()  # one DuckDB connection: one query at a time
        self._planner: Any = None  # explain()'s own DuckAPI: checking a query never waits on the one running
        self._plan_lock = threading.Lock()

    @property
    def kql(self) -> Any:
        """The KQL translator: the config file's ``"kql": {"extension": …}`` (relative to it), else the defaults."""
        if getattr(self, "_kql", None) is None:
            from ..kql import KqlTranslator

            extension = None
            if self._config_path:
                try:
                    with open(self._config_path, encoding="utf-8") as fh:
                        extension = ((json.load(fh) or {}).get("kql") or {}).get("extension")
                except (OSError, ValueError, AttributeError):
                    extension = None
                if extension and not os.path.isabs(os.path.expanduser(extension)):
                    extension = os.path.join(os.path.dirname(os.path.abspath(self._config_path)), extension)
            self._kql = KqlTranslator(extension=extension)
        return self._kql

    @kql.setter
    def kql(self, translator: Any) -> None:
        self._kql = translator

    def kql_status(self) -> Dict[str, Any]:
        """For the page's SQL | KQL switch: the translator, and the ADX connectors KQL runs on natively without one."""
        status = dict(self.kql.status())
        status["native"] = sorted({self.duck.service_of[n] for n, f in self.duck.functions.items()
                                   if getattr(f, "__name__", "") == "query" and getattr(f, "__module__", "") == "duckduck.connectors.databases.adx"
                                   and self.duck.service_of.get(n)})
        return status

    def run(self, query: str, debug: bool = False, log: Optional[List[str]] = None, language: str = "sql",
            cache: bool = True) -> Dict[str, Any]:
        """
        Runs one read query. ``cache``: reuse what a source returned for
        the same call within the cache's ttl (``False``: read every source
        again and keep the new reads); the result's ``cache`` says which
        sources came from it and how old they were. ``debug``: the log at
        DEBUG (request bodies, bound parameters, every page) instead of INFO. ``log``: the list the
        lines go to as they're written — a job's page reads it live. Under a
        ``progress.tracking`` (a job) it reports steps, and pause / cancel
        take effect between API calls and pages; ``interrupt()`` stops the
        DuckDB step itself.
        """
        from .. import cache as source_cache
        from ..common import progress

        log = [] if log is None else log
        translation = None
        if language == "kql":  # KQL → the SQL that runs (or the native ADX call) — shown with the result
            from ..kql import KqlError, KqlUnavailable

            progress.step("translating", "Translating the KQL")
            try:
                translation = self.kql.to_sql(query, self.duck)
            except (KqlError, KqlUnavailable) as exc:
                return {"error": str(exc), "columns": [], "rows": [], "row_count": 0, "log": log, "language": "kql"}
            query = translation.sql
        elif language != "sql":
            return {"error": f"unknown language {language!r} (sql or kql)", "columns": [], "rows": [], "row_count": 0, "log": log}
        extra = {"language": language, **({"translation": translation.to_dict()} if translation else {})}
        reason = read_only_reason(query)
        if reason:
            return {"error": reason, "columns": [], "rows": [], "row_count": 0, "log": log, **extra}
        started = time.perf_counter()
        ms = lambda: round((time.perf_counter() - started) * 1000, 1)  # noqa: E731
        if not self._lock.acquire(timeout=self.timeout):
            return {"error": "another SQL query is still running (or paused) — cancel it or wait",
                    "columns": [], "rows": [], "row_count": 0, "log": log, "elapsed_ms": ms()}
        try:
            with _captured_log(log, logging.DEBUG if debug else logging.INFO):
                timer = threading.Timer(self.timeout, self.duck.conn.interrupt)
                timer.start()
                try:
                    with source_cache.refreshing(not cache), source_cache.collecting() as used:
                        progress.step("planning", "Reading the query: what goes to each source")
                        try:  # what each source will be sent, shown before any of them is read
                            progress.note(pushdown=self.plan(query).to_dict())
                        except Exception as exc:  # noqa: BLE001 — the run itself reports a bad query
                            logging.getLogger("duckduck.admin").debug("couldn't plan ahead: %s", exc)
                        self.duck.last_pushdown = None
                        relation = self.duck.sql(query)
                        pushdown = self.duck.last_pushdown
                        progress.step("query", "DuckDB runs the query")
                        types = [str(t) for t in relation.types]
                        df = relation.df()
                except Exception as exc:
                    return {"error": f"{type(exc).__name__}: {exc}", "columns": [], "rows": [], "row_count": 0,
                            "log": log, "elapsed_ms": ms(), **extra}
                finally:
                    timer.cancel()
        finally:
            self._lock.release()
        shown = df.head(self.max_rows)
        result_id = uuid.uuid4().hex[:12]
        tracked = progress.current()
        stopped = dict((tracked.partial.get("stopped") or {})) if tracked is not None and tracked.stopped else None
        with self._results_lock:
            self._results[result_id] = (query, df, types)
            while len(self._results) > self.keep_results:
                self._results.popitem(last=False)
        return {
            "columns": [str(c) for c in df.columns],
            "rows": _json_rows(shown),
            "row_count": int(len(df)), "truncated": len(df) > self.max_rows,
            "elapsed_ms": ms(), "log": log, "debug": debug, "result_id": result_id,
            "cache": {**self.cache_status(), "used": used, "skipped": not cache},
            "pushdown": pushdown.to_dict() if pushdown is not None else None,
            "types": types,  # DuckDB's type of each column, in order
            # "Stop and show": the sources read only partly — the answer covers the rows read, not the table
            "stopped": stopped, **extra,
        }

    def explain(self, query: str, language: str = "sql") -> Dict[str, Any]:
        """What ``run`` would send to each source (``DuckAPI.explain``) — nothing is read: each condition, LIMIT and
        ORDER BY that reaches a source or stays with DuckDB, and the tables that would read more than needed."""
        extra: Dict[str, Any] = {"language": language}
        if language == "kql":
            from ..kql import KqlError, KqlUnavailable

            try:
                translation = self.kql.to_sql(query, self.duck)
            except (KqlError, KqlUnavailable) as exc:
                return {"error": str(exc), **extra}
            query, extra["translation"] = translation.sql, translation.to_dict()
        reason = read_only_reason(query)
        if reason:
            return {"error": reason, **extra}
        try:
            return {"pushdown": self.plan(query).to_dict(), **extra}
        except Exception as exc:  # noqa: BLE001 — a typo, an unknown table: said, like a run's error
            return {"error": f"{type(exc).__name__}: {exc}", **extra}

    def python_code(self, query: str, language: str = "sql") -> Dict[str, Any]:
        """The query as a Python script and a Jupyter notebook to run on another computer — the install line,
        a masked ``duckduck.json`` of only the connectors it reads (``pycode.python_code``)."""
        from .pycode import python_code

        return python_code(self, query, language)

    def plan(self, query: str) -> Any:
        """``DuckAPI.explain`` on a planner of its own — the same tables, its own DuckDB connection — so it runs
        at once, even while a query holds the console (the page checks as you type, and before each run)."""
        with self._plan_lock:
            if self._planner is None:
                from duckduck import DuckAPI

                planner = DuckAPI()
                for attr in ("functions", "service_of", "failed_services", "views", "view_key", "service_prefix",
                             "failed_views", "default_database", "_streaming_functions"):
                    setattr(planner, attr, getattr(self.duck, attr, None))
                planner.pushdown_warnings = False
                planner.conn.execute("SET enable_external_access = false")
                planner.conn.execute("SET lock_configuration = true")
                self._planner = planner
            return self._planner.explain(query)

    def cache_status(self) -> Dict[str, Any]:
        """The source cache: on or off, ttl, reads kept (``duckduck.cache``)."""
        cache = self.duck.cache
        return cache.status() if cache is not None else {"enabled": False}

    def clear_cache(self) -> int:
        """Forgets every kept read; returns how many."""
        return self.duck.cache.clear() if self.duck.cache is not None else 0

    def _kept(self, result_id: str) -> Any:
        with self._results_lock:
            kept = self._results.get(result_id)
        if kept is None:
            raise KeyError("this result isn't kept any more (only the last few are) — run the query again")
        return kept[1]

    def _view(self, result_id: str, sort: Optional[str], desc: bool, q: Optional[str],
              columns: Optional[List[str]]) -> Tuple[Any, str, str, List[Any], List[str]]:
        """A private DuckDB over one kept result: the FROM/WHERE/ORDER BY for its rows filtered and sorted."""
        import duckdb

        df = self._kept(result_id)
        cols = [str(c) for c in df.columns]
        frame = df.copy()
        frame.columns = cols
        frame.insert(0, "__row", range(1, len(frame) + 1))
        con = duckdb.connect()
        con.execute("SET enable_external_access = false")
        con.register("r", frame)
        quote = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
        params: List[Any] = []
        where = ""
        if q:
            where = " WHERE " + " OR ".join(f"contains(lower(CAST({quote(c)} AS VARCHAR)), ?)" for c in cols)
            params = [q.lower()] * len(cols)
        order = (f" ORDER BY {quote(sort)} {'DESC' if desc else 'ASC'} NULLS LAST, __row" if sort in cols
                 else " ORDER BY __row")
        shown = [c for c in (columns or cols) if c in cols] or cols
        return con, f"FROM r{where}", order, params, shown

    def page(self, result_id: str, offset: int = 0, limit: int = 200, sort: Optional[str] = None,
             desc: bool = False, q: Optional[str] = None) -> Dict[str, Any]:
        """One page of a kept result, searched (every column, case-insensitive) and sorted — over all its rows."""
        con, source, order, params, cols = self._view(result_id, sort, desc, q, None)
        try:
            quote = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
            total = con.execute("SELECT count(*) FROM r").fetchone()[0]
            filtered = con.execute(f"SELECT count(*) {source}", params).fetchone()[0]
            limit = max(1, min(int(limit), 1000))
            offset = max(0, int(offset))
            out = con.execute(f"SELECT __row, {', '.join(quote(c) for c in cols)} {source}{order} "
                              f"LIMIT {limit} OFFSET {offset}", params).df()
        finally:
            con.close()
        with self._results_lock:
            kept = self._results.get(result_id)
        types = list(kept[2]) if kept is not None and len(kept) > 2 else []
        return {"columns": cols, "types": types, "rows": _json_rows(out[[c for c in out.columns if c != "__row"]]),
                "row_numbers": [int(n) for n in out["__row"]], "total": int(total), "filtered": int(filtered),
                "offset": offset, "limit": limit}

    def csv(self, result_id: str, sort: Optional[str] = None, desc: bool = False, q: Optional[str] = None,
            columns: Optional[List[str]] = None) -> str:
        """The whole kept result as CSV — searched, sorted and cut to ``columns`` like the viewer shows it."""
        con, source, order, params, cols = self._view(result_id, sort, desc, q, columns)
        try:
            quote = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
            out = con.execute(f"SELECT {', '.join(quote(c) for c in cols)} {source}{order}", params).df()
        finally:
            con.close()
        for c in out.columns:  # nested values as JSON, like the page shows them
            if out[c].dtype == object:
                out[c] = [json.dumps(v, default=str, ensure_ascii=False) if isinstance(v, (dict, list)) or
                          type(v).__name__ == "ndarray" else v for v in
                          (x.tolist() if type(x).__name__ == "ndarray" else x for x in out[c])]
        return out.to_csv(index=False)

    def interrupt(self) -> None:
        """Stops the query DuckDB is running now (a cancelled job; API calls stop at their next checkpoint)."""
        try:
            self.duck.conn.interrupt()
        except Exception:
            pass

    def connections(self, configured: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Each connector in the config and whether it started: ``services``
        [{name, connector, started, tables, error}], the config file read,
        and how many tables are registered — why the SQL tab's list is what
        it is (a connector that failed to start has no tables).
        """
        failed = self.duck.failed_services
        counts: Dict[str, int] = {}
        for svc in self.duck.service_of.values():
            counts[svc] = counts.get(svc, 0) + 1
        names = list(configured or {}) or sorted(set(counts) | set(failed))
        services = []
        for name in names:
            conf = (configured or {}).get(name) or {}
            f = failed.get(name)
            services.append({
                "name": name, "connector": (f or {}).get("connector") or conf.get("connector") or name,
                "started": f is None,
                "tables": counts.get(name, 0), "error": (f or {}).get("error"),
            })
        from ..addresses import default_database

        return {"config_path": self._config_path, "services": services, "tables": len(self.duck.functions),
                "default_database": default_database(self.duck)}

    def nested(self, refresh: bool = False, ttl: Optional[float] = None, service: Optional[str] = None) -> Dict[str, Any]:
        """
        ``DuckAPI.nested_tables`` for the expanded catalog, per ``service`` (None =
        every connector). Every catalog read is a call to the source, so it's
        kept until ``refresh`` (or ``ttl`` seconds, when given). A refresh says
        which tables are ``new`` since the last read and how many are ``gone``.
        """
        now = time.time()
        cached = self._nested.get(service)
        new: List[str] = []
        gone = 0
        if refresh or cached is None or (ttl is not None and now - cached[0] > ttl):
            tables, notes = self.duck.nested_tables(service=service)
            now_ids = {_nested_id(t) for t in tables}  # a snapshot: the list itself may be the source's own
            if cached is not None:
                new = sorted(now_ids - cached[3])
                gone = len(cached[3] - now_ids)
            cached = self._nested[service] = (now, list(tables), notes, now_ids)
        at, tables, notes, _ = cached
        return {"tables": tables, "notes": notes, "read_at": at, "service": service, "new": new, "gone": gone}

    def _expandable(self, name: str) -> bool:
        """A catalog whose rows feed a registered table function: the expanded catalog can list what's behind it."""
        from ..common.kinds import CATALOG, kind_of, lists_of

        fn = self.duck.functions.get(name)
        method = lists_of(fn) if fn is not None else None
        return bool(method and kind_of(fn) == CATALOG and self.duck.sibling_function(fn, method))

    def tables(self) -> List[Dict[str, Any]]:
        df = self.duck.list_tables()
        kinds = {name: source_kind(self.duck.functions.get(name)) for name in df["name"]}
        out = df[["name", "kind", "usage", "pushdown", "source", "description"]].astype(object)
        records = out.where(out.notna(), None).to_dict(orient="records")
        for r in records:
            r["icon"] = kinds.get(r["name"], "api")
            r["service"] = self.duck.service_of.get(r["name"])
            r["expandable"] = self._expandable(r["name"])
            r["saved_name"] = self.duck.view_key.get(r["name"])  # the saved table's own name (maybe an address)
            saved = self.duck.views.get(r["saved_name"]) if r["saved_name"] else None
            r["saved"] = None if saved is None else ("bound" if saved.get("table") else "query")
            if saved is not None and "." in r["saved_name"]:  # listed under the connector its name starts with
                first = r["saved_name"].split(".")[0].lower()
                if first in services(self.duck):
                    r["service"] = first
            # how it's written by address (nvd.cves); a service's table function by its pattern (s3_data.<database>.<table_name>)
            r["address"] = self.duck.address_of(r["name"]) if r["kind"] == "table" else None
            r["default_address"] = _default_address(self.duck, r["name"], r["address"])
            pattern = None if r["address"] else address_pattern(self.duck, r["name"])
            if r["address"]:
                r["usage"] = f"SELECT * FROM {r['address']} LIMIT 100"
            elif pattern:
                r["usage"] = f"SELECT * FROM {pattern} LIMIT 100"
                r["address_pattern"] = pattern
            # the page's ⓘ, on every table: what it is, what it takes (optional arguments too) and how to call it
            fn = self.duck.functions.get(r["name"])
            r["params"] = _params_of(fn)
            r["doc"] = (inspect.getdoc(fn) or "") if fn is not None else ""
        return records


def _config_value(path: Optional[str], key: str) -> Any:
    """One top-level value of the config file, or None."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data.get(key) if isinstance(data, dict) else None


def _default_address(duck: Any, name: str, address: Optional[str]) -> Optional[str]:
    """``nvd.default.cves`` for ``nvd.cves`` — when it reads back to the same table (never where
    ``default`` would be one of the connector's real databases)."""
    from ..addresses import _parts, default_database, resolve

    if not address:
        return None
    parts = _parts(address)
    if len(parts) != 2:
        return None
    alt = f"{address.split('.', 1)[0]}.{default_database(duck)}.{address.split('.', 1)[1]}"
    try:
        back = resolve(duck, _parts(alt))
    except ValueError:
        return None
    return alt if back is not None and back[0] == name and not back[1] else None


def _nested_id(t: Dict[str, Any]) -> str:
    """One table behind a catalog, stable across reads and saves: its catalog, table function and arguments."""
    args = sorted((str(k), str(v)) for k, v in (t.get("args") or {}).items())
    return f"{t.get('catalog')}|{t.get('table')}|{json.dumps(args)}"


def nested_group(duck: Any, table: str, args: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    (database, name) of a table behind a catalog, for the SQL tab's grouping:
    every required argument but the last is its database (``s3_data_table(database=, table_name=)``
    → ``security``, ``proxy_logs``); a function with one argument has no database.
    """
    from ..common.kinds import required_params

    from ..addresses import native_database

    fn = duck.functions.get(table)
    required = [p.name for p in required_params(fn)] if fn is not None else []
    if not required or not all(r in (args or {}) for r in required):
        return None, None
    name = str(args[required[-1]])
    if len(required) < 2:  # the table's name only: the connection's own database, when it has one (ADX, SQL)
        return native_database(duck, table), name
    return ".".join(str(args[r]) for r in required[:-1]), name


def _params_of(fn: Any) -> List[Dict[str, Any]]:
    """A table function's parameters for the page: name, required, type, default (``where``/``limit`` left out)."""
    out: List[Dict[str, Any]] = []
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return out
    for p in params:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) or p.name in ("where", "limit"):
            continue
        ann = p.annotation
        kind = ("" if ann is inspect.Parameter.empty else ann.__name__ if isinstance(ann, type)
                else str(ann).replace("typing.", ""))  # Optional[str], not "Optional"
        out.append({"name": p.name, "required": p.default is inspect.Parameter.empty, "type": kind,
                    "default": None if p.default is inspect.Parameter.empty else repr(p.default)})
    return out


def _json_rows(df: Any) -> List[List[Any]]:
    """Rows as lists, NULL/NaN → None."""
    return df.astype(object).where(df.notna(), None).values.tolist()


class _ThreadLog(logging.Handler):
    MAX_LINES = 2000

    def __init__(self, sink: List[str], level: int = logging.INFO):
        super().__init__(level)
        self.sink, self.thread = sink, threading.get_ident()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread and len(self.sink) < self.MAX_LINES:
            try:
                self.sink.append(self.format(record))
            except Exception:
                pass


class _captured_log:
    """The ``duckduck`` log lines this thread writes during the block (at ``level`` — INFO — whatever the global level)."""

    def __init__(self, sink: List[str], level: int = logging.INFO):
        self.sink, self.want = sink, level

    def __enter__(self):
        self.logger = logging.getLogger("duckduck")
        self.handler = _ThreadLog(self.sink, self.want)
        self.level = self.logger.level
        self.logger.addHandler(self.handler)
        if self.logger.getEffectiveLevel() > self.want:
            self.logger.setLevel(self.want)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.level)


#: A registered function's module → the kind of source it is (the page draws an icon per kind).
SOURCE_KINDS = {
    "duckduck.connectors.api.sharepoint": "sharepoint", "duckduck.connectors.api.insightvm": "insightvm", "duckduck.connectors.api.servicenow": "servicenow",
    "duckduck.connectors.api.axonius": "axonius", "duckduck.connectors.databases.sql": "database", "duckduck.connectors.lake.glue": "glue", "duckduck.connectors.databases.athena": "athena",
    "duckduck.connectors.api.airflow": "airflow",
    "duckduck.connectors.lake.blob_storage": "blob_storage", "duckduck.connectors.databases.adx": "adx", "duckduck.connectors.local.files": "files",
    "duckduck.connectors.local.python_source": "python", "duckduck.semantic.takeover": "dataset", "duckduck.views": "dataset",
}


def source_kind(fn: Any) -> str:
    module = getattr(fn, "__module__", None) or getattr(type(fn), "__module__", "") or ""
    for prefix, kind in SOURCE_KINDS.items():
        if module == prefix or module.startswith(prefix + "."):
            return kind
    return "api"


# ---------------------------------------------------------------------------
# Config: reference
# ---------------------------------------------------------------------------


def config_reference() -> Dict[str, Any]:
    """Every option of ``duckduck.json``, documented — what the Config tab lists."""
    from duckduck.connectors.registry import SERVICE_REGISTRY

    from .config import AIProviderConfig, SemanticConfig

    connectors = []
    for name, spec in SERVICE_REGISTRY.items():
        cls = getattr(spec.factory, "__self__", None)
        doc = _first_paragraph(inspect.getdoc(spec.factory) or inspect.getdoc(cls) or "")
        options = []
        if cls is not None:
            try:
                hints = typing.get_type_hints(cls.__init__)  # resolves ``from __future__ import annotations``
            except Exception:
                hints = {}
            for p in list(inspect.signature(cls.__init__).parameters.values())[1:]:
                if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                if p.name in _INJECTED:
                    continue  # a Python object for tests / DI — can't come from JSON
                p = p.replace(annotation=hints.get(p.name, p.annotation))
                options.append({"name": p.name, "type": _type_name(p.annotation),
                                "default": None if p.default is p.empty else _jsonable(p.default),
                                "required": p.default is p.empty, "secret": _is_secret_key(p.name),
                                "credential": _is_secret_key(p.name) or p.name in _CREDENTIAL_NAMES,
                                **_input_of(p.annotation)})
        connectors.append({
            "connector": name, "class": cls.__name__ if cls else "", "description": doc, "options": options,
            "icon": source_kind(cls) if cls is not None else "api",
            "tables": sorted(spec.tables), "dynamic_tables": bool(spec.dynamic_tables),
            "requires_authentication": spec.requires_authentication, "path_options": list(spec.path_options),
        })
    return {
        "top_level": [
            {"name": "services", "type": "object", "description": "One entry per connection: {name: {connector, "
             "authentication, ...options}}. Its tables are registered as <name>_<table> (or table_prefix)."},
            {"name": "on_error", "type": "raise | warn", "default": "raise", "input": "choice",
             "choices": ["raise", "warn"],
             "description": "warn: a service that fails to connect is skipped with a warning instead of stopping."},
            {"name": "ai_providers", "type": "object", "description": "Named LLMs and decision engines, "
             "referenced by name from semantic (default_llm, decision_engine.ai_provider, ...)."},
            {"name": "views", "type": "object", "description": "Saved tables: {name: {\"table\": <a registered "
             "table function>, \"args\": {...}} or {\"sql\": \"SELECT ...\"}, optional description}. A bound one is "
             "that table function with its arguments fixed (push-down unchanged); a query runs each time it's read."},
            {"name": "semantic", "type": "object", "description": "Natural-language search: catalog, decision "
             "engine, extractor, catalog generation, feedback, thresholds..."},
        ],
        "service_common": [
            {"name": "connector", "type": "string", "input": "text", "description": "Which connector (below). Default: the "
             "service's own name."},
            {"name": "table_prefix", "type": "string", "input": "text", "description": "Tables are registered as <prefix>_<table>; "
             "\"\" for bare table names. Default: the service's name."},
            {"name": "max_parallel", "type": "integer", "input": "int", "description": "How many requests of one "
             "table run at once (pages read in threads, a join's split calls). Default: what each table declares "
             "(ServiceNow and InsightVM 4, Airflow 2); APIs read one page after another (cursors, NVD) ignore it."},
            {"name": "retry", "type": "object", "input": "json", "description": "HTTP connectors (servicenow, "
             "insightvm, axonius, sharepoint, restcountries): a timeout, a dropped connection, 429 or 5xx is tried "
             "again. {\"retries\": 3, \"backoff\": 2, \"max_wait\": 60, \"timeout\": 60, \"statuses\": [429, 500, "
             "502, 503, 504]} — any of these keys; false = no retries. timeout = seconds per request (default: the "
             "connector's, 30–60)."},
            {"name": "authentication", "type": "object", "description": "Where credentials come from — below."},
        ],
        "authentication": [
            {"name": "type", "type": "local | aws | azure", "default": "local", "input": "choice",
             "choices": ["local", "aws", "azure"],
             "description": "local: the other keys of this block are the credentials. aws: AWS Secrets Manager. "
             "azure: Azure Key Vault."},
            {"name": "secret_id", "type": "string", "input": "text", "description": "aws / azure: the secret's name or ARN."},
            {"name": "region_name", "type": "string", "input": "text", "description": "aws: the Secrets Manager region."},
            {"name": "profile_name", "type": "string", "input": "text", "description": "aws: a named profile from ~/.aws."},
            {"name": "vault_url", "type": "string", "input": "text", "description": "azure: https://<vault>.vault.azure.net."},
            {"name": "tenant_id", "type": "string", "input": "text", "description": "azure: pin DefaultAzureCredential to a tenant."},
            {"name": "<any key>", "type": "value | \"$secret.<key>\"",
             "description": "Added on top of the fetched secret; \"$secret.<key>\" copies <key> from it."},
        ],
        "connectors": connectors,
        #: which keys the page treats as secrets (the same rule as ``mask``)
        "secrets": {"pattern": _SECRETISH.pattern, "not_suffixes": list(_NOT_SECRET_SUFFIX), "mask": MASK},
        "ai_provider": _model_options(AIProviderConfig),
        "semantic": _model_options(SemanticConfig, skip={"ai_providers", "base_dir", "config_file"}),
    }


def _model_options(model: Any, skip: Tuple[str, ...] = (), depth: int = 0) -> List[Dict[str, Any]]:
    from pydantic import BaseModel

    docs = _attr_docs(model)
    out = []
    for name, field in model.model_fields.items():
        if name in skip:
            continue
        key = field.alias or name
        entry: Dict[str, Any] = {
            "name": key, "type": _type_name(field.annotation), "description": docs.get(name, ""),
            "default": None if field.is_required() else _jsonable(field.get_default(call_default_factory=True)),
            "required": field.is_required(), **_input_of(field.annotation),
        }
        nested = _nested_model(field.annotation)
        if nested is not None and issubclass(nested, BaseModel) and depth < 3 and entry["input"] == "object":
            entry["options"] = _model_options(nested, depth=depth + 1)
            entry["default"] = None
            own_doc = nested.__dict__.get("__doc__")  # not BaseModel's own docstring
            entry["description"] = entry["description"] or _first_paragraph(inspect.cleandoc(own_doc) if own_doc else "")
        out.append(entry)
    return out


#: Constructor parameters that only take Python objects (a client, a session) — not config options.
_INJECTED = {"client", "kcsb", "session", "credential", "engine", "sleep"}
#: Non-secret parameters that are still credentials: the form offers them in the authentication block.
_CREDENTIAL_NAMES = {"username", "user", "client_id", "tenant_id", "aws_access_key_id"}


def _input_of(annotation: Any) -> Dict[str, Any]:
    """
    How the Config form edits a value of this type: ``choice`` (+ ``choices``),
    ``bool``, ``int``, ``float``, ``text``, ``list`` (of strings), ``object``
    (a nested section, its ``options`` listed), ``scalar`` (a number or text)
    or ``json`` (anything else — a dict, a list of objects, a union of shapes).
    """
    from pydantic import BaseModel

    if annotation is inspect.Parameter.empty or annotation is Any:
        return {"input": "scalar"}
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        if len(args) == 1:
            return _input_of(args[0])
        if all(a in (int, float, str) for a in args):
            return {"input": "scalar"}
        return {"input": "json"}
    if origin is typing.Literal:
        return {"input": "choice", "choices": [a for a in typing.get_args(annotation) if a is not None]}
    if origin in (list, List) and args and args[0] is str:
        return {"input": "list"}
    if inspect.isclass(annotation):
        if issubclass(annotation, BaseModel):
            return {"input": "object"}
        for kind, name in ((bool, "bool"), (int, "int"), (float, "float"), (str, "text")):
            if issubclass(annotation, kind):
                return {"input": name}
    return {"input": "json"}


def _nested_model(annotation: Any) -> Any:
    from pydantic import BaseModel

    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        found = _nested_model(arg)
        if found is not None:
            return found
    return None


def _attr_docs(cls: Any) -> Dict[str, str]:
    """``#:`` comments right above each attribute of a class (and its bases)."""
    docs: Dict[str, str] = {}
    for klass in reversed(cls.__mro__):
        try:
            lines = inspect.getsource(klass).splitlines()
        except (OSError, TypeError):
            continue
        pending: List[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#:"):
                pending.append(stripped[2:].strip())
                continue
            m = re.match(r"([A-Za-z_]\w*)\s*:", stripped)
            if m and pending:
                docs[m.group(1)] = " ".join(pending)
            if stripped and not stripped.startswith("#"):
                pending = []
    return docs


def _type_name(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty or annotation is None:
        return ""
    if inspect.isclass(annotation):
        return annotation.__name__
    text = str(annotation).replace("typing.", "")
    text = re.sub(r"\b[\w.]+\.(\w+)", r"\1", text)
    return text.replace("NoneType", "None")


def _first_paragraph(doc: str) -> str:
    return doc.strip().split("\n\n")[0].replace("\n", " ") if doc else ""


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return str(value)


# ---------------------------------------------------------------------------
# Config: masking, validation, saving
# ---------------------------------------------------------------------------


def unmask(edited: Any, saved: Any, path: str = "") -> Any:
    """Puts the saved secret back wherever the edited config still says ``"***"``."""
    if isinstance(edited, dict):
        return {k: unmask(v, saved.get(k) if isinstance(saved, dict) else None, f"{path}.{k}" if path else k)
                for k, v in edited.items()}
    if isinstance(edited, list):
        return [unmask(v, saved[i] if isinstance(saved, list) and i < len(saved) else None, f"{path}[{i}]")
                for i, v in enumerate(edited)]
    if edited == MASK:
        if not isinstance(saved, str) or saved == MASK:
            raise ValueError(f"{path}: \"***\" stands for a saved secret, but there is none there — type the value")
        return saved
    return edited


def validate_config(data: Any, path: str = "") -> Dict[str, List[str]]:
    """``{"errors": [...], "warnings": [...]}`` — structure, connectors, authentication, the semantic section."""
    from duckduck.connectors.registry import SERVICE_REGISTRY

    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(data, dict):
        return {"errors": ["the config must be a JSON object"], "warnings": []}
    known = {"services", "on_error", "ai_providers", "semantic", "views", "kql", "default_database", "sql_cache",
             "pg_server", "catalogs", "lake", "system_tables"}
    for key in sorted(set(data) - known):
        warnings.append(f"unknown top-level key {key!r} (known: {', '.join(sorted(known))})")
    if data.get("system_tables") not in (None, True, False):
        errors.append("system_tables is true or false (the duckduck.* tables; on by default)")
    if data.get("on_error") not in (None, "raise", "warn"):
        errors.append("on_error must be \"raise\" or \"warn\"")
    services = data.get("services", {})
    if not isinstance(services, dict):
        errors.append("services must be an object: {name: {connector, authentication, ...}}")
        services = {}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            errors.append(f"services.{name} must be an object")
            continue
        connector = svc.get("connector", name)
        spec = SERVICE_REGISTRY.get(connector)
        if spec is None:
            errors.append(f"services.{name}: unknown connector {connector!r} "
                          f"(known: {', '.join(sorted(SERVICE_REGISTRY))})")
            continue
        auth = svc.get("authentication")
        if auth is None:
            if spec.requires_authentication:
                errors.append(f"services.{name}: an authentication block is required for {connector!r}")
        elif not isinstance(auth, dict):
            errors.append(f"services.{name}.authentication must be an object")
        else:
            kind = auth.get("type", "local")
            if kind not in ("local", "aws", "azure"):
                errors.append(f"services.{name}.authentication.type must be local, aws or azure")
            if kind in ("aws", "azure") and not auth.get("secret_id"):
                errors.append(f"services.{name}.authentication: {kind} needs secret_id")
            if kind == "azure" and not auth.get("vault_url"):
                errors.append(f"services.{name}.authentication: azure needs vault_url")
        cls = getattr(spec.factory, "__self__", None)
        if cls is not None:
            params = set(list(inspect.signature(cls.__init__).parameters)[1:])
            takes_any = any(p.kind == p.VAR_KEYWORD for p in inspect.signature(cls.__init__).parameters.values())
            extra = set(svc) - {"connector", "authentication", "table_prefix", "max_parallel", "retry"} - params
            at_once = svc.get("max_parallel")
            if at_once is not None and not (isinstance(at_once, int) and not isinstance(at_once, bool) and at_once >= 1):
                errors.append(f"services.{name}.max_parallel must be a whole number ≥ 1")
            if "retry" in svc:
                from ..common.retry import policy_problem

                problem = policy_problem(svc["retry"])
                if problem:
                    errors.append(f"services.{name}.{problem}")
            if extra and not takes_any:
                warnings.append(f"services.{name}: {', '.join(sorted(extra))} — not an option of {connector!r} "
                                f"(options: {', '.join(sorted(params))})")
    default_db = data.get("default_database")
    if default_db is not None and not (isinstance(default_db, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", default_db)):
        errors.append("default_database must be a plain name (letters, digits and _), e.g. \"duckdefault\"")
    if "lake" in data:
        from ..pipeline.settings import lake_problems

        errors.extend(lake_problems(data["lake"]))
    catalogs = data.get("catalogs", {})
    if not isinstance(catalogs, dict) or not all(isinstance(v, dict) for v in catalogs.values()):
        errors.append("catalogs must be an object: {name: {type: glue|unity|iceberg, ...}}")
    else:
        for name, block in catalogs.items():
            if block.get("type") not in ("glue", "unity", "iceberg"):
                errors.append(f"catalogs.{name}: type must be glue, unity or iceberg")
    if "pg_server" in data:
        from ..pgserver.server import config_problems

        errors.extend(config_problems(data["pg_server"]))
    if "sql_cache" in data:
        from ..cache import SourceCache

        try:
            SourceCache.from_config(data["sql_cache"])
        except ValueError as exc:
            errors.append(str(exc))
    views = data.get("views", {})
    if not isinstance(views, dict):
        errors.append("views must be an object: {name: {table, args} | {sql}}")
        views = {}
    for name, definition in views.items():
        from ..views import DOTTED_RE, NAME_RE, clean_definition

        if not (NAME_RE.match(str(name)) or DOTTED_RE.match(str(name))):
            errors.append(f"views.{name}: a table name is lowercase letters, digits and _, starting with a letter "
                          f"— or an address, connector.database.table")
        try:
            clean_definition(definition)
        except ValueError as exc:
            errors.append(f"views.{name}: {exc}")
    if "semantic" in data:
        from .config import SemanticConfig

        try:
            SemanticConfig.from_file_data(data, path)
        except Exception as exc:  # pydantic ValidationError or ValueError — both read well
            errors.append(f"semantic: {exc}")
    return {"errors": errors, "warnings": warnings}


def save_config(path: str, edited: Dict[str, Any]) -> Dict[str, Any]:
    """Unmasks, validates and writes the config (keeping ``<path>.bak``). Raises ``ValueError`` if invalid."""
    saved = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
    data = unmask(edited, saved)
    report = validate_config(data, path)
    if report["errors"]:
        raise ValueError("; ".join(report["errors"]))
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return {"saved": path, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "warnings": report["warnings"]}

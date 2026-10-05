"""
Generic relational database wrapper for use with DuckAPI.

Works with any SQLAlchemy-supported engine — SQL Server, MySQL,
PostgreSQL, SQLite, Oracle, etc. — via that engine's dialect/driver
package (``pyodbc``, ``PyMySQL``, ``psycopg2``, ...). One ``SQLDatabase``
instance is one connection; unlike SharePoint/InsightVM there's no fixed
set of endpoints, so tables are read through the same
structural-parameter convention already used for ``asset_id``/``site_id``
elsewhere: ``table_name`` builds the query, so nothing needs to be
enumerated ahead of time in an ``auto_register()`` config.

Usage
-----
::

    from duckduck import DuckAPI, SQLDatabase

    db = SQLDatabase("postgresql+psycopg2://user:pass@host:5432/mydb")
    duck = DuckAPI()
    duck.register_api_function("db_table", db.table)
    duck.register_api_function("db_query", db.query)

    duck.sql(
        "SELECT * FROM db_table(table_name='public.customers')"
        " WHERE status = 'active' LIMIT 50"
    ).df()

    duck.sql(
        "SELECT * FROM db_query(sql='SELECT * FROM orders WHERE total > 100')"
    ).df()

Via ``auto_register()`` (connector ``"database"``, registered as
``{name}_table`` / ``{name}_query``)::

    duck.auto_register({
        "sqlserver": {
            "connector": "database",
            "authentication": {
                "type": "local",
                "connection_string": (
                    "mssql+pyodbc://user:pass@host:1433/db"
                    "?driver=ODBC+Driver+17+for+SQL+Server"
                ),
            },
        },
    })
    duck.sql("SELECT * FROM sqlserver_table(table_name='dbo.Customers')").df()
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

import pandas as pd

from ...common.kinds import catalog, raw_query
from ...common.logs import get_logger
from ...common.pushdown import Condition, sortable
from ...common.sparkplan import SparkSource, spark_plan
from ...common.idempotency import checkpoint

logger = get_logger("database")

try:
    import sqlalchemy as sa
except ImportError:
    sa = None


class SQLDatabase:
    """
    Generic SQL database client, built on SQLAlchemy.

    Parameters
    ----------
    connection_string : str
        A SQLAlchemy URL. Examples:

        - SQL Server: ``mssql+pyodbc://user:pass@host:1433/db?driver=ODBC+Driver+17+for+SQL+Server``
        - MySQL:      ``mysql+pymysql://user:pass@host:3306/db``
        - PostgreSQL: ``postgresql+psycopg2://user:pass@host:5432/db``
        - Oracle:     ``oracle+oracledb://user:pass@host:1521/?service_name=ORCLPDB1`` (python-oracledb,
          thin mode: no Instant Client; a database part instead is a SID to SQLAlchemy)
        - SQLite:     ``sqlite:///path/to/file.db``
    **engine_kwargs
        Passed straight to ``sqlalchemy.create_engine`` (e.g. ``pool_size``).
    """

    #: what a failed batched pipeline read does next (duckduck.common.idempotency)
    IDEMPOTENCY = checkpoint("ORDER BY and WHERE col > value run in the database — name a column that only grows (an id, a creation date)")

    #: ``where`` applies join key values as ``col IN (…)`` — bound parameters, at most ``IN_MAX`` per query
    #: (SQL Server takes 2100 parameters): DuckAPI splits more into several calls
    WHERE_OPS = frozenset({"eq", "like", "ilike", "gt", "gte", "lt", "lte", "in"})
    IN_MAX = 1000

    def __init__(self, connection_string: str, **engine_kwargs):
        if sa is None:
            raise ImportError(
                "sqlalchemy is required for database connectors.\n"
                "Install with:  pip install \"duckduck[database]\"\n"
                "plus the driver package for your engine, e.g. pyodbc "
                "(SQL Server), PyMySQL (MySQL), psycopg2-binary (PostgreSQL)."
            )
        self.engine = sa.create_engine(connection_string, **engine_kwargs)
        self._metadata = sa.MetaData()
        self._reflected: Dict[str, Any] = {}

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "SQLDatabase":
        """
        Builds SQLDatabase from a credentials dict (e.g. a secret fetched
        by ``auto_register()`` from AWS Secrets Manager / Azure Key Vault).

        Accepts either:

        - ``connection_string``: a full SQLAlchemy URL — the same shape for
          every engine. ``${key}`` in it is filled from the secret, URL-escaped
          (``oracle+oracledb://${username}:${password}@db:1521/?service_name=X``):
          the URL in the config, the credentials in the vault.
        - discrete fields: ``drivername`` (required, e.g. ``"mssql+pyodbc"``,
          ``"mysql+pymysql"``, ``"postgresql+psycopg2"``), plus
          ``username``, ``password``, ``host``, ``port``, ``database``,
          and an optional ``query`` dict of driver-specific URL params
          (e.g. ``{"driver": "ODBC Driver 17 for SQL Server"}``) —
          assembled via ``sqlalchemy.engine.URL.create``. This shape
          matches what many managed secrets (e.g. AWS RDS) already store.
        """
        connection_string = overrides.pop("connection_string", None) or secret.get(
            "connection_string"
        )
        if connection_string:
            return cls(cls._filled(connection_string, secret), **overrides)

        if "drivername" not in secret:
            raise ValueError(
                "SQLDatabase secret needs either 'connection_string' or "
                "'drivername' (+ username/password/host/port/database)."
            )
        url = sa.engine.URL.create(
            drivername=secret["drivername"],
            username=secret.get("username"),
            password=secret.get("password"),
            host=secret.get("host"),
            port=secret.get("port"),
            database=secret.get("database"),
            query=secret.get("query") or {},
        )
        return cls(url, **overrides)

    _PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")

    @classmethod
    def _filled(cls, connection_string: str, secret: Dict[str, Any]) -> str:
        """``${key}`` → the secret's value, escaped for a URL (a password with ``@``, ``/`` or ``:`` stays one
        password). A key the secret doesn't have is an error naming the keys it has — never sent as written."""
        connection_string = str(connection_string)

        def value(match: "re.Match[str]") -> str:
            key = match.group(1).strip()
            if key not in secret or secret[key] is None:
                have = ", ".join(sorted(k for k in secret if k != "connection_string")) or "none"
                raise ValueError(f"connection_string has ${{{key}}}, but the secret has no {key!r} "
                                 f"(it has: {have}) — add it to the secret, or rename it in the "
                                 f"authentication block: \"{key}\": \"$secret.<its name>\"")
            return quote(str(secret[key]), safe="")

        return cls._PLACEHOLDER.sub(value, connection_string)

    # ------------------------------------------------------------------
    # Schema reflection
    # ------------------------------------------------------------------

    def _reflect(self, table_name: str) -> sa.Table:
        """Reflects (and caches) a table's schema by name, e.g. 'dbo.Customers'."""
        if table_name not in self._reflected:
            schema, _, name = table_name.rpartition(".")
            self._reflected[table_name] = sa.Table(
                name, self._metadata, schema=schema or None, autoload_with=self.engine
            )
        return self._reflected[table_name]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _where_clauses(self, tbl: "sa.Table", where: Optional[List[Condition]]) -> list:
        """
        SQLAlchemy clauses for DuckAPI push-down conditions. Columns are
        matched case-insensitively (DuckAPI lowercases names); conditions
        on columns the table doesn't have are skipped. Values are bound
        parameters, never spliced into the SQL.
        """
        columns = {c.name.lower(): c for c in tbl.columns}
        clauses = []
        for cond in where or []:
            col = columns.get(cond.column.lower())
            if col is None:
                continue
            if cond.op in ("like", "ilike"):
                pattern, escape = self._portable_like(str(cond.value))
                method = col.like if cond.op == "like" else col.ilike
                clauses.append(method(pattern, escape=escape))
            elif cond.op == "eq":
                clauses.append(col == self._typed(col, cond.value))
            elif cond.op == "gt":
                clauses.append(col > self._typed(col, cond.value))
            elif cond.op == "gte":
                clauses.append(col >= self._typed(col, cond.value))
            elif cond.op == "lt":
                clauses.append(col < self._typed(col, cond.value))
            elif cond.op == "lte":
                clauses.append(col <= self._typed(col, cond.value))
            elif cond.op == "in":
                clauses.append(col.in_([self._typed(col, v) for v in cond.value or ()]))
        return clauses

    @staticmethod
    def _typed(col: Any, value: Any) -> Any:
        """A date/time column's value as a Python date/datetime/time, bound as that type: ``WHERE opened_at >=
        '2026-10-01'`` arrives as text (DuckDB's ``TIMESTAMP '…'`` too), and Oracle reads a text bound to a DATE
        with the session's NLS_DATE_FORMAT (ORA-01843) — other engines cast it. A DATE column compared with a
        moment that isn't midnight keeps the moment (a date would move the bound). Text that isn't a plain
        date/time, or carries an offset, stays as it was."""
        if not isinstance(value, str):
            return value
        try:
            kind = col.type.python_type
        except (NotImplementedError, AttributeError):
            return value
        try:
            if kind is dt.time:
                return dt.time.fromisoformat(value.strip())
            if kind not in (dt.datetime, dt.date):
                return value
            moment = dt.datetime.fromisoformat(value.strip().replace("T", " ", 1))
        except ValueError:
            return value
        if moment.tzinfo is not None:
            return value
        if kind is dt.date and moment.time() == dt.time():
            return moment.date()
        return moment

    def _portable_like(self, pattern: str):
        """
        Makes a DuckDB LIKE pattern (only ``%``/``_`` are special, no escape
        character) mean the same thing on this engine: backslash is escaped
        and declared as the ESCAPE character (MySQL treats it as one by
        default), and on SQL Server ``[`` — a character-class opener there —
        is escaped too.
        """
        translated = pattern.replace("\\", "\\\\")
        if self.engine.dialect.name == "mssql":
            translated = translated.replace("[", "\\[")
        return translated, "\\"

    # Spark (``duckduck.spark``): JDBC — the driver jar must be on Spark's classpath
    _JDBC = {"postgresql": ("jdbc:postgresql://{host}:{port}/{db}", "org.postgresql.Driver", 5432),
             "mysql": ("jdbc:mysql://{host}:{port}/{db}", "com.mysql.cj.jdbc.Driver", 3306),
             "mariadb": ("jdbc:mariadb://{host}:{port}/{db}", "org.mariadb.jdbc.Driver", 3306),
             "mssql": ("jdbc:sqlserver://{host}:{port};databaseName={db}", "com.microsoft.sqlserver.jdbc.SQLServerDriver", 1433),
             "oracle": ("jdbc:oracle:thin:@//{host}:{port}/{db}", "oracle.jdbc.OracleDriver", 1521)}

    def jdbc_options(self) -> Dict[str, str]:
        """Spark's JDBC options for this connection: url, driver, user, password."""
        url = self.engine.url
        name = url.get_backend_name()
        if name == "sqlite":
            return {"url": f"jdbc:sqlite:{url.database}", "driver": "org.sqlite.JDBC"}
        if name not in self._JDBC:
            raise ValueError(f"no JDBC mapping for {name!r} — add it to SQLDatabase._JDBC")
        template, driver, port = self._JDBC[name]
        if name == "oracle":
            template = self._oracle_jdbc(url)
        options = {"url": template.format(host=url.host, port=url.port or port, db=url.database or ""),
                   "driver": driver}
        if url.username:
            options["user"] = url.username
        if url.password:
            options["password"] = str(url.password)
        return options

    @staticmethod
    def _oracle_jdbc(url: Any) -> str:
        """Oracle's thin URL as SQLAlchemy reads the connection: ``?service_name=`` → ``@//host:port/service``,
        a database part (a SID to SQLAlchemy) → ``@host:port:SID``, a host alone (a TNS alias) → ``@alias``."""
        service = url.query.get("service_name")
        if service:
            return "jdbc:oracle:thin:@//{host}:{port}/" + str(service)
        if url.database:
            return "jdbc:oracle:thin:@{host}:{port}:{db}"
        return "jdbc:oracle:thin:@{host}"

    def _spark_table(self, table_name: str) -> SparkSource:
        return SparkSource("jdbc", options={**self.jdbc_options(), "dbtable": table_name})

    def _spark_query(self, sql: str) -> SparkSource:
        return SparkSource("jdbc", options={**self.jdbc_options(), "query": sql})

    @spark_plan("native", source="_spark_table", why="a database table: Spark reads it over JDBC")
    @sortable(exact=True)
    def table(
        self,
        table_name: str,
        where: Optional[List[Condition]] = None,
        order_by: Optional[List[Tuple[str, bool]]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Reads rows from a table.

        Parameters
        ----------
        table_name : str
            Structural — builds the query (inline only, e.g.
            ``db_table(table_name='dbo.Customers')``). Schema-qualified
            names are supported (``"dbo.Customers"``, ``"public.customers"``).
        limit : int, optional
            Pushed down server-side via SQLAlchemy's ``.limit()``, which
            translates to the right per-dialect syntax (``TOP`` for SQL
            Server, ``LIMIT`` for MySQL/PostgreSQL/SQLite).

        where : list[Condition], optional
            Filled by DuckAPI's push-down: every simple ``WHERE`` condition
            (``=``, ``LIKE``, ``ILIKE``, ``>``, ``>=``, ``<``, ``<=``) runs
            server-side as a real ``WHERE`` in the database's own dialect,
            so only matching rows are transferred.
        order_by : list of (column, descending), optional
            A query's ``ORDER BY … LIMIT n`` (``@sortable(exact=True)``): sorted
            in the database with NULLs last (written as ``CASE WHEN c IS NULL``,
            which every dialect takes) — on number, date/time and boolean columns
            only: text follows the database's collation, which may not be
            DuckDB's, so its order (and the limit) stay with DuckDB.
        """
        tbl = self._reflect(table_name)
        stmt = sa.select(tbl)
        clauses = self._where_clauses(tbl, where)
        if clauses:
            stmt = stmt.where(*clauses)
        if order_by:
            keys = self._order_keys(tbl, order_by)
            if keys is None:
                limit = None  # both or neither: DuckDB sorts and cuts
            else:
                stmt = stmt.order_by(*keys)
        if limit is not None:
            stmt = stmt.limit(limit)
        self._log_statement(stmt)
        return pd.read_sql(stmt, self.engine)

    #: column types whose order is the same here as in DuckDB (text has a collation)
    _ORDERED_TYPES = (sa.Integer, sa.Numeric, sa.Float, sa.Date, sa.DateTime, sa.Time, sa.Boolean, sa.Interval)

    def _order_keys(self, tbl, order_by: List[Tuple[str, bool]]):
        """ORDER BY clauses, NULLs last; None when a column is missing or not of an ordered type."""
        by_name = {c.name.lower(): c for c in tbl.columns}
        keys = []
        for column, descending in order_by:
            col = by_name.get(column.lower())
            if col is None or not isinstance(col.type, self._ORDERED_TYPES):
                return None
            keys += [sa.case((col.is_(None), 1), else_=0), col.desc() if descending else col.asc()]
        return keys

    def _log_statement(self, stmt) -> None:
        """The SQL sent to the database at INFO (placeholders), bound values at DEBUG."""
        if not logger.isEnabledFor(logging.INFO):
            return
        compiled = stmt.compile(dialect=self.engine.dialect)
        logger.info("%s: %s", self.engine.dialect.name, " ".join(str(compiled).split()))
        if compiled.params:
            logger.debug("    params: %s", compiled.params)

    #: Schemas that hold the engine's own metadata, never user data.
    _SYSTEM_SCHEMAS = {
        "information_schema", "pg_catalog", "pg_toast", "sys", "guest", "mysql",
        "performance_schema", "db_owner", "db_accessadmin", "db_securityadmin",
        "db_ddladmin", "db_backupoperator", "db_datareader", "db_datawriter",
        "db_denydatareader", "db_denydatawriter",
    }

    def _user_schemas(self, inspector: Any) -> List[str]:
        """The schemas holding user data. Oracle lists every database user as a schema — dozens of its own
        (SYS, XDB, MDSYS…, thousands of views): ``ALL_USERS.ORACLE_MAINTAINED`` (12c+) leaves them out."""
        names = [s for s in inspector.get_schema_names() if s.lower() not in self._SYSTEM_SCHEMAS]
        if self.engine.dialect.name != "oracle":
            return names
        try:
            with self.engine.connect() as conn:
                ours = {r[0] for r in conn.execute(sa.text("SELECT username FROM all_users WHERE oracle_maintained = 'Y'"))}
        except Exception as exc:  # noqa: BLE001 — before 12c: no such column
            logger.debug("oracle: all_users.oracle_maintained unavailable (%s); listing every schema", exc)
            return names
        maintained = {inspector.dialect.normalize_name(u) for u in ours}
        return [s for s in names if s not in maintained]

    @spark_plan("driver", why="catalog: a small listing")
    @catalog(lists="table")
    def tables(self, schema: Optional[str] = None, limit: Optional[int] = None) -> pd.DataFrame:
        """
        Lists tables and views (every non-system schema, or just ``schema``).
        ``table_name`` is what ``table(table_name=...)`` takes — schema-
        qualified outside the connection's default schema.
        """
        inspector = sa.inspect(self.engine)
        default = inspector.default_schema_name
        schemas = [schema] if schema else self._user_schemas(inspector)
        rows = []
        for sch in schemas:
            for object_type, names in (("table", inspector.get_table_names(schema=sch)),
                                       ("view", inspector.get_view_names(schema=sch))):
                for name in names:
                    qualified = name if sch in (None, default) else f"{sch}.{name}"
                    rows.append({"table_name": qualified, "schema": sch, "name": name, "object_type": object_type})
                    if limit is not None and len(rows) >= limit:
                        return pd.DataFrame(rows)
        return pd.DataFrame(rows, columns=["table_name", "schema", "name", "object_type"])

    @spark_plan("native", source="_spark_query", why="the database runs the SQL; Spark reads the result over JDBC")
    @raw_query
    def query(
        self,
        sql: str,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Runs a raw, engine-specific SQL query and returns the result.

        Parameters
        ----------
        sql : str
            Structural — the query to run (inline only, e.g.
            ``db_query(sql='SELECT * FROM dbo.Orders')``), written in
            your database's own dialect. This is a passthrough, not
            translated between engines.
        limit : int, optional
            Applied client-side (``.head(limit)``) after the query runs —
            for a true server-side limit, add it to ``sql`` itself using
            your engine's syntax.
        """
        logger.info("%s: %s", self.engine.dialect.name, " ".join(sql.split()))
        df = pd.read_sql_query(sql, self.engine)
        if limit is not None:
            df = df.head(limit)
        return df

    # ------------------------------------------------------------------
    # Streaming (iter_*) — for use with DuckAPI.stream()
    # ------------------------------------------------------------------

    @sortable(exact=True)
    def iter_table(
        self,
        table_name: str,
        where: Optional[List[Condition]] = None,
        order_by: Optional[List[Tuple[str, bool]]] = None,
        chunksize: int = 10_000,
    ) -> Iterator[pd.DataFrame]:
        """
        Yields one chunk of up to ``chunksize`` rows at a time, read from a
        server-side cursor (``stream_results``) — the table is never whole in
        memory, here or in the driver. ``order_by``: ORDER BY in the database
        (number/date/time/bool columns, NULLs last; another column → no order).
        """
        tbl = self._reflect(table_name)
        stmt = sa.select(tbl)
        clauses = self._where_clauses(tbl, where)
        if clauses:
            stmt = stmt.where(*clauses)
        if order_by:
            keys = self._order_keys(tbl, order_by)
            if keys is not None:
                stmt = stmt.order_by(*keys)
        self._log_statement(stmt)
        yield from self._stream(stmt, chunksize)

    def _stream(self, stmt: Any, chunksize: int) -> Iterator[pd.DataFrame]:
        with self.engine.connect() as conn:
            result = conn.execution_options(stream_results=True, yield_per=chunksize).execute(stmt)
            columns = list(result.keys())
            empty = True
            for rows in result.partitions(chunksize):
                if rows:
                    empty = False
                    yield pd.DataFrame.from_records([tuple(r) for r in rows], columns=columns)
            if empty:
                yield pd.DataFrame({c: pd.Series(dtype="object") for c in columns})

    def iter_query(self, sql: str, chunksize: int = 10_000) -> Iterator[pd.DataFrame]:
        """Yields one chunk of up to `chunksize` rows at a time from a raw query (a server-side cursor)."""
        yield from (chunk for chunk in self._stream(sa.text(sql), chunksize) if not chunk.empty)

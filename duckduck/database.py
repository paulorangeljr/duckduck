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

from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

from .pushdown import Condition

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
        - Oracle:     ``oracle+cx_oracle://user:pass@host:1521/?service_name=orcl``
        - SQLite:     ``sqlite:///path/to/file.db``
    **engine_kwargs
        Passed straight to ``sqlalchemy.create_engine`` (e.g. ``pool_size``).
    """

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

        - ``connection_string``: a full SQLAlchemy URL, used as-is.
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
            return cls(connection_string, **overrides)

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
                clauses.append(col == cond.value)
            elif cond.op == "gt":
                clauses.append(col > cond.value)
            elif cond.op == "gte":
                clauses.append(col >= cond.value)
            elif cond.op == "lt":
                clauses.append(col < cond.value)
            elif cond.op == "lte":
                clauses.append(col <= cond.value)
        return clauses

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

    def table(
        self,
        table_name: str,
        where: Optional[List[Condition]] = None,
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
        """
        tbl = self._reflect(table_name)
        stmt = sa.select(tbl)
        clauses = self._where_clauses(tbl, where)
        if clauses:
            stmt = stmt.where(*clauses)
        if limit is not None:
            stmt = stmt.limit(limit)
        return pd.read_sql(stmt, self.engine)

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
        df = pd.read_sql_query(sql, self.engine)
        if limit is not None:
            df = df.head(limit)
        return df

    # ------------------------------------------------------------------
    # Streaming (iter_*) — for use with DuckAPI.stream()
    # ------------------------------------------------------------------

    def iter_table(
        self,
        table_name: str,
        where: Optional[List[Condition]] = None,
        chunksize: int = 10_000,
    ) -> Iterator[pd.DataFrame]:
        """Yields one chunk of up to `chunksize` rows at a time from a table."""
        tbl = self._reflect(table_name)
        stmt = sa.select(tbl)
        clauses = self._where_clauses(tbl, where)
        if clauses:
            stmt = stmt.where(*clauses)
        for chunk in pd.read_sql(stmt, self.engine, chunksize=chunksize):
            if not chunk.empty:
                yield chunk

    def iter_query(self, sql: str, chunksize: int = 10_000) -> Iterator[pd.DataFrame]:
        """Yields one chunk of up to `chunksize` rows at a time from a raw query."""
        for chunk in pd.read_sql_query(sql, self.engine, chunksize=chunksize):
            if not chunk.empty:
                yield chunk

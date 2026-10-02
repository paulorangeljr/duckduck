"""
Amazon Athena connector for use with DuckAPI: the query runs on Athena.

Where ``glue`` reads a table's files with DuckDB on this machine, ``athena``
sends the SQL to Athena, which scans S3 on its own fleet — partition pruning,
projection, Parquet/ORC statistics, Iceberg — and only the answer comes back.
Worth it when the files are many or big and the WHERE keeps a small part.

Tables (``{name}_…`` once registered)
-------------------------------------
- ``table(database, table_name, where, limit)`` — the push-down WHERE and
  LIMIT become Athena SQL (``SELECT * FROM "db"."t" WHERE … LIMIT n``):
  ``=``, LIKE, ILIKE (``lower(c) LIKE lower(p)``), comparisons and a join's
  ``IN``, values typed by the table's columns (``DATE '…'``,
  ``TIMESTAMP '…'``, numbers bare); a condition that can't be written
  exactly stays with DuckDB and then the LIMIT isn't sent.
- ``query(sql)`` — any Athena SQL, as written (``@raw_query``).
- ``databases`` / ``tables`` / ``columns`` — the catalog (``tables`` lists
  ``table``'s arguments: ``athena.<database>.<table>`` addresses).

How the answer comes back (``results``)
---------------------------------------
- ``"unload"``: the SELECT runs as ``UNLOAD (…) TO '<output>/duckduck-unload/<id>/'
  WITH (format = 'PARQUET')`` and DuckDB reads those Parquet files straight
  from S3 — typed, compressed, several files at once. Deleted afterwards
  (``cleanup``).
- ``"api"``: ``GetQueryResults``, 1,000 rows per request — fine for small
  answers, slow for big ones.
- ``"auto"`` (default): the API up to ``api_rows`` (a LIMIT that small, the
  catalog calls, non-SELECT statements), UNLOAD otherwise — when there's an
  output location (``output_location``, else the workgroup's).

A query's result can be reused by Athena itself (``reuse_minutes``: the
same SQL within that many minutes isn't run again nor billed again).
Cancelling a DuckAPI read (the page's ✕, a SQL client's cancel) stops the
Athena query (``StopQueryExecution``).

Built from the boto3 Athena API as documented (``start_query_execution``,
``get_query_execution``, ``get_query_results``, ``list_databases``,
``list_table_metadata``, ``get_table_metadata``, ``get_work_group``); the
tests replay those shapes — no AWS account was reachable while writing it.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import date, datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd

from . import progress, s3layout
from .kinds import catalog, raw_query
from .lakehouse import LakehouseConnection
from .logs import get_logger
from .pushdown import Condition, parse_like
from .sparkplan import SparkSource, spark_plan

logger = get_logger("athena")

try:
    import boto3
except ImportError:
    boto3 = None

#: Athena column types → how a value is written in its SQL
_NUMBERS = ("tinyint", "smallint", "int", "integer", "bigint", "float", "double", "real", "decimal")
_TEXT = ("varchar", "char", "string")
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class AthenaError(RuntimeError):
    """An Athena query that failed or was cancelled — with Athena's own reason."""


class Athena:
    """
    Runs SQL on Amazon Athena.

    Parameters
    ----------
    region_name, profile_name, aws_access_key_id, aws_secret_access_key
        As for ``glue``: explicit keys, or the default credential chain
        (optionally a named profile) — used both for Athena and to read the
        UNLOAD results from S3.
    workgroup : str
        The Athena workgroup (``"primary"``).
    catalog : str
        The data catalog (``"AwsDataCatalog"`` — Glue).
    output_location : str, optional
        ``s3://…`` for query results; else the workgroup's.
    results : ``"auto"`` | ``"unload"`` | ``"api"``
    api_rows : int
        Up to this many rows (a LIMIT at most this) come through the API.
    reuse_minutes : int
        Athena's query result reuse (0: off).
    cleanup : bool
        Delete the UNLOAD files once read.
    poll_interval, timeout : float
        Seconds between status checks (doubling up to 2 s); give up after
        ``timeout`` (None: wait).
    """

    #: ``where`` applies join key values too (``col IN (…)``); more than ``IN_MAX`` → several queries
    WHERE_OPS = frozenset({"eq", "like", "ilike", "gt", "gte", "lt", "lte", "in"})
    IN_MAX = 1000

    def __init__(
        self,
        region_name: Optional[str] = None,
        profile_name: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        workgroup: str = "primary",
        catalog: str = "AwsDataCatalog",
        output_location: Optional[str] = None,
        results: str = "auto",
        api_rows: int = 1000,
        reuse_minutes: int = 0,
        cleanup: bool = True,
        poll_interval: float = 0.25,
        timeout: Optional[float] = None,
        list_threads: int = 8,
        client: Any = None,
        s3_client: Any = None,
    ):
        if results not in ("auto", "unload", "api"):
            raise ValueError("results must be 'auto', 'unload' or 'api'")
        if client is None and boto3 is None:
            raise ImportError("boto3 is required for the Athena connector.\n"
                              "Install with:  pip install \"duckduck[aws]\"")
        self.workgroup, self.catalog = workgroup, catalog
        self.results, self.api_rows = results, int(api_rows)
        self.reuse_minutes, self.cleanup = int(reuse_minutes or 0), bool(cleanup)
        self.poll_interval, self.timeout = float(poll_interval), timeout
        self.list_threads = max(1, int(list_threads))  # databases listed at once by ``tables``
        self._output = output_location
        self._region = region_name
        self._session = None
        if client is None:
            self._session = boto3.Session(profile_name=profile_name, region_name=region_name,
                                          aws_access_key_id=aws_access_key_id,
                                          aws_secret_access_key=aws_secret_access_key)
            client = self._session.client("athena")
        self._athena = client
        self._s3_client = s3_client
        self._metadata: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._lake: Optional[LakehouseConnection] = None
        self._secret = (aws_access_key_id, aws_secret_access_key, profile_name, region_name)
        self.base_url = f"athena://{region_name or 'default'}/{workgroup}"  # the endpoint list_tables() shows

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "Athena":
        """Every key optional: without keys, the default AWS credential chain."""
        keys = ("region_name", "profile_name", "aws_access_key_id", "aws_secret_access_key")
        return cls(**{k: overrides.pop(k, None) or secret.get(k) for k in keys}, **overrides)

    def __getstate__(self) -> Dict[str, Any]:  # travels to Spark executors: no clients, no connection
        state = dict(self.__dict__)
        state.update(_athena=None, _s3_client=None, _lake=None, _session=None)
        return state

    # ------------------------------------------------------------------
    # Running a query
    # ------------------------------------------------------------------

    def output_location(self) -> Optional[str]:
        """Where results go: ``output_location``, else the workgroup's (asked once)."""
        if self._output is None:
            try:
                wg = self._athena.get_work_group(WorkGroup=self.workgroup)["WorkGroup"]
                self._output = ((wg.get("Configuration") or {}).get("ResultConfiguration") or {}).get(
                    "OutputLocation") or ""
            except Exception as exc:  # noqa: BLE001 — no permission to read it: results through the API
                logger.info("athena: workgroup %s has no readable output location (%s)", self.workgroup, exc)
                self._output = ""
        return self._output or None

    def _run(self, sql: str, database: Optional[str] = None) -> Dict[str, Any]:
        """Starts ``sql``, waits for it (cancel-aware) and returns its QueryExecution."""
        request: Dict[str, Any] = {"QueryString": sql, "WorkGroup": self.workgroup,
                                   "QueryExecutionContext": {"Catalog": self.catalog,
                                                             **({"Database": database} if database else {})}}
        if self._output:
            request["ResultConfiguration"] = {"OutputLocation": self._output}
        if self.reuse_minutes > 0 and not sql.lstrip().upper().startswith("UNLOAD"):
            request["ResultReuseConfiguration"] = {
                "ResultReuseByAgeConfiguration": {"Enabled": True, "MaxAgeInMinutes": self.reuse_minutes}}
        logger.info("athena: %s", " ".join(sql.split()))
        progress.step("fetching", "Athena query running…")  # a cancel before it starts: nothing to stop
        started = time.perf_counter()
        qid = self._athena.start_query_execution(**request)["QueryExecutionId"]
        wait = self.poll_interval
        try:
            while True:
                execution = self._athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
                state = (execution.get("Status") or {}).get("State")
                if state == "SUCCEEDED":
                    break
                if state in ("FAILED", "CANCELLED"):
                    reason = (execution.get("Status") or {}).get("StateChangeReason") or state.lower()
                    raise AthenaError(f"Athena query {qid} {state.lower()}: {reason}")
                if self.timeout is not None and time.perf_counter() - started > self.timeout:
                    self._stop(qid)
                    raise AthenaError(f"Athena query {qid} still {state} after {self.timeout:.0f}s: stopped")
                progress.wait(wait)  # a cancel lands here
                wait = min(wait * 2, 2.0)
        except progress.Cancelled:
            self._stop(qid)
            raise
        stats = execution.get("Statistics") or {}
        scanned = stats.get("DataScannedInBytes") or 0
        reused = (stats.get("ResultReuseInformation") or {}).get("ReusedPreviousResult")
        logger.info("athena: %s done in %.1fs · %.1f MB scanned%s", qid[:8], time.perf_counter() - started,
                    scanned / 1e6, " · result reused" if reused else "")
        return execution

    def _stop(self, qid: str) -> None:
        try:
            self._athena.stop_query_execution(QueryExecutionId=qid)
            logger.info("athena: stopped %s", qid[:8])
        except Exception as exc:  # noqa: BLE001
            logger.warning("athena: couldn't stop %s: %s", qid, exc)

    def _read(self, select: str, database: Optional[str], limit: Optional[int],
              empty_columns: Optional[List[str]] = None) -> pd.DataFrame:
        """Runs a SELECT and brings its answer back the cheapest way (``results``)."""
        if self._unload_it(select, limit):
            return self._unloaded(select, database, empty_columns)
        return self._api_result(self._run(select, database))

    def _unload_it(self, sql: str, limit: Optional[int]) -> bool:
        if self.results == "api" or not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I):
            return False
        if self.results == "auto" and limit is not None and limit <= self.api_rows:
            return False
        return self.output_location() is not None

    # ------------------------------------------------------------------
    # Results: UNLOAD to Parquet, read by DuckDB
    # ------------------------------------------------------------------

    def _unload_prefix(self) -> str:
        return self.output_location().rstrip("/") + f"/duckduck-unload/{uuid.uuid4().hex}/"

    def _unload(self, select: str, database: Optional[str]) -> Tuple[str, List[str]]:
        prefix = self._unload_prefix()
        self._run(f"UNLOAD ({select}) TO '{prefix}' WITH (format = 'PARQUET', compression = 'SNAPPY')", database)
        bucket, key = s3layout.split_s3(prefix)
        files = []
        for page in self._s3().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key):
            for obj in page.get("Contents", []) or []:
                if s3layout.is_data_file(obj["Key"][len(key):], obj.get("Size")):
                    files.append(f"s3://{bucket}/{obj['Key']}")
        return prefix, files

    def _unloaded(self, select: str, database: Optional[str], empty_columns: Optional[List[str]]) -> pd.DataFrame:
        prefix, files = self._unload(select, database)
        try:
            if not files:
                return pd.DataFrame({c: pd.Series(dtype="object") for c in empty_columns or []})
            return self._lake_conn().scan("read_parquet(getvariable('duckduck_files'), union_by_name=true)",
                                          variables={"duckduck_files": files})
        finally:
            self._clean(prefix)

    def _clean(self, prefix: str) -> None:
        if not self.cleanup:
            return
        try:
            bucket, key = s3layout.split_s3(prefix)
            s3 = self._s3()
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", []) or []]
                if keys:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
        except Exception as exc:  # noqa: BLE001 — the answer is read; a leftover prefix only costs storage
            logger.info("athena: couldn't delete %s (%s) — set cleanup=false or allow s3:DeleteObject", prefix, exc)

    def _s3(self) -> Any:
        if self._s3_client is None:
            self._s3_client = self._session.client("s3")
        return self._s3_client

    def _lake_conn(self) -> LakehouseConnection:
        if self._lake is None:
            lake = LakehouseConnection()
            key_id, secret, profile, region = self._secret
            region_clause = f", REGION '{region}'" if region else ""
            if key_id and secret:
                lake.create_secret(f"CREATE OR REPLACE SECRET duckduck_athena_s3 (TYPE s3, KEY_ID '{key_id}', "
                                   f"SECRET '{secret}'{region_clause})")
            else:
                profile_clause = f", PROFILE '{profile}'" if profile else ""
                lake.create_secret("CREATE OR REPLACE SECRET duckduck_athena_s3 (TYPE s3, "
                                   f"PROVIDER credential_chain{profile_clause}{region_clause})")
            self._lake = lake
        return self._lake

    # ------------------------------------------------------------------
    # Results: GetQueryResults, typed by the result's own metadata
    # ------------------------------------------------------------------

    def _api_pages(self, execution: Dict[str, Any]) -> Iterator[pd.DataFrame]:
        qid = execution["QueryExecutionId"]
        header_skipped = (execution.get("StatementType") or "DML") != "DML"
        columns: Optional[List[Tuple[str, str]]] = None
        for page in self._athena.get_paginator("get_query_results").paginate(
                QueryExecutionId=qid, PaginationConfig={"PageSize": 1000}):
            result = page.get("ResultSet") or {}
            if columns is None:
                info = (result.get("ResultSetMetadata") or {}).get("ColumnInfo") or []
                columns = [(c.get("Name") or c.get("Label"), (c.get("Type") or "varchar").lower()) for c in info]
            rows = result.get("Rows") or []
            if not header_skipped and rows:
                rows, header_skipped = rows[1:], True  # a SELECT's first row repeats the column names
            progress.checkpoint()
            yield pd.DataFrame([[_typed(cell.get("VarCharValue"), t) for cell, (_, t) in
                                 zip(r.get("Data") or [], columns)] for r in rows],
                               columns=[n for n, _ in columns])

    def _api_result(self, execution: Dict[str, Any]) -> pd.DataFrame:
        frames = list(self._api_pages(execution))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    # ------------------------------------------------------------------
    # Push-down: conditions → Athena SQL
    # ------------------------------------------------------------------

    def _table_metadata(self, database: str, table_name: str) -> Dict[str, Any]:
        key = (database.lower(), table_name.lower())
        if key not in self._metadata:
            self._metadata[key] = self._athena.get_table_metadata(
                CatalogName=self.catalog, DatabaseName=database, TableName=table_name)["TableMetadata"]
        return self._metadata[key]

    def _column_types(self, database: str, table_name: str) -> Dict[str, Tuple[str, str]]:
        meta = self._table_metadata(database, table_name)
        return {c["Name"].lower(): (c["Name"], (c.get("Type") or "string").lower())
                for c in (meta.get("Columns") or []) + (meta.get("PartitionKeys") or [])}

    def _select(self, database: str, table_name: str, where: Optional[List[Condition]],
                limit: Optional[int]) -> Tuple[str, bool]:
        """(the SELECT, whether every condition went into it)."""
        for part in (database, table_name):
            if not _NAME_RE.match(part or ""):
                raise ValueError(f"not an Athena database/table name: {part!r}")
        types = self._column_types(database, table_name) if where else {}
        clauses, complete = [], True
        for cond in where or []:
            clause, why = _clause(cond, types)
            if clause is None:
                complete = False
                logger.info("athena: %s %s … stays with DuckDB (%s)", cond.column, cond.op, why)
            else:
                clauses.append(clause)
        sql = f'SELECT * FROM "{database}"."{table_name}"'
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if limit is not None and complete:
            sql += f" LIMIT {int(limit)}"
        return sql, complete

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    @spark_plan("native", source="_spark_table", why="a Glue-catalogued table on S3: Spark reads it by name or path")
    def table(
        self,
        database: str,
        table_name: str,
        where: Optional[List[Condition]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        An Athena table, queried on Athena: the WHERE and LIMIT go into its
        SQL. ``database`` and ``table_name`` are required
        (``athena.<database>.<table>``).
        """
        sql, complete = self._select(database, table_name, where, limit)
        empty = [c[0] for c in self._column_types(database, table_name).values()]
        return self._read(sql, database, limit if complete else None, empty_columns=empty)

    @spark_plan("driver", why="Athena runs the query; its answer comes back in one piece")
    @raw_query
    def query(self, sql: str, database: Optional[str] = None, limit: Optional[int] = None) -> pd.DataFrame:
        """Any Athena SQL, as written (``WHERE arg.sql = '…'``); ``limit`` wraps a SELECT."""
        if limit is not None and re.match(r"^\s*(SELECT|WITH)\b", sql, re.I):
            sql = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) LIMIT {int(limit)}"
        if self._unload_it(sql, limit):
            return self._unloaded(sql, database, None)
        return self._api_result(self._run(sql, database))

    @spark_plan("driver", why="catalog: a small listing")
    @catalog
    def databases(self, limit: Optional[int] = None) -> pd.DataFrame:
        """The catalog's databases."""
        rows = [{"database": d.get("Name"), "description": d.get("Description")}
                for page in self._athena.get_paginator("list_databases").paginate(CatalogName=self.catalog)
                for d in page.get("DatabaseList", []) or []]
        return pd.DataFrame(rows[:limit] if limit else rows, columns=["database", "description"])

    @spark_plan("driver", why="catalog: a small listing")
    @catalog(lists="table")
    def tables(self, database: Optional[str] = None, table_name_ilike: Optional[str] = None,
               limit: Optional[int] = None) -> pd.DataFrame:
        """Every table of a database (every database's when not given): each row a ``table(database, table_name)``.

        Every database is one ``list_table_metadata`` listing (50 tables a page, columns included), so a whole
        catalog is many calls: ``list_threads`` databases are listed at once, each page is a pause / cancel point,
        the progress says how many databases are done, and a ``limit`` stops as soon as it has its rows. Narrow it
        with ``WHERE database = '…'`` (or ``table_name LIKE …``) when only some are needed."""
        names = [database] if database else list(self.databases()["database"])
        pattern = parse_like(table_name_ilike) if table_name_ilike else None
        columns = ["database", "table_name", "table_type", "format", "location", "partition_keys", "column_count",
                   "created", "last_access"]

        def list_db(db: str) -> List[Dict[str, Any]]:
            kwargs: Dict[str, Any] = {"CatalogName": self.catalog, "DatabaseName": db}
            if pattern is not None:
                kwargs["Expression"] = _athena_regex(pattern)
            out: List[Dict[str, Any]] = []
            for page in self._athena.get_paginator("list_table_metadata").paginate(**kwargs):
                progress.checkpoint()
                for t in page.get("TableMetadataList", []) or []:
                    params = t.get("Parameters") or {}
                    out.append({
                        "database": db, "table_name": t.get("Name"), "table_type": t.get("TableType"),
                        "format": (params.get("table_type") or params.get("classification") or "").lower() or None,
                        "location": params.get("location"),
                        "partition_keys": ", ".join(k.get("Name", "") for k in t.get("PartitionKeys") or []),
                        "column_count": len(t.get("Columns") or []),
                        "created": t.get("CreateTime"), "last_access": t.get("LastAccessTime"),
                    })
                if limit and len(out) >= limit:
                    break
            return out

        rows: List[Dict[str, Any]] = []
        started = time.perf_counter()

        def done(i: int, found: List[Dict[str, Any]]) -> bool:
            rows.extend(found)
            text = f"athena tables: {i}/{len(names)} database(s) · {len(rows):,} table(s)"
            progress.update(text)
            logger.info("%s · %.1fs", text, time.perf_counter() - started)
            return bool(limit) and len(rows) >= limit

        if len(names) <= 1 or self.list_threads == 1:
            for i, db in enumerate(names, 1):
                if done(i, list_db(db)):
                    break
        else:
            import contextvars
            from concurrent.futures import ThreadPoolExecutor

            pool = ThreadPoolExecutor(max_workers=min(self.list_threads, len(names)))
            try:  # each listing in this context: the job's pause / cancel reach it
                futures = [pool.submit(contextvars.copy_context().run, list_db, db) for db in names]
                for i, future in enumerate(futures, 1):  # in the databases' order: the same rows every time
                    if done(i, future.result()):
                        break
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        return pd.DataFrame(rows[:limit] if limit else rows, columns=columns)

    @spark_plan("driver", why="catalog: a small listing")
    @catalog
    def columns(self, database: str, table_name: str, limit: Optional[int] = None) -> pd.DataFrame:
        """A table's columns (partition keys included), with Athena's types."""
        meta = self._table_metadata(database, table_name)
        rows = [{"database": database, "table_name": table_name, "column_name": c.get("Name"),
                 "data_type": c.get("Type"), "comment": c.get("Comment"), "partition_key": part}
                for part, cols in ((False, meta.get("Columns")), (True, meta.get("PartitionKeys")))
                for c in cols or []]
        return pd.DataFrame(rows[:limit] if limit else rows,
                            columns=["database", "table_name", "column_name", "data_type", "comment",
                                     "partition_key"])

    # ------------------------------------------------------------------
    # Streaming: one DataFrame per UNLOAD file (or per API page)
    # ------------------------------------------------------------------

    def iter_table(self, database: str, table_name: str,
                   where: Optional[List[Condition]] = None) -> Iterator[pd.DataFrame]:
        """Yields the answer a piece at a time: each UNLOAD file, or each page of the API."""
        sql, _ = self._select(database, table_name, where, None)
        return self._iter(sql, database)

    def iter_query(self, sql: str, database: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """``query``'s answer, a piece at a time."""
        return self._iter(sql, database)

    def _iter(self, sql: str, database: Optional[str]) -> Iterator[pd.DataFrame]:
        if not self._unload_it(sql, None):
            yield from self._api_pages(self._run(sql, database))
            return
        prefix, files = self._unload(sql, database)
        try:
            for f in files:
                progress.checkpoint()
                yield self._lake_conn().scan("read_parquet(getvariable('duckduck_file'))",
                                             variables={"duckduck_file": [f]})
        finally:
            self._clean(prefix)

    # ------------------------------------------------------------------
    # Spark (``duckduck.spark``): where the table's data is
    # ------------------------------------------------------------------

    def _spark_table(self, database: str, table_name: str) -> SparkSource:
        meta = self._table_metadata(database, table_name)
        params = meta.get("Parameters") or {}
        kind = (params.get("table_type") or "").lower()
        fmt = "iceberg" if kind == "iceberg" else "delta" if kind == "delta" else "parquet"
        location = params.get("metadata_location") if fmt == "iceberg" else params.get("location")
        return SparkSource(fmt, path=location, table=f"{database}.{table_name}")


# ---------------------------------------------------------------------------
# Writing a condition in Athena (Trino) SQL
# ---------------------------------------------------------------------------


def _clause(cond: Condition, types: Dict[str, Tuple[str, str]]) -> Tuple[Optional[str], str]:
    """``cond`` as Athena SQL, or (None, why it stays with DuckDB)."""
    found = types.get(cond.column.lower())
    if found is None:
        return None, "not a column of the table"
    name, athena_type = found
    col = '"' + name.replace('"', '""') + '"'
    if cond.op in ("like", "ilike"):
        if not athena_type.startswith(_TEXT):
            return None, f"LIKE on a {athena_type} column"
        pattern = _string(str(cond.value))  # same wildcards and no escape character as DuckDB: exact
        return (f"{col} LIKE {pattern}" if cond.op == "like" else f"lower({col}) LIKE lower({pattern})"), ""
    values = list(cond.value or ()) if cond.op == "in" else [cond.value]
    literals = [_literal(v, athena_type) for v in values]
    if not literals or any(lit is None for lit in literals):
        return None, f"a value that isn't a {athena_type}"
    if cond.op == "in":
        return f"{col} IN ({', '.join(literals)})", ""
    op = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}.get(cond.op)
    if op is None:
        return None, f"operator {cond.op}"
    return f"{col} {op} {literals[0]}", ""


def _string(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _literal(value: Any, athena_type: str) -> Optional[str]:
    """A value as an Athena literal of the column's type — None when it isn't one (DuckDB then compares)."""
    if value is None:
        return None
    if athena_type.startswith(_NUMBERS):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return str(int(number)) if number.is_integer() else repr(number)  # Trino widens either side to compare
    if athena_type == "boolean":
        text = str(value).lower()
        return text if text in ("true", "false") else None
    if athena_type == "date":
        moment = _moment(value)
        return f"DATE '{moment.date().isoformat()}'" if moment else None
    if athena_type.startswith("timestamp"):
        moment = _moment(value)
        return f"TIMESTAMP '{moment.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}'" if moment else None
    if athena_type.startswith(_TEXT):
        return _string(str(value))
    return None  # arrays, maps, structs: DuckDB compares


def _moment(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _athena_regex(pattern: Any) -> str:
    """``list_table_metadata``'s Expression (a regex) for a LIKE pattern kind — case-insensitive names anyway."""
    text = re.escape(pattern.text.lower())
    return {"equals": text, "startswith": text + ".*", "endswith": ".*" + text}.get(pattern.kind, f".*{text}.*")


def _typed(text: Optional[str], athena_type: str) -> Any:
    """One ``GetQueryResults`` cell (always text) as the column's type."""
    if text is None:
        return None
    try:
        if athena_type in ("tinyint", "smallint", "integer", "int", "bigint"):
            return int(text)
        if athena_type in ("double", "float", "real") or athena_type.startswith("decimal"):
            return float(text)
        if athena_type == "boolean":
            return text.lower() == "true"
        if athena_type == "date":
            return date.fromisoformat(text)
        if athena_type.startswith("timestamp"):
            return pd.Timestamp(text.replace(" UTC", ""))
    except (ValueError, TypeError):
        return text
    return text

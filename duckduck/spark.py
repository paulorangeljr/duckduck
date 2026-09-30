"""
duckduck's tables as Spark DataFrames — each read the way its connector
says it should be read (``@spark_plan`` on the connector's table method).

    from duckduck.spark import SparkReader

    reader = SparkReader(spark, duck)
    df = reader.sql("SELECT * FROM nvd.cves c JOIN s3_data.security.proxy_logs p ON ...")
    reader.decisions        # per table: the strategy used and why

The SQL is duckduck's: addresses, ``arg.x``, push-down — the same planning
as ``duck.sql()`` (``DuckAPI`` decides what each table is called with). Then
every table becomes a Spark temporary view, read by its strategy, and the
query itself runs in Spark (translated from DuckDB's SQL by sqlglot):

- **native** — the data already sits where Spark reads it (Parquet / Delta /
  Iceberg on S3 or ADLS, a database over JDBC): the connector's ``source``
  hook says where (``SparkSource``), Spark reads it, the WHERE's simple
  conditions become Spark filters. Nothing goes through Python.
- **partitioned** — an API whose read splits into independent pieces: page
  windows (``by="pages"``: the connector pages through
  ``duckduck.slicing.pages``, which reports the total and reads a window of
  pages on request) or a hook returning the call's kwargs per piece. The
  pieces run on the executors (``mapInArrow``), at most ``max_parallel`` at
  a time — the API's rate limit, not the cluster's size, sets it.
- **driver** — anything else (cursor pagination, small tables, strict rate
  limits): read on the driver by duckduck and handed to Spark; with
  ``staging_path`` and a streaming function, page by page into Parquet
  there, then read by Spark — memory stays one page.

A native read that fails (a format Spark lacks, no storage credentials in
Spark) falls back to the driver read with a warning (``fallback=False``
raises instead). Needs ``pyspark`` and ``pyarrow`` (``pip install
"duckduck[spark]"``).
"""

from __future__ import annotations

import functools
import logging
import math
import os
import pickle
import re
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pandas as pd

from . import slicing
from .core import DuckAPI
from .sparkplan import SPARK_ATTR, STRATEGIES, SparkPlan, SparkSource, plan_of, spark_plan  # noqa: F401

logger = logging.getLogger("duckduck.spark")


def _owner(fn: Any) -> Any:
    """The object a table function belongs to (its connector), for the plan's hooks: a bound method's instance,
    or a callable object itself (``FileTable``)."""
    import inspect

    if inspect.ismethod(fn):
        return fn.__self__
    return None if inspect.isfunction(fn) else fn


# ---------------------------------------------------------------------------
# frames → Arrow (through DuckDB: it types pandas' object columns sensibly)
# ---------------------------------------------------------------------------

def _frame(data: Any, name: str) -> pd.DataFrame:
    return DuckAPI._to_dataframe(None, data, name, allow_empty=True)  # self isn't used there


def _arrow(df: pd.DataFrame, fallback_columns: Optional[List[str]] = None):
    import duckdb

    if len(df.columns) == 0:
        df = pd.DataFrame({c: pd.Series(dtype="string") for c in (fallback_columns or [DuckAPI.EMPTY_PLACEHOLDER_COLUMN])})
    else:
        df = df.copy()
        for c in df.columns:  # all empty: text, not the INTEGER DuckDB would guess (a later page may hold text)
            if df[c].dtype == object and df[c].isna().all():
                df[c] = df[c].astype("string")
    con = duckdb.connect()
    try:
        con.execute("SET enable_external_access = false")
        con.register("frame", df)
        return con.sql("SELECT * FROM frame").to_arrow_table()
    finally:
        con.close()


_DUCK_OF_ARROW = [("large_string", "VARCHAR"), ("string", "VARCHAR"), ("int64", "BIGINT"), ("int32", "INTEGER"),
                  ("int16", "SMALLINT"), ("int8", "TINYINT"), ("double", "DOUBLE"), ("float", "FLOAT"),
                  ("bool", "BOOLEAN"), ("date32", "DATE"), ("binary", "BLOB"), ("large_binary", "BLOB")]


def _duck_type(arrow_type: Any) -> str:
    import pyarrow as pa

    if pa.types.is_timestamp(arrow_type):
        return "TIMESTAMPTZ" if arrow_type.tz else "TIMESTAMP"
    if pa.types.is_decimal(arrow_type):
        return f"DECIMAL({arrow_type.precision},{arrow_type.scale})"
    return dict(_DUCK_OF_ARROW).get(str(arrow_type), "VARCHAR")


def _flat_schema(table: Any):
    """A piece's schema for the partitioned read: nested columns as JSON text (pieces may not agree on their shape)."""
    import pyarrow as pa

    fields = [pa.field(f.name, pa.string()) if pa.types.is_nested(f.type) or pa.types.is_null(f.type) else f
              for f in table.schema]
    return pa.schema(fields)


def _conform(df: pd.DataFrame, schema: Any):
    """One piece's rows in exactly ``schema``: missing columns NULL, others TRY_CAST, nested ones as JSON."""
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("SET enable_external_access = false")
        present = {}
        if len(df.columns):
            con.register("piece", df)
            present = {r[0]: r[1] for r in con.execute("DESCRIBE piece").fetchall()}
        quote = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
        select = []
        for f in schema:
            target = _duck_type(f.type)
            if f.name not in present:
                select.append(f"CAST(NULL AS {target}) AS {quote(f.name)}")
            elif target == "VARCHAR" and re.match(r"(STRUCT|MAP|UNION)|.*\[\d*\]$", str(present[f.name])):
                select.append(f"CAST(to_json({quote(f.name)}) AS VARCHAR) AS {quote(f.name)}")
            else:
                select.append(f"TRY_CAST({quote(f.name)} AS {target}) AS {quote(f.name)}")
        source = "piece" if present else "(SELECT 1 WHERE false)"
        table = con.sql(f"SELECT {', '.join(select)} FROM {source}").to_arrow_table()
    finally:
        con.close()
    return table.cast(schema)


def _conform_flat(df: pd.DataFrame):
    """The first piece: its own columns, nested ones as JSON — the schema every other piece is cast to."""
    table = _arrow(df)
    return _conform(df, _flat_schema(table)) if len(df.columns) else table


def _run_pieces(fn: Callable, kwargs: Dict[str, Any], mode: str, arrow_schema: Any, name: str) -> Callable:
    """The executor side of a partitioned read: each spec is a page window or a call's kwargs."""

    def run(batches: Iterator[Any]) -> Iterator[Any]:
        from duckduck import slicing as _slicing

        for batch in batches:
            for blob in batch.column(0).to_pylist():
                spec = pickle.loads(blob)
                if mode == "pages":
                    with _slicing.window(*spec) as w:
                        data = fn(**kwargs)
                    if not w.seen:
                        raise RuntimeError(f"{name}: the read didn't go through duckduck.slicing.pages — "
                                           f"its plan can't be by='pages'")
                else:
                    data = fn(**spec)
                yield from _conform(_frame(data, name), arrow_schema).to_batches()

    return run


# ---------------------------------------------------------------------------
# the reader
# ---------------------------------------------------------------------------

class SparkReader:
    """
    Reads duckduck's tables into Spark, each by its connector's plan.

    ``spark``: the SparkSession. ``duck``: the ``DuckAPI`` whose tables to
    read. ``fallback``: a native read that fails is read on the driver
    instead (with a warning). ``staging_path``: where driver reads land as
    Parquet, page by page, when the table has a streaming function — a path
    both the driver (pyarrow) and Spark can reach (a shared filesystem,
    ``s3://…``). ``max_parallel``: caps every partitioned read (the plan's
    own value otherwise). ``path_schemes``: rewrite storage URL schemes for
    this Spark (``{"s3": "s3a"}`` on open-source Spark).
    """

    def __init__(self, spark: Any, duck: Any, fallback: bool = True, staging_path: Optional[str] = None,
                 max_parallel: Optional[int] = None, path_schemes: Optional[Dict[str, str]] = None):
        self.spark = spark
        self.duck = duck
        self.fallback = fallback
        self.staging_path = staging_path
        self.max_parallel = max_parallel
        self.path_schemes = dict(path_schemes or {})
        #: the last ``sql()``'s reads: {table, strategy, planned, why, detail}
        self.decisions: List[Dict[str, Any]] = []

    # -- the API ------------------------------------------------------------------------------------------

    def sql(self, query: str):
        """Runs duckduck SQL with every table read into Spark by its plan; returns a Spark DataFrame."""
        self.decisions = []
        planner = _SparkDuck(self)
        return planner.sql(query)

    def table(self, name: str, **args: Any):
        """One table (``nvd.cves``, a registered name, ``s3_data.security.proxy_logs``); ``args`` fill its arguments."""
        where = " AND ".join(f"arg.{k} = {_sql_value(v)}" for k, v in args.items())
        return self.sql(f"SELECT * FROM {name}" + (f" WHERE {where}" if where else ""))

    def explain(self, name: str) -> Dict[str, Any]:
        """How a registered table would be read (without reading it): strategy, why, the hooks it uses."""
        fn = self.duck.functions[name]
        plan, _, _ = self._resolve(name, fn, {})
        return {"table": name, "strategy": plan.strategy if plan else "driver",
                "declared": plan is not None, "why": (plan.why if plan else "no spark_plan declared"),
                "source": plan.source if plan else None, "by": plan.by if plan else None,
                "max_parallel": plan.max_parallel if plan else None}

    # -- one table ----------------------------------------------------------------------------------------

    def _resolve(self, name: str, fn: Any, kwargs: Dict[str, Any]) -> Tuple[Optional[SparkPlan], Any, Dict[str, Any]]:
        """A saved table is its base table with its arguments; a saved query is its SQL."""
        from .views import VIEW_ATTR

        definition = getattr(fn, VIEW_ATTR, None)
        if isinstance(definition, dict):
            if definition.get("sql"):
                return SparkPlan("driver", why="saved query"), definition, kwargs
            base = self.duck.functions.get(str(definition.get("table", "")).lower())
            if base is not None:
                merged = {**dict(definition.get("args") or {}), **kwargs}
                plan, base_fn, _ = self._resolve(definition["table"], base, merged)
                return plan, base_fn, merged
        return plan_of(fn), fn, kwargs

    def read(self, name: str, fn: Any, kwargs: Dict[str, Any], fallback_columns: Optional[List[str]] = None):
        plan, target, kwargs = self._resolve(name, fn, kwargs)
        if isinstance(target, dict):  # a saved query: its own SQL, read the same way
            df = self.sql(target["sql"])
            limit = kwargs.get("limit")
            self._decide(name, "sql", plan, "a saved query, planned table by table")
            return df.limit(int(limit)) if limit is not None else df
        validated = self.duck._validate_arguments(name, target, kwargs)
        strategy = plan.strategy if plan else "driver"
        if strategy == "native":
            try:
                df = self._native(name, target, validated, plan)
                self._decide(name, "native", plan, "read by Spark where the data is")
                return df
            except Exception as exc:  # noqa: BLE001 — a missing format / credentials in Spark
                if not self.fallback:
                    raise
                logger.warning("%s: native Spark read failed (%s) — reading it on the driver instead", name, exc)
                return self._driver(name, target, validated, fallback_columns, plan,
                                    f"native read failed: {type(exc).__name__}: {exc}")
        if strategy == "partitioned":
            if validated.get("limit") is not None:
                return self._driver(name, target, validated, fallback_columns, plan,
                                    "LIMIT reached the source: one request")
            return self._partitioned(name, target, validated, fallback_columns, plan)
        return self._driver(name, target, validated, fallback_columns, plan, None)

    def _decide(self, name: str, used: str, plan: Optional[SparkPlan], detail: str) -> None:
        entry = {"table": name, "strategy": used, "planned": plan.strategy if plan else None,
                 "why": plan.why if plan else "no spark_plan declared", "detail": detail}
        self.decisions.append(entry)
        logger.info("  %s: %s — %s%s", name, used, detail, f" ({entry['why']})" if entry["why"] else "")

    # -- native ---------------------------------------------------------------------------------------------

    def _native(self, name: str, fn: Any, kwargs: Dict[str, Any], plan: SparkPlan):
        import inspect

        hook = getattr(_owner(fn), plan.source)
        accepted = inspect.signature(hook).parameters
        src = hook(**{k: v for k, v in kwargs.items() if k in accepted and k not in ("where", "limit")})
        df = self._load(src)
        df = _filtered(df, kwargs.get("where"))
        if kwargs.get("limit") is not None:
            df = df.limit(int(kwargs["limit"]))
        return df

    def _load(self, src: SparkSource):
        if src.table:
            try:
                if self.spark.catalog.tableExists(src.table):
                    return self.spark.table(src.table)
            except Exception:  # noqa: BLE001 — no catalog / a name it can't parse: read the location
                pass
        reader = self.spark.read.format(src.format).options(**{k: str(v) for k, v in src.options.items()})
        if src.format == "jdbc" or not src.path:
            return reader.load()
        return reader.load(self._path(src.path))

    def _path(self, path: str) -> str:
        scheme, sep, rest = path.partition("://")
        return f"{self.path_schemes[scheme]}://{rest}" if sep and scheme in self.path_schemes else path

    # -- partitioned ------------------------------------------------------------------------------------------

    def _partitioned(self, name: str, fn: Any, kwargs: Dict[str, Any], fallback_columns, plan: SparkPlan):
        parallel = min(plan.max_parallel, self.max_parallel or plan.max_parallel)
        if plan.by == "pages":
            with slicing.probing() as probe:
                first = _frame(fn(**kwargs), name)
            if not probe.seen or probe.total_rows is None:
                why = ("the read didn't go through duckduck.slicing.pages" if not probe.seen
                       else "the API didn't say how many rows there are")
                return self._driver(name, fn, kwargs, fallback_columns, plan, f"can't split: {why}")
            total_pages = max(1, math.ceil(probe.total_rows / max(1, probe.page_size or 1)))
            rest = list(range(1, total_pages))
            if not rest:
                self._decide(name, "partitioned", plan, f"1 page ({probe.total_rows} rows): read on the driver")
                return self._spark_frame(_arrow(first, fallback_columns))
            count = min(len(rest), parallel * 4)  # a few windows per task: an uneven one doesn't hold the rest up
            size = math.ceil(len(rest) / count)
            specs = [(rest[i], min(size, len(rest) - i)) for i in range(0, len(rest), size)]
            detail = (f"{probe.total_rows} rows in {total_pages} pages: the first page read on the driver, "
                      f"{len(specs)} windows on the executors, ≤{parallel} at a time")
            mode, fn_kwargs = "pages", kwargs
        else:
            pieces = list(getattr(_owner(fn), plan.by)(**kwargs))
            if not pieces:
                self._decide(name, "partitioned", plan, "no pieces: no rows")
                return self._spark_frame(_arrow(pd.DataFrame(), fallback_columns))
            first = _frame(fn(**pieces[0]), name)
            specs = pieces[1:]
            detail = f"{len(pieces)} pieces: the first on the driver, the rest on the executors, ≤{parallel} at a time"
            mode, fn_kwargs = "kwargs", {}
        first_table = _conform_flat(first)
        if not specs:
            self._decide(name, "partitioned", plan, detail)
            return self._spark_frame(first_table)
        from pyspark.sql.pandas.types import from_arrow_schema

        spark_schema = from_arrow_schema(first_table.schema)
        try:
            runner = _run_pieces(fn, fn_kwargs, mode, first_table.schema, name)
            from pyspark import cloudpickle

            cloudpickle.dumps(runner)  # the connector must travel to the executors
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: the connector can't be sent to the executors (%s) — reading every piece on the driver",
                           name, exc)
            return self._driver(name, fn, kwargs, fallback_columns, plan,
                                f"connector not serializable: {type(exc).__name__}")
        tasks = self.spark.createDataFrame([(pickle.dumps(s),) for s in specs], "spec binary") \
            .repartition(min(parallel, len(specs)))
        rest_df = tasks.mapInArrow(runner, spark_schema)
        self._decide(name, "partitioned", plan, detail)
        return self._spark_frame(first_table).unionByName(rest_df)

    # -- driver -----------------------------------------------------------------------------------------------

    def _driver(self, name: str, fn: Any, kwargs: Dict[str, Any], fallback_columns, plan: Optional[SparkPlan],
                reason: Optional[str]):
        stream = self.duck.streaming_function(name) if hasattr(self.duck, "streaming_function") else None
        if self.staging_path and stream is not None and kwargs.get("limit") is None:
            df = self._staged(name, stream, kwargs, fallback_columns)
            self._decide(name, "driver", plan, (reason + "; " if reason else "") +
                         f"page by page into Parquet at {self.staging_path}")
            return df
        data = fn(**kwargs)
        self._decide(name, "driver", plan, reason or "read on the driver by duckduck")
        return self._spark_frame(_arrow(_frame(data, name), fallback_columns))

    def _staged(self, name: str, stream: Callable, kwargs: Dict[str, Any], fallback_columns):
        import inspect

        import pyarrow.parquet as pq

        from pyarrow import fs as pafs

        accepted = inspect.signature(stream).parameters
        call = {k: v for k, v in kwargs.items() if k in accepted and k != "limit"}
        base = self.staging_path if "://" in self.staging_path else os.path.abspath(self.staging_path)
        folder = f"{base.rstrip('/')}/{name}-{uuid.uuid4().hex[:12]}"
        filesystem, inner = pafs.FileSystem.from_uri(folder)  # local, s3://, gs://, abfs://… — pyarrow's own
        filesystem.create_dir(inner, recursive=True)
        schema = None
        written = 0
        for page in stream(**call):
            frame = _frame(page, name)
            if not len(frame):
                continue
            table = _conform_flat(frame) if schema is None else _conform(frame, schema)
            schema = schema or table.schema
            pq.write_table(table, f"{inner}/part-{written:05d}.parquet", filesystem=filesystem)
            written += 1
        if not written:
            return self._spark_frame(_arrow(pd.DataFrame(), fallback_columns))
        return self.spark.read.parquet(self._path(folder))

    def _spark_frame(self, table: Any):
        major = int(str(self.spark.version).split(".")[0])
        return self.spark.createDataFrame(table if major >= 4 else table.to_pandas())


def _sql_value(v: Any) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _filtered(df: Any, conditions: Optional[List[Any]]):
    """The push-down's conditions as Spark filters (Spark then prunes files / pushes them to JDBC)."""
    from pyspark.sql import functions as F

    columns = {c.lower(): c for c in df.columns}
    for c in conditions or []:
        col = columns.get(str(c.column).lower())
        if col is None:
            continue
        ref, value = F.col(f"`{col}`"), c.value
        if c.op == "eq":
            df = df.filter(ref == F.lit(value))
        elif c.op == "like":
            df = df.filter(ref.like(str(value)))
        elif c.op == "ilike":
            df = df.filter(F.lower(ref).like(str(value).lower()))
        elif c.op in ("gt", "gte", "lt", "lte"):
            df = df.filter({"gt": ref > F.lit(value), "gte": ref >= F.lit(value),
                            "lt": ref < F.lit(value), "lte": ref <= F.lit(value)}[c.op])
    return df


class _SparkConnection:
    """What ``DuckAPI.sql()`` talks to, in Spark: temp views, and the final query translated to Spark SQL."""

    def __init__(self, reader: SparkReader):
        self.reader = reader

    def register(self, name: str, df: pd.DataFrame) -> None:
        self.reader._spark_frame(_arrow(df)).createOrReplaceTempView(name)

    def unregister(self, name: str) -> None:
        self.reader.spark.catalog.dropTempView(name)

    def sql(self, query: str):
        spark_sql = to_spark_sql(query)
        logger.debug("  Spark SQL: %s", " ".join(spark_sql.split()))
        return self.reader.spark.sql(spark_sql)

    def execute(self, query: str, *args: Any) -> Any:  # DuckAPI calls it only for things Spark has no say in
        raise NotImplementedError(query)

    def close(self) -> None:
        pass


@functools.lru_cache(maxsize=256)
def to_spark_sql(query: str) -> str:
    """DuckDB SQL → Spark SQL (sqlglot)."""
    import sqlglot

    return sqlglot.transpile(query, read="duckdb", write="spark")[0]


class _SparkDuck(DuckAPI):
    """``DuckAPI.sql()``'s planning (addresses, arguments, push-down) with every table read into Spark."""

    def __init__(self, reader: SparkReader):
        super().__init__(stream_pages=False, join_pushdown=False)  # its conn is Spark: no DuckDB to read key values from
        self.conn.close()
        self.conn = _SparkConnection(reader)
        self._reader = reader
        src = reader.duck
        for attr in ("functions", "service_of", "service_prefix", "failed_services", "views", "view_key",
                     "failed_views", "_streaming_functions", "default_database", "_config_path"):
            if hasattr(src, attr):
                setattr(self, attr, getattr(src, attr))

    def _pages_instead(self, fn_name: str, kwargs: Dict[str, Any]) -> bool:
        return False

    def _materialize(self, function_name: str, fetch_function: Any, kwargs: Dict[str, Any],
                     fallback_columns: Optional[List[str]] = None) -> tuple:
        df = self._reader.read(function_name, fetch_function, kwargs, fallback_columns)
        self._table_counter += 1
        view = f"_api_{function_name}_{self._table_counter}"
        df.createOrReplaceTempView(view)
        return view, list(df.columns)

"""
Engines run a pipeline's views and write its targets. Each one is small:
``define`` (make a view, kept — materialized — or not), ``query`` (a small
read in DuckDB SQL, written in the engine's dialect by sqlglot: the sip's
samples), ``columns``, ``write``, ``close``.

- ``duckdb`` (``DuckEngine``): views go through ``DuckAPI.sql`` — addresses,
  push-down, joins, everything ``duck.sql()`` does; a kept view is a DuckDB
  table. Writes Parquet (pyarrow: local, ``s3://``, ``gs://``, ``abfs://``…)
  or Delta (the ``deltalake`` package) to a path.
- ``spark`` (``SparkEngine``): views go through ``SparkReader.sql`` (the same
  planning, read by Spark); a kept view is a persisted DataFrame. Writes a
  path (``df.write``) or a catalog table (``saveAsTable`` / ``MERGE INTO``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .sip import _filesystem, ident, literal
from .spec import PipelineError, Target

logger = logging.getLogger("duckduck.pipeline")


def _is_data_file(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return not any(p.startswith(("_", ".")) for p in parts[-1:]) and not path.endswith((".crc", "$folder$"))


def _files_under(fs: Any, base: str) -> List[str]:
    import pyarrow.fs as pafs

    if fs.get_file_info(base).type == pafs.FileType.NotFound:
        return []
    return [i.path for i in fs.get_file_info(pafs.FileSelector(base, recursive=True))
            if i.type == pafs.FileType.File and _is_data_file(i.path)]


def _counting(reader: Any, counter: List[int]) -> Any:
    import pyarrow as pa

    def batches():
        for batch in reader:
            counter[0] += batch.num_rows
            yield batch
    return pa.RecordBatchReader.from_batches(reader.schema, batches())


def check_unique(engine: Any, view: str, key: List[str]) -> None:
    """A merge source with two rows for one key would update the target twice (or fail half-way): refused."""
    cols = ", ".join(ident(k) for k in key)
    dup = engine.query(f"SELECT {cols}, count(*) AS _n FROM {ident(view)} GROUP BY {cols} HAVING count(*) > 1 "
                       "LIMIT 3")
    if len(dup):
        examples = "; ".join(", ".join(f"{k}={r[k]}" for k in key) + f" ({r['_n']} rows)" for _, r in dup.iterrows())
        raise PipelineError(f"merge: {view} has more than one row for some keys ({examples}) — deduplicate it "
                            "(a _loaded_at column picks the newest by itself), or set \"unique\": false")


class DuckEngine:
    name = "duckdb"

    def __init__(self, duck: Any):
        self.duck = duck
        self.conn = duck.conn
        self._made: Dict[str, str] = {}  # name → "table" | "view"

    def _drop(self, name: str) -> None:
        kind = self._made.pop(name, None)
        if kind:
            self.conn.execute(f"DROP {'TABLE' if kind == 'table' else 'VIEW'} IF EXISTS {ident(name)}")

    def define(self, name: str, sql: str, keep: bool) -> None:
        if name in self.duck.functions:
            raise PipelineError(f"view {name} has the name of a registered table — DuckAPI would read the table "
                                "instead; rename the view")
        rel = self.duck.sql(sql)
        self._drop(name)
        if keep:
            rel.create_view("__duckduck_pipeline_rel", replace=True)
            self.conn.execute(f"CREATE OR REPLACE TABLE {ident(name)} AS SELECT * FROM __duckduck_pipeline_rel")
            self.conn.execute("DROP VIEW IF EXISTS __duckduck_pipeline_rel")
            self._made[name] = "table"
        else:
            rel.create_view(name, replace=True)
            self._made[name] = "view"

    def query(self, sql: str):
        return self.conn.execute(sql).df()

    def columns(self, name: str) -> List[str]:
        return [d[0] for d in self.conn.execute(f"SELECT * FROM {ident(name)} LIMIT 0").description]

    def rows(self, name: str) -> Optional[int]:
        return int(self.conn.execute(f"SELECT count(*) FROM {ident(name)}").fetchone()[0])

    def arrow(self, name: str):
        result = self.conn.execute(f"SELECT * FROM {ident(name)}")
        return (getattr(result, "to_arrow_table", None) or result.fetch_arrow_table)()

    def schema(self, name: str):
        """The view's columns as Hive/Glue types (an all-NULL column marked so it can become anything)."""
        import pyarrow as pa

        from .catalogs import Column, arrow_to_hive, null_column

        result = self.conn.execute(f"SELECT * FROM {ident(name)} LIMIT 0")
        table = (getattr(result, "to_arrow_table", None) or result.fetch_arrow_table)()
        return [null_column(f.name) if pa.types.is_null(f.type) else Column(f.name, arrow_to_hive(f.type))
                for f in table.schema]

    def frame(self, sql: str):
        """A query of the user's (a notebook cell), through DuckAPI."""
        return self.duck.sql(sql).df()

    def _reader(self, sql: str, counter: List[int]):
        result = self.conn.execute(sql)
        reader = getattr(result, "to_arrow_reader", None) or result.fetch_record_batch  # the newer name first
        return _counting(reader(100_000), counter)

    def write(self, target: Target, key: List[str], run_id: str) -> Dict[str, Any]:
        if target.table:
            raise PipelineError(f"the duckdb engine writes to a path (s3://…, a folder): {target.table} is a catalog "
                                "table — give the target a 'path', or run the pipeline with \"engine\": \"spark\"")
        if target.format == "iceberg":
            raise PipelineError("the duckdb engine writes Parquet or Delta; Iceberg needs \"engine\": \"spark\"")
        if target.mode == "merge" and target.unique:
            check_unique(self, target.view, key)
        if target.format == "delta":
            return self._write_delta(target, key)
        return self._write_parquet(target, key, run_id)

    def _write_parquet(self, target: Target, key: List[str], run_id: str) -> Dict[str, Any]:
        import pyarrow.dataset as ds

        fs, base = _filesystem(target.path)
        base = base.rstrip("/")
        old = _files_under(fs, base) if target.mode in ("overwrite", "merge") else []
        sql = f"SELECT * FROM {ident(target.view)}"
        if target.mode == "merge" and old:
            partitioning = ds.partitioning(flavor="hive") if target.partition_by else None
            existing = ds.dataset(old, filesystem=fs, format="parquet", partitioning=partitioning,
                                  partition_base_dir=base)
            self.conn.register("__duckduck_pipeline_old", existing)
            same = " AND ".join(f"n.{ident(k)} IS NOT DISTINCT FROM o.{ident(k)}" for k in key)
            sql = (f"SELECT * FROM __duckduck_pipeline_old o WHERE NOT EXISTS (SELECT 1 FROM {ident(target.view)} n "
                   f"WHERE {same}) UNION ALL BY NAME SELECT * FROM {ident(target.view)}")
        counter = [0]
        written: List[str] = []
        try:
            ds.write_dataset(
                self._reader(sql, counter), base, filesystem=fs, format="parquet",
                partitioning=target.partition_by or None, partitioning_flavor="hive" if target.partition_by else None,
                basename_template=f"part-{run_id}-{{i}}.parquet",
                existing_data_behavior="delete_matching" if target.mode == "overwrite_partitions"
                else "overwrite_or_ignore",
                file_visitor=lambda f: written.append(f.path))
        finally:
            if target.mode == "merge" and old:
                self.conn.unregister("__duckduck_pipeline_old")
        removed = [f for f in old if f not in set(written)]
        for f in removed:  # overwrite / merge: the new files are in place, the old ones go
            fs.delete_file(f)
        if target.mode == "merge" and old:  # the whole table was rewritten: say both
            merged = self.conn.execute(f"SELECT count(*) FROM {ident(target.view)}").fetchone()[0]
            return {"rows": int(merged), "table_rows": counter[0], "files": len(written), "removed": len(removed),
                    "_files": written}
        return {"rows": counter[0], "files": len(written), "removed": len(removed), "_files": written}

    def _write_delta(self, target: Target, key: List[str]) -> Dict[str, Any]:
        try:
            from deltalake import DeltaTable, write_deltalake
            from deltalake.exceptions import TableNotFoundError
        except ImportError:
            raise PipelineError("writing Delta with the duckdb engine needs the deltalake package: "
                                "pip install deltalake") from None
        uri = target.path if "://" in target.path else _filesystem(target.path)[1]
        opts = target.storage_options or None
        schema_mode = {"evolve": "merge", "overwrite": "overwrite"}.get(target.schema)  # strict: as it is
        counter = [0]
        reader = self._reader(f"SELECT * FROM {ident(target.view)}", counter)
        if target.mode == "merge":
            try:
                table = DeltaTable(uri, storage_options=opts)
            except TableNotFoundError:
                write_deltalake(uri, reader, mode="append", partition_by=target.partition_by or None,
                                storage_options=opts)
                return {"rows": counter[0], "created": True}
            same = " AND ".join(f't."{k}" = s."{k}"' for k in key)
            stats = (table.merge(reader, predicate=same, source_alias="s", target_alias="t",
                                 merge_schema=target.schema == "evolve")
                     .when_matched_update_all().when_not_matched_insert_all().execute())
            return {"rows": counter[0], **{k: v for k, v in (stats or {}).items()
                                           if k in ("num_target_rows_inserted", "num_target_rows_updated")}}
        if target.mode == "overwrite_partitions":
            if not target.partition_by:
                raise PipelineError("overwrite_partitions replaces the partitions written: set 'partition_by'")
            cols = ", ".join(ident(c) for c in target.partition_by)
            parts = self.conn.execute(f"SELECT DISTINCT {cols} FROM {ident(target.view)}").fetchall()
            if not parts:
                return {"rows": 0}
            predicate = " OR ".join(
                "(" + " AND ".join(f'"{c}" = {v if isinstance(v, (int, float)) else literal(v)}'
                                   for c, v in zip(target.partition_by, p)) + ")" for p in parts)
            write_deltalake(uri, reader, mode="overwrite", predicate=predicate,
                            partition_by=target.partition_by, storage_options=opts,
                            schema_mode="merge" if schema_mode else None)
            return {"rows": counter[0], "partitions": len(parts)}
        if schema_mode == "overwrite" and target.mode != "overwrite":
            schema_mode = "merge"
        write_deltalake(uri, reader, mode=target.mode, partition_by=target.partition_by or None,
                        storage_options=opts, schema_mode=schema_mode)
        return {"rows": counter[0]}

    def close(self) -> None:
        for name in list(self._made):
            try:
                self._drop(name)
            except Exception:  # noqa: BLE001
                pass


class SparkEngine:
    name = "spark"

    def __init__(self, spark: Any, duck: Any, reader: Any = None):
        if reader is None:
            from ..spark import SparkReader

            reader = SparkReader(spark, duck)
        self.spark = spark
        self.reader = reader
        self._kept: Dict[str, Any] = {}
        self._views: List[str] = []

    def define(self, name: str, sql: str, keep: bool) -> None:
        df = self.reader.sql(sql)
        old = self._kept.pop(name, None)
        if old is not None:
            old.unpersist()
        if keep:
            df = df.persist()
            self._kept[name] = df
        df.createOrReplaceTempView(name)
        if name not in self._views:
            self._views.append(name)

    def query(self, sql: str):
        from ..spark import spark_query

        text, helpers = spark_query(sql)
        df = self.spark.sql(text)
        return (df.drop(*helpers) if helpers else df).toPandas()

    def columns(self, name: str) -> List[str]:
        return list(self.spark.table(name).columns)

    def rows(self, name: str) -> Optional[int]:
        return None  # counting would run the view once more

    def frame(self, sql: str):
        return self.reader.sql(sql).toPandas()

    def arrow(self, name: str):
        df = self.spark.table(name)
        if hasattr(df, "toArrow"):  # Spark 4: Arrow batches straight to the driver
            return df.toArrow()
        import pyarrow as pa

        return pa.Table.from_pandas(df.toPandas(), preserve_index=False)

    def schema(self, name: str):
        from .catalogs import Column, null_column

        out = []
        for f in self.spark.table(name).schema.fields:
            t = f.dataType.simpleString()
            out.append(null_column(f.name) if t in ("void", "null") else Column(f.name, t))
        return out

    def _exists(self, target: Target) -> bool:
        if target.table:
            return bool(self.spark.catalog.tableExists(target.table))
        try:
            self.spark.read.format(target.format or "parquet").load(target.path).limit(0).collect()
            return True
        except Exception:  # noqa: BLE001 — no table there yet
            return False

    def write(self, target: Target, key: List[str], run_id: str) -> Dict[str, Any]:
        if target.mode == "merge" and target.unique:
            check_unique(self, target.view, key)
        df = self.spark.table(target.view)
        fmt = target.format or ("parquet" if target.path else None)
        if target.mode == "merge":
            if target.path and fmt not in ("delta", "iceberg"):
                raise PipelineError(f"merge into a path needs a table format (\"format\": \"delta\"), not {fmt}")
            if self._exists(target):
                where = f"delta.`{target.path}`" if target.path else target.table
                same = " AND ".join(f"t.`{k}` = s.`{k}`" for k in key)
                auto = "spark.databricks.delta.schema.autoMerge.enabled"
                before = self.spark.conf.get(auto, "false")
                if target.schema == "evolve":
                    self.spark.conf.set(auto, "true")  # new columns of the view join the Delta table
                try:
                    self.spark.sql(f"MERGE INTO {where} t USING `{target.view}` s ON {same} "
                                   "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *")
                finally:
                    self.spark.conf.set(auto, before)
                return {"merged": True}
            mode = "append"  # nothing to merge into yet: the first run creates it
        else:
            mode = target.mode
        writer = df.write.options(**target.storage_options)
        if fmt:
            writer = writer.format(fmt)
        if fmt == "delta" and target.schema == "evolve":
            writer = writer.option("mergeSchema", "true")
        if fmt == "delta" and target.schema == "overwrite":
            writer = writer.option("overwriteSchema", "true")
        if target.partition_by:
            writer = writer.partitionBy(*target.partition_by)
        if mode == "overwrite_partitions":
            writer = writer.option("partitionOverwriteMode", "dynamic")
            mode = "overwrite"
        writer = writer.mode(mode)
        if target.path:
            writer.save(target.path)
        else:
            writer.saveAsTable(target.table)
        return {}

    def close(self) -> None:
        for df in self._kept.values():
            try:
                df.unpersist()
            except Exception:  # noqa: BLE001
                pass
        for name in self._views:
            try:
                self.spark.catalog.dropTempView(name)
            except Exception:  # noqa: BLE001
                pass
        self._kept.clear()
        self._views.clear()


def make_engine(engine: str, duck: Any = None, spark: Any = None):
    if engine == "spark":
        if spark is None:
            try:
                from pyspark.sql import SparkSession
            except ImportError:
                raise PipelineError("\"engine\": \"spark\" needs pyspark (pip install \"duckduck[spark]\")") from None
            spark = SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
        return SparkEngine(spark, duck)
    return DuckEngine(duck)

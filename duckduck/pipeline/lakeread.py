"""
A pipeline reading the lake's own tables: ``FROM inventory.assets`` (a table of
the lake's catalog) or ``FROM raw.inventory.assets`` (that layer's table).

Each one found after FROM / JOIN becomes a table of the run's DuckAPI
(``_lake_<…>``), read how ``read_engine`` says — the pipeline file's, else
duckduck.json's ``lake.read_engine``:

* ``duckdb`` (default): the files themselves (Parquet / Delta / Iceberg),
  through pyarrow with the lake's AWS account and DuckDB on top — the
  WHERE (an incremental load's ``_loaded_at > …`` too) prunes files and row
  groups before anything is read, and a big table is read a batch at a time;
* ``athena``: the query runs on Athena (``lake.athena``: workgroup,
  output_location…), the WHERE written in Athena's SQL.

With ``"engine": "spark"`` Spark reads it natively: by its name when the
session's catalog has it (a Glue job), else its files.

A layer past ingestion (silver, gold — every layer but ``lake.ingestion_layers``,
default the first of ``lake.layers``) reads only the lake: a connector's table,
a saved table or a file is *virtualized* — it never went through raw — and is
refused unless the pipeline says so: ``"virtualized_table": true`` (or the
list of the ones it may read).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd

from ..pushdown import Condition, conditions_to_sql
from ..sparkplan import SparkSource, spark_plan
from .spec import PipelineError

logger = logging.getLogger("duckduck.pipeline")

READ_ENGINES = ("duckdb", "athena")
ATHENA_KEYS = {"workgroup", "output_location", "region", "catalog", "results", "reuse_minutes", "timeout"}
_PREFIX = "_lake_"


def ingestion_layers(lake: Dict[str, Any]) -> List[str]:
    """The layers that read the sources (connectors, files): ``lake.ingestion_layers``, else the first layer."""
    given = lake.get("ingestion_layers")
    if given:
        return list(given)
    layers = list((lake.get("layers") or {}))
    return layers[:1]


def _split(ref: str) -> List[str]:
    """A reference as the analysis names it (parts joined by ``.``; a call kept whole)."""
    return [ref] if "(" in ref else ref.split(".")


def lake_parts(parts: List[str], lake: Dict[str, Any], duck: Any = None) -> Optional[List[str]]:
    """``[layer?, database, table]`` when a reference names a table of the lake, else None (a connector's
    table, a saved table, a file: virtualized)."""
    if not parts or any("(" in p for p in parts):
        return None
    layers = lake.get("layers") or {}
    if len(parts) == 3 and parts[0] in layers:
        return list(parts)
    if len(parts) != 2:
        return None
    if duck is not None:
        from ..addresses import services

        if parts[0].lower() in services(duck):
            return None
    return [None, parts[0], parts[1]]


def check_sources(spec: Any, plan: Any, duck: Any = None) -> None:
    """A layer past ingestion reads only the lake — anything else must be declared ``virtualized_table``."""
    lake = spec.lake or {}
    written = sorted({t.layer for t in spec.targets if t.layer})
    ingest = ingestion_layers(lake)
    past = [layer for layer in written if layer not in ingest]
    if not past or spec.virtualized is True:
        return
    allowed = {str(v).lower() for v in (spec.virtualized or [])}
    found = []
    for name in plan.needed:
        for ref in plan.views[name].external:
            if lake_parts(_split(ref), lake, duck) is None and ref.lower() not in allowed and ref not in found:
                found.append(ref)
    if found:
        one = len(found) == 1
        first = (ingest or ["raw"])[0]
        names = "[" + ", ".join(f'"{f}"' for f in found) + "]"
        raise PipelineError(
            f"{past[0]} reads only the lake: {', '.join(found)} {'is' if one else 'are'} not a table of the lake "
            f"(a connector's table, a saved table or a file — virtualized), so {'it' if one else 'they'} never went "
            f"through {first}. Ingest {'it' if one else 'them'} there and read <database>.<table> (or "
            f"{first}.<database>.<table>), or say it's on purpose: \"virtualized_table\": true (or {names})")


# ---------------------------------------------------------------------------
# Finding the table
# ---------------------------------------------------------------------------


def _under(location: str, prefix: str) -> bool:
    def norm(p: str) -> str:
        p = p.split("://", 1)[1] if "://" in p else p
        return p.rstrip("/") + "/"
    return norm(location).startswith(norm(prefix))


def _exists(path: str) -> bool:
    import pyarrow.fs as pafs

    from .sip import _filesystem

    fs, inner = _filesystem(path)
    return fs.get_file_info(inner.rstrip("/")).type != pafs.FileType.NotFound


def locate(parts: List[Optional[str]], lake: Dict[str, Any], catalog_of: Any) -> Tuple[Any, Optional[Any]]:
    """(TableInfo, the catalog it's in — None for a lake of files only)."""
    from .catalogs import TableInfo

    layer, database, name = parts
    written = ".".join(p for p in parts if p)
    layers = lake.get("layers") or {}
    if lake.get("catalog"):
        catalog = catalog_of(lake["catalog"])
        info = catalog.table(database, name)
        if info is None:
            raise PipelineError(f"{written}: no table {database}.{name} in catalog {catalog.name}")
        if layer and not _under(info.location, layers[layer]):
            raise PipelineError(
                f"{written}: {database}.{name} in catalog {catalog.name} is at {info.location}, not in the {layer} "
                f"layer ({layers[layer]}) — two layers write the same database.table there; give each layer's "
                f"targets their own \"database\"")
        return info, catalog
    if layer is None:
        found = [lay for lay, prefix in layers.items() if _exists(f"{prefix.rstrip('/')}/{database}/{name}")]
        if not found:
            raise PipelineError(f"{written}: no {database}/{name} in any layer of the lake "
                                f"({', '.join(layers) or 'no layers'}) — and the lake has no catalog")
        if len(found) > 1:
            raise PipelineError(f"{written}: {', '.join(found)} all have {database}/{name} — write which: "
                                + " or ".join(f"{lay}.{database}.{name}" for lay in found))
        layer = found[0]
    location = f"{layers[layer].rstrip('/')}/{database}/{name}"
    if not _exists(location):
        raise PipelineError(f"{written}: nothing at {location}")
    fmt = "delta" if _exists(f"{location}/_delta_log") else "parquet"
    return TableInfo(database, name, location, fmt), None


# ---------------------------------------------------------------------------
# Reading it
# ---------------------------------------------------------------------------


def _is_data(relative: str) -> bool:
    """Spark's rule: a ``_x`` / ``.x`` part is hidden, unless it's a partition (``_load_date=…``)."""
    parts = relative.replace("\\", "/").split("/")
    hidden = any(p.startswith(("_", ".")) and "=" not in p for p in parts)
    return not hidden and not relative.endswith((".crc", "$folder$", ".json"))


class LakeTable:
    """One table of the lake as a DuckAPI table: ``where`` / ``limit`` go into the read."""

    __module__ = __name__

    def __init__(self, info: Any, how: str, account: Any = None, catalog: Any = None, athena: Any = None,
                 address: str = ""):
        self.info, self.how, self.account, self.catalog, self.athena = info, how, account, catalog, athena
        self.address = address or info.full_name
        self.base_url = info.location  # what list_tables() shows as the endpoint
        #: Athena writes IN lists; the files' DuckDB scan takes everything conditions_to_sql writes
        self.WHERE_OPS = frozenset({"eq", "like", "ilike", "gt", "gte", "lt", "lte", "in"})
        self.__doc__ = (f"{self.address}: {info.format} at {info.location}, read "
                        f"{'on Athena' if how == 'athena' else 'from its files (WHERE/LIMIT inside the read)'}.")

    def describe(self) -> str:
        return f"{self.address} → {self.info.location} ({self.info.format}, {self.how})"

    @spark_plan("native", source="_spark_source",
                why="a table of the lake: Spark reads it by its catalog name, else its files")
    def __call__(self, where: Optional[List[Condition]] = None, limit: Optional[int] = None) -> pd.DataFrame:
        from . import aws

        with aws.using(self.account):
            if self.how == "athena":
                return self.athena.table(self.info.database, self.info.name, where=where, limit=limit)
            con, rel = self._relation(where, limit)
            try:
                return rel.df()
            finally:
                con.close()

    def stream(self, where: Optional[List[Condition]] = None) -> Iterator[pd.DataFrame]:
        """A batch at a time (``sql()`` reads it page by page when no LIMIT reaches it)."""
        from . import aws

        with aws.using(self.account):
            if self.how == "athena":
                yield from self.athena.iter_table(self.info.database, self.info.name, where=where)
                return
            con, rel = self._relation(where, None)
            try:
                reader = rel.to_arrow_reader(100_000) if hasattr(rel, "to_arrow_reader") \
                    else rel.fetch_record_batch(100_000)
                empty = True
                for batch in reader:
                    if batch.num_rows:
                        empty = False
                        yield batch.to_pandas()
                if empty:  # nothing matched: still the table's columns, so the step keeps its shape
                    yield reader.schema.empty_table().to_pandas()
            finally:
                con.close()

    def _spark_source(self) -> SparkSource:
        return SparkSource(self.info.format, path=self.info.location,
                           table=self.info.full_name if self.catalog is not None else None)

    # -- the files, through pyarrow, DuckDB on top ----------------------------------------------------------------

    def _relation(self, where: Optional[List[Condition]], limit: Optional[int]):
        import duckdb

        data, casts = self._data()
        con = duckdb.connect()
        con.register("lake_table", data)
        names = list(data.schema.names)
        cols = ", ".join(f'CAST("{c}" AS {casts[c]}) AS "{c}"' if c in casts else f'"{c}"' for c in names)
        con.execute(f"CREATE VIEW t AS SELECT {cols} FROM lake_table")
        cond = conditions_to_sql(where or [], names)
        sql = "SELECT * FROM t" + (f" WHERE {cond}" if cond else "") + (f" LIMIT {int(limit)}" if limit else "")
        logger.info("  lake %s: %s", self.address, sql.replace("SELECT * FROM t", f"read {self.info.location}"))
        return con, con.sql(sql)

    def _data(self) -> Tuple[Any, Dict[str, str]]:
        """A pyarrow dataset (or table) of the table, and the DuckDB types its partition columns are cast to."""
        import pyarrow as pa
        import pyarrow.dataset as ds
        import pyarrow.fs as pafs

        from ..s3layout import duck_type
        from . import aws
        from .sip import _filesystem

        info = self.info
        if info.format == "delta":
            try:
                from deltalake import DeltaTable
            except ImportError:
                raise PipelineError(f"{self.address} is a Delta table: pip install \"duckduck[delta]\"") from None
            options = aws.delta_options(self.account) if aws.is_s3(info.location) else {}
            return DeltaTable(info.location, storage_options=options or None).to_pyarrow_dataset(), {}
        if info.format == "iceberg":
            table = self.catalog.iceberg().load_table((info.database, info.name)) if self.catalog is not None \
                and hasattr(self.catalog, "iceberg") else None
            if table is None:
                raise PipelineError(f"{self.address}: an Iceberg table is read through its catalog")
            return table.scan().to_arrow(), {}
        fs, base = _filesystem(info.location)
        base = base.rstrip("/")
        selector = pafs.FileSelector(base, recursive=True, allow_not_found=True)
        files = [i.path for i in fs.get_file_info(selector)
                 if i.type == pafs.FileType.File and i.size and _is_data(i.path[len(base) + 1:])]
        keys = [k.name for k in info.partition_keys]
        if not keys and files:  # a lake of files only: the k=v folders say the partitions
            first = files[0][len(base) + 1:].split("/")[:-1]
            keys = [p.split("=", 1)[0] for p in first if "=" in p]
        partitioning = ds.partitioning(pa.schema([(k, pa.string()) for k in keys]), flavor="hive") if keys else None
        if not files:
            schema = pa.schema([(c.name, pa.string()) for c in info.columns + info.partition_keys]) \
                if info.columns else pa.schema([("_no_rows", pa.string())])
            return pa.table({n: pa.array([], type=pa.string()) for n in schema.names}), {}
        data = ds.dataset(files, filesystem=fs, format="parquet", partitioning=partitioning,
                          partition_base_dir=base)
        types = {k.name: k.type for k in info.partition_keys}
        casts = {k: duck_type(types[k]) if k in types else ("DATE" if k == "_load_date" else "VARCHAR")
                 for k in keys}
        casts = {k: v for k, v in casts.items() if v != "VARCHAR"}
        return data, casts


# ---------------------------------------------------------------------------
# Into the run
# ---------------------------------------------------------------------------


def _athena_of(lake: Dict[str, Any], account: Any) -> Any:
    from ..athena import Athena

    block = dict(lake.get("athena") or {})
    region = block.pop("region", None) or (account.region if account else None)
    keys = {}
    if account is not None and account.has_keys:
        keys = {"aws_access_key_id": account.access_key, "aws_secret_access_key": account.secret_key,
                "aws_session_token": account.session_token}
    return Athena(region_name=region, **keys, **block)


def _function_name(parts: List[Optional[str]]) -> str:
    return _PREFIX + "__".join(re.sub(r"[^a-z0-9_]", "_", p.lower()) for p in parts if p)


class LakeReads:
    """The lake tables a run reads: found in its views' SQL, registered in its DuckAPI, unregistered after."""

    def __init__(self, lake: Dict[str, Any], duck: Any, catalog_of: Any, account: Any = None,
                 read_engine: Optional[str] = None):
        self.duck, self.catalog_of, self.account = duck, catalog_of, account
        self.lake = lake or {}
        self.how = read_engine or self.lake.get("read_engine") or "duckdb"
        self.tables: Dict[str, LakeTable] = {}  # function name → its table
        self._athena = None

    def rewrite(self, sql: str) -> str:
        """Every lake table after FROM / JOIN rewritten into its function (the last part as its alias)."""
        from ..addresses import _PLAIN, _REF, _masked, _parts

        if "." not in sql:
            return sql
        masked = _masked(sql)
        edits = []
        for m in _REF.finditer(masked):
            parts = lake_parts(_parts(m.group(3)), self.lake, self.duck)
            if parts is None:
                continue
            name = self._register(parts)
            alias = parts[-1].lower()
            text = name
            if self.duck._alias_at(masked, m.end()) is None and _PLAIN.match(alias):
                text += f" AS {alias}"
            edits.append((m.start(3), m.end(3), text))
        for start, end, text in reversed(edits):
            sql = sql[:start] + text + sql[end:]
        return sql

    def _register(self, parts: List[Optional[str]]) -> str:
        name = _function_name(parts)
        if name in self.tables:
            return name
        from . import aws

        with aws.using(self.account):
            info, catalog = locate(parts, self.lake, self.catalog_of)
        how = self.how
        if how == "athena":
            if catalog is None or getattr(catalog, "kind", "") != "glue":
                raise PipelineError(f"\"read_engine\": \"athena\" reads tables of a Glue catalog — this lake's "
                                    f"tables are {'files only' if catalog is None else 'in a ' + catalog.kind} "
                                    "catalog; use \"duckdb\"")
            if self._athena is None:
                self._athena = _athena_of(self.lake, self.account)
        address = ".".join(p for p in parts if p)
        table = LakeTable(info, how, self.account, catalog, self._athena, address)
        self.duck.register_api_function(name, table)
        self.duck.register_streaming_function(name, table.stream)
        self.tables[name] = table
        logger.info("  reads the lake: %s", table.describe())
        return name

    def close(self) -> None:
        for name in self.tables:
            self.duck.functions.pop(name, None)
            getattr(self.duck, "_streaming_functions", {}).pop(name, None)
        self.tables.clear()


def read_engine_problem(value: Any, where: str) -> Optional[str]:
    if value is None or value in READ_ENGINES:
        return None
    hint = " (pandas: use \"duckdb\" — it reads the files with pyarrow too, without holding everything in memory)" \
        if value == "pandas" else ""
    return f"{where} is one of {list(READ_ENGINES)}{hint}"


__all__ = ["LakeReads", "LakeTable", "READ_ENGINES", "check_sources", "ingestion_layers", "lake_parts", "locate"]

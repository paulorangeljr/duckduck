"""
Writing a target: the storage first (the engine's own writers), then the
catalog (its own API). A target without a ``catalog`` is the storage only, as
before; with one, ``catalogs.py`` keeps the table: created when missing, its
schema evolved, its partitions registered.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from ..s3layout import duck_type
from .catalogs import Column, TableInfo, hive_escape, partitions_from_files, plan_schema
from .sip import ident
from .spec import PipelineError, Target

logger = logging.getLogger("duckduck.pipeline")


def _empty(engine: Any, view: str) -> bool:
    return len(engine.query(f"SELECT 1 AS x FROM {ident(view)} LIMIT 1")) == 0


def with_audit(engine: Any, view: str, audit: Dict[str, str]) -> str:
    """The view plus the audit columns (``name → SQL literal``) — replacing ones of the same name it already has
    (silver reading bronze's ``_loaded_at``: what's written says when *this* table got the row)."""
    if not audit:
        return view
    names = {n.lower() for n in audit}
    cols = [ident(c) for c in engine.columns(view) if c.lower() not in names]
    cols += [f"{expr} AS {ident(name)}" for name, expr in audit.items()]
    out = f"__duckduck_audit_{view}"
    engine.define(out, f"SELECT {', '.join(cols)} FROM {ident(view)}", keep=False)
    return out


def write_target(engine: Any, target: Target, key: List[str], run_id: str,
                 catalog_of: Callable[[str], Any], audit: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    if target.mode != "overwrite" and _empty(engine, target.view):
        # nothing new (a watermark past everything): append / merge / partitions have nothing to do — and an
        # empty result may have lost its column types, which must never reach a table's schema
        logger.info("  %s: no rows — nothing written to %s", target.view, target.where)
        return {"rows": 0, "skipped": True}
    target = replace(target, view=with_audit(engine, target.view, audit or {}))
    if not target.catalog:
        return {k: v for k, v in engine.write(target, key, run_id).items() if not k.startswith("_")}
    return write_table(engine, catalog_of(target.catalog), target, key, run_id)


def _split(table: str):
    database, _, name = table.rpartition(".")
    return database, name


def write_table(engine: Any, catalog: Any, target: Target, key: List[str], run_id: str) -> Dict[str, Any]:
    database, name = _split(target.table)
    info = catalog.table(database, name)
    view_cols = engine.schema(target.view)
    fmt = info.format if info else (target.format or ("iceberg" if catalog.kind == "iceberg" else "parquet"))
    if info and target.format and target.format != info.format:
        raise PipelineError(f"{target.where} is a {info.format} table; the target says {target.format}")
    if catalog.kind == "iceberg" and fmt != "iceberg":
        raise PipelineError(f"catalog {catalog.name} keeps Iceberg tables only: set \"format\": \"iceberg\"")
    if fmt == "iceberg":
        return write_iceberg(engine, catalog, target, info, database, name, key, view_cols)
    by_lower = {c.name.lower(): c for c in view_cols}
    if info:
        keys = [k.name for k in info.partition_keys]
        if target.partition_by and [p.lower() for p in target.partition_by] != [k.lower() for k in keys]:
            raise PipelineError(f"{target.where} is partitioned by {keys or 'nothing'}; the target says "
                                f"{target.partition_by}")
    else:
        keys = list(target.partition_by)
    partition_by = [by_lower[k.lower()].name if k.lower() in by_lower else k for k in keys]
    plan = plan_schema(info, view_cols, target.schema, target.mode, partition_by)
    location = info.location if info else (target.path or catalog.default_location(database, name))

    # what's written: the view, cast to the table's types, the table's missing columns as NULL, NULL columns as text
    nulls = [c.name for c in view_cols if getattr(c, "_null", False) and c.name not in plan.casts]
    source = target.view
    if plan.casts or plan.missing or nulls:
        cols = []
        for c in view_cols:
            if c.name in plan.casts:
                cols.append(f"CAST({ident(c.name)} AS {duck_type(plan.casts[c.name])}) AS {ident(c.name)}")
            elif c.name in nulls:
                cols.append(f"CAST({ident(c.name)} AS VARCHAR) AS {ident(c.name)}")
            else:
                cols.append(ident(c.name))
        cols += [f"CAST(NULL AS {duck_type(m.type)}) AS {ident(m.name)}" for m in plan.missing]
        source = f"__duckduck_write_{target.view}"
        engine.define(source, f"SELECT {', '.join(cols)} FROM {ident(target.view)}", keep=False)
    storage = replace(target, view=source, path=location, table=None, catalog=None, format=fmt,
                      partition_by=partition_by)
    result = engine.write(storage, key, run_id)
    files = result.pop("_files", None)

    parts = {p.lower() for p in partition_by}
    typed = [Column(c.name, plan.casts.get(c.name, c.type)) for c in view_cols]
    data_cols = [c for c in typed if c.name.lower() not in parts]
    key_cols = [next(c for c in typed if c.name.lower() == p.lower()) for p in partition_by]
    if info is None:
        info = TableInfo(database, name, location, fmt, columns=data_cols, partition_keys=key_cols)
        catalog.create(info)
        info = catalog.table(database, name) or info
        result["created"] = True
    elif plan.replace:
        catalog.set_columns(info, data_cols)
        info.columns = data_cols
        result["schema_replaced"] = True
    elif plan.added:
        merged = list(info.columns) + plan.added
        catalog.set_columns(info, merged)
        info.columns = merged
        result["columns_added"] = [c.name for c in plan.added]
    if fmt == "parquet" and partition_by:
        if files is not None:
            written = partitions_from_files(location if "://" in location else _local(location), files, partition_by)
        else:
            written = _partitions_by_query(engine, target.view, partition_by)
        result.update(catalog.sync_partitions(info, written, replace=target.mode == "overwrite"))
    result["table"] = f"{catalog.name}:{database}.{name}"
    return result


def _local(path: str) -> str:
    from .sip import _filesystem

    return _filesystem(path)[1]


def _partitions_by_query(engine: Any, view: str, keys: List[str]):
    """The partitions a view's rows fall in, as an engine without a list of written files (Spark) wrote them."""
    cols = ", ".join(ident(k) for k in keys)
    rows = engine.query(f"SELECT DISTINCT {cols} FROM {ident(view)}")
    out = []
    for _, r in rows.iterrows():
        raw = {k: (None if r[k] is None or (isinstance(r[k], float) and r[k] != r[k]) else r[k]) for k in keys}
        text = {k: None if v is None else (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in raw.items()}
        values = {k: "__HIVE_DEFAULT_PARTITION__" if v is None else v for k, v in text.items()}
        out.append((values, "/".join(f"{k}={hive_escape(text[k])}" for k in keys)))
    return out


def write_iceberg(engine: Any, catalog: Any, target: Target, info: Optional[TableInfo], database: str, name: str,
                  key: List[str], view_cols: List[Column]) -> Dict[str, Any]:
    """An Iceberg table through pyiceberg: created, its schema evolved (``union_by_name``), then appended /
    overwritten / partitions overwritten / upserted — the same calls for any catalog pyiceberg speaks."""
    import pyarrow as pa

    from .engines import check_unique

    cat = catalog.iceberg()
    if target.mode == "merge" and target.unique:
        check_unique(engine, target.view, key)
    data = engine.arrow(target.view)
    if any(pa.types.is_null(f.type) for f in data.schema):  # an all-NULL column: text, like everywhere else
        data = data.cast(pa.schema([pa.field(f.name, pa.string()) if pa.types.is_null(f.type) else f
                                    for f in data.schema]))
    result: Dict[str, Any] = {"rows": data.num_rows}
    if info is None:
        try:
            cat.create_namespace_if_not_exists(database)
        except AttributeError:
            from pyiceberg.exceptions import NamespaceAlreadyExistsError

            try:
                cat.create_namespace(database)
            except NamespaceAlreadyExistsError:
                pass
        kwargs = {"location": target.path} if target.path else {}
        table = cat.create_table((database, name), schema=data.schema, **kwargs)
        if target.partition_by:
            with table.update_spec() as spec:
                for p in target.partition_by:
                    spec.add_identity(p)
        result["created"] = True
    else:
        table = info.raw if info.raw is not None else cat.load_table((database, name))
    current = {f.name.lower(): f for f in table.schema().fields}
    incoming = {f.name.lower() for f in data.schema}
    new = [f.name for f in data.schema if f.name.lower() not in current]
    missing = [f.name for n, f in current.items() if n not in incoming]
    if target.schema == "strict" and (new or missing) and not result.get("created"):
        raise PipelineError(f"{target.where}: \"schema\": \"strict\" — new {new or 'none'}, missing "
                            f"{missing or 'none'}")
    if target.schema == "fixed" and new and not result.get("created"):
        raise PipelineError(f"{target.where}: {', '.join(new)} — new column(s), and schema evolution is off "
                            "(\"schema_evolution\": false)")
    if target.schema == "overwrite" and not result.get("created"):
        with table.update_schema(allow_incompatible_changes=True) as update:
            for col in missing:
                update.delete_column(col)
            update.union_by_name(data.schema)
        result["schema_replaced"] = True
    elif new and not result.get("created"):
        with table.update_schema() as update:
            update.union_by_name(data.schema)
        result["columns_added"] = new
    if result.get("columns_added") or result.get("schema_replaced"):
        table = cat.load_table((database, name))  # the evolved schema, as the catalog now has it
    wanted = table.schema().as_arrow()
    fields = []
    for f in wanted:  # the table's types (a widening cast where they differ), its missing columns as NULL
        if f.name in data.schema.names:
            col = data.column(f.name)
            fields.append(col if col.type == f.type else col.cast(f.type))
        else:
            fields.append(pa.nulls(data.num_rows, f.type))
    data = pa.table(fields, schema=wanted)
    if target.mode == "append":
        table.append(data)
    elif target.mode == "overwrite":
        table.overwrite(data)
    elif target.mode == "overwrite_partitions":
        table.dynamic_partition_overwrite(data)
    else:  # merge: the rows with these keys replaced by the view's, in one commit
        from pyiceberg.table.upsert_util import create_match_filter

        match = create_match_filter(data.select(key), key)
        matched = len(table.scan(row_filter=match, selected_fields=tuple(key)).to_arrow()) if data.num_rows else 0
        with table.transaction() as tx:
            if matched:
                tx.delete(match)
            tx.append(data)
        result.update(rows_updated=matched, rows_inserted=data.num_rows - matched)
    result["table"] = f"{catalog.name}:{database}.{name}"
    logger.info("  Iceberg %s.%s: %s %d row(s)", database, name, target.mode, data.num_rows)
    return result

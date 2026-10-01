"""
The sip: a few rows followed through every step of a pipeline, written to
the lake next to the data.

**Which rows.** A key is followed when the first 8 hex digits of
``md5(key as text)`` fall under ``rate`` (``substr(md5(k), 1, 8) <= 'xxxxxxxx'``)
— a plain SQL filter every engine runs (sqlglot writes it per dialect), and
the *same* keys in every step, every run and every pipeline that uses the
same rate: silver and gold follow the same rows without talking to each
other. ``watch`` keys are always followed. ``max_rows`` caps a run by keeping
the smallest hashes (so runs still overlap as much as they can).

**What it costs.** One filtered read of each followed view (kept in the
engine: a DuckDB table, a persisted Spark DataFrame) and, when ``null_keys``
is on, one count of rows without a key. A GROUP BY adds one small query over
its input, limited to the followed keys, to learn each key's group.

**What it says** (``event``): ``seen``, ``duplicated`` (more than one row here,
one before — or, where the sip starts, more than one), ``changed``
(a recorded column changed from the step before: ``changed`` lists them),
``dropped`` (gone since the step before — ``note`` says where), ``new`` (not
in the step before), ``grouped`` (folded into a group: ``stage_key`` is the
group, ``row`` the group's row), ``null_keys`` (rows without a key: ``n``),
``lineage_break`` / ``sip_error`` (``note``). ``key`` is always the key the
row started with, so ``WHERE key = 'A-1001' ORDER BY position`` is its whole
way. ``row`` holds the key and the ``columns`` allow-list only (``mask``ed
ones as a hash) — nothing else leaves the pipeline.

The sip never fails the pipeline: a problem is an event and a warning.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd
import sqlglot
from sqlglot import exp

from .analysis import View, leftmost
from .spec import SipSpec

logger = logging.getLogger("duckduck.pipeline")

EVENT_COLUMNS = ["pipeline", "run_id", "run_at", "run_date", "engine", "stage", "position", "key", "stage_key",
                 "event", "n", "changed", "row", "note"]


def ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def key_sql(parts: List[str]) -> str:
    """The key as one text value: ``CAST(k AS VARCHAR)``, or the parts joined by ``|``."""
    casts = [f"CAST({p} AS VARCHAR)" for p in parts]
    return casts[0] if len(casts) == 1 else f"concat_ws('|', {', '.join(casts)})"


def hash_sql(key: str) -> str:
    return f"substr(md5({key}), 1, 8)"


def in_list(expr: str, values: List[str]) -> str:
    return f"{expr} IN ({', '.join(literal(v) for v in values)})" if values else "FALSE"


def _plain(value: Any) -> Any:
    """A cell as JSON can hold it."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        if pd.isna(value) is True:
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and not isinstance(value, (list, dict, str, bytes)):
        try:
            return _plain(value.item())
        except (ValueError, AttributeError):
            pass
    if isinstance(value, (dt.datetime, dt.date, dt.time, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "tolist"):
        return [_plain(v) for v in value.tolist()]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass
class StageState:
    view: str
    columns: List[str]  # the key's columns at this view, as named there
    hash_mode: bool  # its keys are the keys followed (no GROUP BY on the way): picked by hash
    cutoff: str
    rows: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # stage key → its (first) row, recorded columns
    counts: Dict[str, int] = field(default_factory=dict)
    stage_of: Dict[str, Optional[str]] = field(default_factory=dict)  # followed key → its key here (None: gone)


class Sip:
    """Follows the sampled keys view by view (``observe``), then ``save``s the events to the lake."""

    def __init__(self, spec: SipSpec, engine: Any, pipeline: str, run_id: str, run_at: str, run_date: str):
        self.spec = spec
        self.engine = engine
        self.base = {"pipeline": pipeline, "run_id": run_id, "run_at": run_at, "run_date": run_date,
                     "engine": getattr(engine, "name", "")}
        self.events: List[Dict[str, Any]] = []
        self.states: Dict[str, StageState] = {}
        self.position = 0
        self.saved_to: Optional[str] = None
        self.warnings: List[str] = []
        self._columns = {c.lower() for c in spec.columns}
        self._mask = {c.lower() for c in spec.mask}

    # -- events ----------------------------------------------------------

    def _event(self, view: str, event: str, key: Optional[str] = None, stage_key: Optional[str] = None,
               n: Optional[int] = None, row: Optional[Dict[str, Any]] = None, changed: Optional[List[str]] = None,
               note: str = "") -> None:
        self.events.append({**self.base, "stage": view, "position": self.position, "key": key,
                            "stage_key": stage_key, "event": event, "n": n,
                            "changed": ",".join(changed) if changed else None,
                            "row": json.dumps(row, default=str, ensure_ascii=False) if row is not None else None,
                            "note": note or None})

    def _record(self, row: pd.Series, key_columns: List[str]) -> Dict[str, Any]:
        out = {}
        keys = {c.lower() for c in key_columns}
        for col, value in row.items():
            lc = str(col).lower()
            if lc.startswith("_sip_") or (lc not in keys and lc not in self._columns):
                continue
            v = _plain(value)
            if lc in self._mask and v is not None:
                v = "sha256:" + hashlib.sha256(str(v).encode()).hexdigest()[:16]
            out[str(col)] = v
        return out

    def _collect(self, df: pd.DataFrame, key_columns: List[str]) -> tuple:
        rows: Dict[str, Dict[str, Any]] = {}
        counts: Dict[str, int] = {}
        for _, r in df.iterrows():
            k = r["_sip_key"]
            if k is None or (isinstance(k, float) and math.isnan(k)):
                continue
            k = str(k)
            counts[k] = counts.get(k, 0) + 1
            if k not in rows:
                rows[k] = self._record(r, key_columns)
        return rows, counts

    @staticmethod
    def _changed(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> List[str]:
        if not before:
            return []
        lower = {k.lower(): v for k, v in before.items()}
        return [k for k, v in after.items() if k.lower() in lower and json.dumps(lower[k.lower()], default=str)
                != json.dumps(v, default=str)]

    # -- following ---------------------------------------------------------

    def observe(self, view: View) -> None:
        """Records what happened to the followed keys at this view. Never raises (but a cancel goes through)."""
        self.position += 1
        try:
            self._observe(view)
        except Exception as exc:  # noqa: BLE001 — the sip must never fail the pipeline
            msg = f"sip at {view.name}: {exc.__class__.__name__}: {exc}"
            logger.warning(msg)
            self.warnings.append(msg)
            self._event(view.name, "sip_error", note=str(exc)[:500])

    def _observe(self, view: View) -> None:
        key = view.key
        names = self.engine.columns(view.name)
        by_lower = {c.lower(): c for c in names}
        cols = [by_lower.get(c.lower()) for c in key.columns]
        if None in cols:
            missing = [c for c, a in zip(key.columns, cols) if a is None]
            note = f"{view.name} has no column {', '.join(missing)} — the key can't be followed past here"
            logger.warning("sip: %s", note)
            self.warnings.append(note)
            self._event(view.name, "lineage_break", note=note)
            return
        up = self.states.get(key.upstream) if key.upstream else None
        if up is None:
            state = self._root(view, cols)
        elif key.mode == "group":
            state = self._group(view, cols, up)
        else:
            state = self._row(view, cols, up)
        self.states[view.name] = state
        if self.spec.null_keys and key.mode == "row":
            missing = " OR ".join(f"{ident(c)} IS NULL" for c in cols)
            n = int(self.engine.query(f"SELECT count(*) AS n FROM {ident(view.name)} WHERE {missing}").iloc[0, 0])
            if n:
                self._event(view.name, "null_keys", n=n, note=f"{n:,} row(s) without a key can't be followed")
        followed = sum(1 for s in state.stage_of.values() if s is not None)
        logger.info("sip %s: following %d key(s)", view.name, followed)

    def _sample(self, view: str, cols: List[str], where: str) -> pd.DataFrame:
        k = key_sql([ident(c) for c in cols])
        not_null = " AND ".join(f"{ident(c)} IS NOT NULL" for c in cols)
        return self.engine.query(f"SELECT *, {k} AS _sip_key, {hash_sql(k)} AS _sip_hash FROM {ident(view)} "
                                 f"WHERE {not_null} AND ({where})")

    def _picked(self, cols: List[str], cutoff: str) -> str:
        k = key_sql([ident(c) for c in cols])
        where = f"{hash_sql(k)} <= {literal(cutoff)}"
        if self.spec.watch:
            where += f" OR {in_list(k, self.spec.watch)}"
        return where

    def _root(self, view: View, cols: List[str]) -> StageState:
        df = self._sample(view.name, cols, self._picked(cols, self.spec.upper))
        cutoff = self.spec.upper
        watch = set(self.spec.watch)
        if not df.empty:
            hashes = sorted({h for k, h in zip(df["_sip_key"], df["_sip_hash"]) if str(k) not in watch})
            if len(hashes) > self.spec.max_rows:
                cutoff = hashes[self.spec.max_rows - 1]
                df = df[(df["_sip_hash"] <= cutoff) | df["_sip_key"].astype(str).isin(watch)]
        rows, counts = self._collect(df, cols)
        state = StageState(view.name, cols, hash_mode=view.key.mode == "row", cutoff=cutoff, rows=rows,
                           counts=counts, stage_of={k: k for k in rows})
        why = "" if view.key.root else f"(nothing followed from {view.key.upstream})"
        for k in rows:
            n = counts[k]
            self._event(view.name, "duplicated" if n > 1 else "seen", key=k, stage_key=k, n=n, row=rows[k],
                        note=why)
        return state

    def _row(self, view: View, cols: List[str], up: StageState) -> StageState:
        alive = {o: s for o, s in up.stage_of.items() if s is not None}
        k = key_sql([ident(c) for c in cols])
        if up.hash_mode:
            df = self._sample(view.name, cols, self._picked(cols, up.cutoff) + (
                f" OR {in_list(k, sorted(alive))}" if alive else ""))
        else:
            df = self._sample(view.name, cols, in_list(k, sorted(set(alive.values()))))
        rows, counts = self._collect(df, cols)
        state = StageState(view.name, cols, hash_mode=up.hash_mode, cutoff=up.cutoff, rows=rows, counts=counts)
        for origin, s in alive.items():
            if s in rows:
                state.stage_of[origin] = s
                n = counts[s]
                changed = self._changed(up.rows.get(s), rows[s])
                event = "duplicated" if n > 1 and up.counts.get(s, 1) <= 1 else ("changed" if changed else "seen")
                self._event(view.name, event, key=origin, stage_key=s, n=n, row=rows[s], changed=changed)
            else:
                state.stage_of[origin] = None
                self._event(view.name, "dropped", key=origin, stage_key=s, n=0,
                            note=f"in {up.view}, not in {view.name}")
        if up.hash_mode:  # keys under the cutoff that the step before didn't have
            for s in rows:
                if s not in alive:
                    state.stage_of[s] = s
                    self._event(view.name, "new", key=s, stage_key=s, n=counts[s], row=rows[s],
                                note=f"not in {up.view}")
        return state

    def trace_sql(self, view: View, up: StageState) -> str:
        """The view's own FROM/JOIN/WHERE, selecting each followed key of the step before and its group."""
        query = view.ast.copy()
        select = leftmost(query)
        alias = view.key.alias or up.view
        up_key = key_sql([f"{ident(alias)}.{ident(c)}" for c in up.columns])
        group_key = key_sql([g.sql(dialect="duckdb") for g in view.key.group_exprs])
        alive = sorted({s for s in up.stage_of.values() if s is not None})
        if up.hash_mode:
            cond = f"{hash_sql(up_key)} <= {literal(up.cutoff)}"
            extra = sorted(set(alive) | set(self.spec.watch))
            if extra:
                cond += f" OR {in_list(up_key, extra)}"
        else:
            cond = in_list(up_key, alive)
        select.set("expressions", [
            exp.alias_(sqlglot.parse_one(up_key, dialect="duckdb"), "_sip_up"),
            exp.alias_(sqlglot.parse_one(group_key, dialect="duckdb"), "_sip_group"),
        ])
        for arg in ("group", "having", "qualify", "order", "limit", "offset", "distinct"):
            select.set(arg, None)
        select.where(f"({cond})", append=True, dialect="duckdb", copy=False)
        if isinstance(query, exp.Select):
            for arg in ("order", "limit", "offset"):
                query.set(arg, None)
        return query.sql(dialect="duckdb")

    def _group(self, view: View, cols: List[str], up: StageState) -> StageState:
        mapping = self.engine.query(self.trace_sql(view, up))
        group_of: Dict[str, str] = {}
        for u, g in zip(mapping["_sip_up"], mapping["_sip_group"]):
            if u is not None and g is not None:
                group_of.setdefault(str(u), str(g))
        k = key_sql([ident(c) for c in cols])
        groups = sorted(set(group_of.values()))
        df = self.engine.query(f"SELECT *, {k} AS _sip_key FROM {ident(view.name)} WHERE {in_list(k, groups)}") \
            if groups else pd.DataFrame(columns=["_sip_key"])
        rows, counts = self._collect(df, cols)
        state = StageState(view.name, cols, hash_mode=False, cutoff=up.cutoff, rows=rows, counts=counts)
        for origin, s in up.stage_of.items():
            if s is None:
                continue
            g = group_of.get(s)
            if g is None:
                state.stage_of[origin] = None
                self._event(view.name, "dropped", key=origin, stage_key=None, n=0,
                            note=f"filtered out of {view.name} before grouping")
            elif g in rows:
                state.stage_of[origin] = g
                self._event(view.name, "grouped", key=origin, stage_key=g, n=counts[g], row=rows[g],
                            note=f"in group {g}")
            else:
                state.stage_of[origin] = None
                self._event(view.name, "dropped", key=origin, stage_key=g, n=0,
                            note=f"its group {g} isn't in {view.name} (HAVING / a filter after grouping)")
        return state

    def failed(self, view: Optional[str], error: BaseException) -> None:
        """The run stopped: recorded so the sip says where."""
        self._event(view or "", "failed", note=f"{error.__class__.__name__}: {error}"[:500])

    # -- results -------------------------------------------------------------

    def dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.events, columns=EVENT_COLUMNS)

    def save(self, store: Optional[str]) -> Optional[str]:
        """Writes this run's events as one Parquet file: ``{store}/{pipeline}/run_date=…/run-{run_id}.parquet``."""
        if not store or not self.events:
            return None
        try:
            self.saved_to = save_events(self.events, store, self.base["pipeline"], self.base["run_date"],
                                        self.base["run_id"])
            logger.info("sip: %d event(s) → %s", len(self.events), self.saved_to)
        except Exception as exc:  # noqa: BLE001
            msg = f"sip: couldn't write to {store}: {exc.__class__.__name__}: {exc}"
            logger.warning(msg)
            self.warnings.append(msg)
        return self.saved_to


def _filesystem(location: str):
    import pyarrow.fs as pafs

    if "://" not in location:
        location = os.path.abspath(os.path.expanduser(location))
        return pafs.LocalFileSystem(), location
    return pafs.FileSystem.from_uri(location)


def save_events(events: List[Dict[str, Any]], store: str, pipeline: str, run_date: str, run_id: str) -> str:
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema([(c, pa.int64() if c in ("position", "n") else pa.string()) for c in EVENT_COLUMNS])
    table = pa.Table.from_pylist([{c: e.get(c) for c in EVENT_COLUMNS} for e in events], schema=schema)
    fs, base = _filesystem(store)
    folder = f"{base.rstrip('/')}/{pipeline}/run_date={run_date}"
    fs.create_dir(folder, recursive=True)
    path = f"{folder}/run-{run_id}.parquet"
    pq.write_table(table, path, filesystem=fs)
    return path if "://" not in store else store.rstrip("/") + path[len(base.rstrip("/")):]


def read_sip(store: str, pipeline: Optional[str] = None, key: Optional[str] = None,
             run_id: Optional[str] = None) -> pd.DataFrame:
    """Every sip event kept in ``store`` (or one pipeline's), oldest run first; ``key`` / ``run_id`` narrow it."""
    import pyarrow.fs as pafs
    import pyarrow.parquet as pq

    fs, base = _filesystem(store)
    root = f"{base.rstrip('/')}/{pipeline}" if pipeline else base.rstrip("/")
    info = fs.get_file_info(root)
    if info.type == pafs.FileType.NotFound:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    files = sorted(i.path for i in fs.get_file_info(pafs.FileSelector(root, recursive=True))
                   if i.type == pafs.FileType.File and i.path.endswith(".parquet"))
    frames = [pq.read_table(f, filesystem=fs).to_pandas() for f in files]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=EVENT_COLUMNS)
    if key is not None:
        df = df[df["key"] == str(key)]
    if run_id is not None:
        df = df[df["run_id"] == run_id]
    return df.sort_values(["run_at", "run_id", "position"], kind="stable").reset_index(drop=True)

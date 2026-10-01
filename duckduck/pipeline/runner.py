"""Running a pipeline file: plan, run the views in order, follow the sip, write the targets."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import pandas as pd

from .analysis import Plan, _from_sources, build_plan, leftmost
from .engines import make_engine
from .lake import write_target
from .sip import Sip
from .sources import Statement, statements_of
from .sip import ident
from .sources import inject_where
from .spec import DURATION_RE, PipelineError, PipelineSpec, _shifted, load_spec, run_parameters, substitute
from .state import read_state, write_state

logger = logging.getLogger("duckduck.pipeline")


def _spec(source: Union[str, os.PathLike, Dict[str, Any], PipelineSpec]) -> PipelineSpec:
    return source if isinstance(source, PipelineSpec) else load_spec(source)


def _state(spec: PipelineSpec, quiet: bool = False) -> Dict[str, Any]:
    if not spec.state:
        return {}
    try:
        return read_state(spec.resolve(spec.state), spec.name)
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            raise PipelineError(f"couldn't read the pipeline's state at {spec.state}: {exc}") from None
        return {}


_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MOMENT = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?$")


def _literal(value: str) -> str:
    """A watermark as a SQL literal of its kind: a number bare, a date / timestamp typed, else text."""
    if _NUMBER.match(value):
        return value
    if _DATE.match(value):
        return f"DATE '{value}'"
    if _MOMENT.match(value):
        return f"TIMESTAMP '{value}'"
    return "'" + value.replace("'", "''") + "'"


def apply_load(spec: PipelineSpec, plan: Plan, state: Dict[str, Any]) -> Dict[str, Any]:
    """An incremental load: ``col > <its max at the last successful run>`` (several columns: any of them)
    ANDed into the WHERE of the step that reads the source, its text otherwise as written. No watermark yet
    (and no ``initial``) → the first run reads everything."""
    load = spec.load
    if not load.incremental:
        return {"type": "full"}
    step = load.step or next((n for n in plan.needed if plan.views[n].driving
                              and plan.views[n].driving not in plan.views), plan.needed[0])
    if step not in plan.views:
        raise PipelineError(f"load.step {step!r}: the SQL has no such step ({', '.join(plan.views)})")
    view = plan.views[step]
    select = leftmost(view.ast)
    sources = _from_sources(select) if select is not None else []
    alias = sources[0].alias if len(sources) > 1 and sources[0].alias else None
    marks = state.get("watermarks") or {}
    since: Dict[str, str] = {}
    conditions = []
    for col in load.columns:
        value = marks.get(col) or load.initial.get(col)
        if value is None:
            continue
        value = str(value)
        since[col] = value
        if load.lookback:
            m = DURATION_RE.match(load.lookback)
            try:
                value = _shifted(value, "-", m.group(1), m.group(2), col) if not _NUMBER.match(value) else value
            except PipelineError:
                pass  # not a date or a time: no lookback for it
        conditions.append(f"{ident(alias) + '.' if alias else ''}{ident(col)} > {_literal(value)}")
    where = None
    if conditions:
        where = conditions[0] if len(conditions) == 1 else "(" + " OR ".join(conditions) + ")"
        view.sql = inject_where(view.sql, where)
    plan.keep.add(step)
    return {"type": "incremental", "step": step, "columns": list(load.columns), "where": where, "since": since}


def plan_pipeline(source: Union[str, os.PathLike, Dict[str, Any], PipelineSpec],
                  params: Optional[Dict[str, Any]] = None, now: Optional[dt.datetime] = None,
                  duck: Any = None) -> Plan:
    """What a run would do — views in order, what each reads, the key's way, the targets — without running.
    ``duck``: the connectors (a WITH named like one of their tables stays inside its query)."""
    spec = _spec(source)
    values = run_parameters(spec, params, now=now, state=_state(spec, quiet=True))
    plan = build_plan(spec, [Statement(substitute(s.sql, values), s.origin) for s in statements_of(spec)],
                      reserved=getattr(duck, "functions", ()))
    plan.parameters = values
    plan.load = apply_load(spec, plan, _state(spec, quiet=True))
    return plan


@dataclass
class PipelineRun:
    pipeline: str
    run_id: str
    run_at: str
    engine: str
    plan: Plan
    steps: List[Dict[str, Any]] = field(default_factory=list)  # one per view run: name, seconds, rows, kept
    writes: List[Dict[str, Any]] = field(default_factory=list)
    sip: pd.DataFrame = field(default_factory=pd.DataFrame)
    sip_path: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    dry_run: bool = False
    seconds: float = 0.0
    load: Dict[str, Any] = field(default_factory=dict)  # full / incremental: the WHERE added, since → now
    state_path: Optional[str] = None

    def report(self) -> str:
        lines = [f"pipeline {self.pipeline} · run {self.run_id} · {self.engine}"
                 f"{' · dry run (nothing written)' if self.dry_run else ''} · {self.seconds:.1f}s"]
        for s in self.steps:
            rows = f", {s['rows']:,} rows" if s.get("rows") is not None else ""
            lines.append(f"  {s['view']}: {s['seconds']:.2f}s{rows}{' (kept)' if s['kept'] else ''}")
        for w in self.writes:
            extra = ", ".join(_fact(k, v) for k, v in w.items()
                              if k not in ("view", "target", "mode", "seconds", "table") and v)
            lines.append(f"  wrote {w['view']} → {w['target']} ({w['mode']}{', ' + extra if extra else ''}) "
                         f"in {w['seconds']:.2f}s")
        if len(self.sip):
            counts = self.sip["event"].value_counts()
            followed = self.sip.loc[self.sip["key"].notna(), "key"].nunique()
            lines.append(f"  sip: {followed} key(s) followed — " + ", ".join(f"{n} {e}" for e, n in counts.items())
                         + (f" → {self.sip_path}" if self.sip_path else ""))
        if self.load.get("type") == "incremental":
            cols = ", ".join(self.load["columns"])
            before, after = self.load.get("since") or {}, self.load.get("now") or {}
            read = f"read where {self.load['where']}" if self.load.get("where") else "first run: read everything"
            if before and after == before:
                moved = "nothing newer"
            else:
                moved = ", ".join(f"{c}: {before.get(c, '—')} → {v}" for c, v in after.items()) or "no rows"
            lines.append(f"  incremental on {cols}: {read} · {moved}")
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


def _max_of(engine: Any, view: str, column: str) -> Optional[str]:
    """``max(column)`` of the view as text (a timestamp without its 'T'), None when it has no rows."""
    try:
        value = engine.query(f"SELECT max({ident(column)}) AS w FROM {ident(view)}").iloc[0, 0]
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(f"incremental load: couldn't read max({column}) from {view} — is the column in its "
                            f"SELECT? ({exc})") from None
    if value is None or (isinstance(value, float) and value != value) or pd.isna(value):
        return None
    if hasattr(value, "item") and not isinstance(value, pd.Timestamp):
        value = value.item()
    return str(value)


def _fact(k: str, v: Any) -> str:
    if isinstance(v, bool):
        return k.replace("_", " ")
    if isinstance(v, int):
        return f"{k.replace('_', ' ')} {v:,}"
    if isinstance(v, (list, tuple)):
        return f"{k.replace('_', ' ')}: {', '.join(map(str, v))}"
    return f"{k.replace('_', ' ')} {v}"


def _duck(duck: Any, config_path: Optional[str]) -> Any:
    if duck is not None:
        return duck
    from ..core import DuckAPI

    duck = DuckAPI()
    if config_path or os.environ.get("DUCKDUCK_CONFIG") or duck._find_default_config_file():
        duck.auto_register(config_path=config_path, on_error="warn")
    return duck


def catalog_lookup(spec: PipelineSpec, duck: Any, given: Optional[Dict[str, Any]] = None):
    """``name → catalog``: ones passed in code, then the pipeline file's ``catalogs``, then duckduck.json's — built
    once, on first use."""
    built: Dict[str, Any] = dict(given or {})

    def of(name: str) -> Any:
        if name in built:
            return built[name]
        blocks = dict(spec.catalogs)
        config = getattr(duck, "_config_path", None)
        if name not in blocks and config and os.path.isfile(config):
            with open(config, encoding="utf-8") as f:
                blocks = {**(json.load(f).get("catalogs") or {}), **blocks}
        if name not in blocks:
            known = sorted(set(built) | set(blocks))
            raise PipelineError(f"no catalog {name!r} — define it under \"catalogs\" in the pipeline file or in "
                                f"duckduck.json (known: {', '.join(known) or 'none'})")
        from .catalogs import make_catalog

        built[name] = make_catalog(name, blocks[name], duck=duck)
        return built[name]
    return of


def run_pipeline(source: Union[str, os.PathLike, Dict[str, Any], PipelineSpec], duck: Any = None, spark: Any = None,
                 config_path: Optional[str] = None, params: Optional[Dict[str, Any]] = None,
                 run_id: Optional[str] = None, dry_run: bool = False, now: Optional[dt.datetime] = None,
                 engine: Any = None, catalogs: Optional[Dict[str, Any]] = None) -> PipelineRun:
    """
    Runs a pipeline file. ``duck``: the DuckAPI its SQL reads through (default:
    one ``auto_register``ed from ``config_path`` / ``DUCKDUCK_CONFIG`` /
    ``duckduck.json``); ``spark``: the session for ``"engine": "spark"``;
    ``params``: overrides of its ``parameters``; ``dry_run``: runs the views
    and the sip, writes nothing (the sip isn't stored either).
    """
    started = time.perf_counter()
    spec = _spec(source)
    now = now or dt.datetime.now(dt.timezone.utc)
    state = _state(spec)
    values = run_parameters(spec, params, now=now, run_id=run_id, state=state)
    statements = [Statement(substitute(s.sql, values), s.origin) for s in statements_of(spec)]
    if duck is None and engine is not None:
        duck = getattr(engine, "duck", None)
    if duck is None:
        duck = _duck(None, config_path)
    plan = build_plan(spec, statements, reserved=getattr(duck, "functions", ()))
    if params and "watermark" in params and spec.load.columns:  # read from a given point: a backfill / re-read
        state = {**state, "watermarks": {**(state.get("watermarks") or {}), spec.load.columns[0]: str(params["watermark"])}}
    run_load = apply_load(spec, plan, state)
    if engine is None:
        engine = make_engine(spec.engine, duck=duck, spark=spark)
    catalog_of = catalog_lookup(spec, duck, catalogs)
    run = PipelineRun(pipeline=spec.name, run_id=values["run_id"], run_at=values["run_at"], engine=engine.name,
                      plan=plan, dry_run=dry_run)
    # the sip's run_at keeps the microseconds: two runs in the same second still sort in the order they ran
    sip = Sip(spec.sip, engine, spec.name, run.run_id, now.isoformat(sep=" "), values["run_date"]) \
        if spec.sip.enabled else None
    logger.info("pipeline %s · run %s · %d view(s)%s", spec.name, run.run_id, len(plan.needed),
                f" · sip on {len(plan.sampled)}" if sip else "")
    current: Optional[str] = None
    try:
        for name in plan.needed:
            current = name
            view = plan.views[name]
            t0 = time.perf_counter()
            keep = name in plan.keep
            engine.define(name, view.sql, keep)
            step = {"view": name, "kept": keep, "rows": engine.rows(name) if keep else None,
                    "seconds": time.perf_counter() - t0}
            run.steps.append(step)
            logger.info("  %s: %.2fs%s", name, step["seconds"],
                        f", {step['rows']:,} rows" if step["rows"] is not None else "")
            if sip and name in plan.sampled:
                sip.observe(view)
            for target in plan.targets:
                if target.view != name or dry_run:
                    continue
                t1 = time.perf_counter()
                result = write_target(engine, target, plan.key_of(target), run.run_id, catalog_of)
                run.writes.append({"view": name, "target": target.where, "mode": target.mode, **result,
                                   "seconds": round(time.perf_counter() - t1, 2)})
                logger.info("  wrote %s → %s (%s)", name, target.where, target.mode)
        current = None
        marks = dict(state.get("watermarks") or {})
        if run_load["type"] == "incremental":
            now_marks = {}
            for col in spec.load.columns:  # how far this run read: each column's max in the step it filtered
                top = _max_of(engine, run_load["step"], col)
                if top is not None:
                    now_marks[col] = top
            run_load["now"] = {**{c: v for c, v in marks.items() if c in spec.load.columns}, **now_marks}
            marks.update(now_marks)
        run.load = run_load
        if spec.state and not dry_run:
            run.state_path = write_state(spec.resolve(spec.state), spec.name, {
                "pipeline": spec.name, "last_run_id": run.run_id, "last_success_at": run.run_at,
                "load": spec.load.type, "watermarks": marks,
                "previous_watermarks": state.get("watermarks") or {},
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(sep=" ")})
    except BaseException as exc:
        if sip is not None:
            sip.failed(current, exc)
            if not dry_run:
                sip.save(spec.sip.store and spec.resolve(spec.sip.store))
        raise
    finally:
        try:
            engine.close()
        except Exception:  # noqa: BLE001
            pass
    if sip is not None:
        if not dry_run:
            if spec.sip.store:
                run.sip_path = sip.save(spec.resolve(spec.sip.store))
            else:
                sip.warnings.append("no sip.store: the sip of this run is only in PipelineRun.sip")
        run.sip = sip.dataframe()
        run.warnings.extend(sip.warnings)
    run.seconds = time.perf_counter() - started
    return run


__all__ = ["PipelineError", "PipelineRun", "plan_pipeline", "run_pipeline"]

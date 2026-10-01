"""Running a pipeline file: plan, run the views in order, follow the sip, write the targets."""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import pandas as pd

from .analysis import Plan, build_plan
from .engines import make_engine
from .sip import Sip
from .sources import Statement, statements_of
from .spec import PipelineError, PipelineSpec, load_spec, run_parameters, substitute

logger = logging.getLogger("duckduck.pipeline")


def _spec(source: Union[str, os.PathLike, Dict[str, Any], PipelineSpec]) -> PipelineSpec:
    return source if isinstance(source, PipelineSpec) else load_spec(source)


def plan_pipeline(source: Union[str, os.PathLike, Dict[str, Any], PipelineSpec],
                  params: Optional[Dict[str, Any]] = None, now: Optional[dt.datetime] = None) -> Plan:
    """What a run would do — views in order, what each reads, the key's way, the targets — without running."""
    spec = _spec(source)
    values = run_parameters(spec, params, now=now)
    return build_plan(spec, [Statement(substitute(s.sql, values), s.origin) for s in statements_of(spec)])


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

    def report(self) -> str:
        lines = [f"pipeline {self.pipeline} · run {self.run_id} · {self.engine}"
                 f"{' · dry run (nothing written)' if self.dry_run else ''} · {self.seconds:.1f}s"]
        for s in self.steps:
            rows = f", {s['rows']:,} rows" if s.get("rows") is not None else ""
            lines.append(f"  {s['view']}: {s['seconds']:.2f}s{rows}{' (kept)' if s['kept'] else ''}")
        for w in self.writes:
            extra = ", ".join(f"{k} {v:,}" if isinstance(v, int) and not isinstance(v, bool) else k
                              for k, v in w.items() if k not in ("view", "target", "mode", "seconds") and v)
            lines.append(f"  wrote {w['view']} → {w['target']} ({w['mode']}{', ' + extra if extra else ''}) "
                         f"in {w['seconds']:.2f}s")
        if len(self.sip):
            counts = self.sip["event"].value_counts()
            followed = self.sip.loc[self.sip["key"].notna(), "key"].nunique()
            lines.append(f"  sip: {followed} key(s) followed — " + ", ".join(f"{n} {e}" for e, n in counts.items())
                         + (f" → {self.sip_path}" if self.sip_path else ""))
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


def _duck(duck: Any, config_path: Optional[str]) -> Any:
    if duck is not None:
        return duck
    from ..core import DuckAPI

    duck = DuckAPI()
    if config_path or os.environ.get("DUCKDUCK_CONFIG") or duck._find_default_config_file():
        duck.auto_register(config_path=config_path, on_error="warn")
    return duck


def run_pipeline(source: Union[str, os.PathLike, Dict[str, Any], PipelineSpec], duck: Any = None, spark: Any = None,
                 config_path: Optional[str] = None, params: Optional[Dict[str, Any]] = None,
                 run_id: Optional[str] = None, dry_run: bool = False, now: Optional[dt.datetime] = None,
                 engine: Any = None) -> PipelineRun:
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
    values = run_parameters(spec, params, now=now, run_id=run_id)
    plan = build_plan(spec, [Statement(substitute(s.sql, values), s.origin) for s in statements_of(spec)])
    if engine is None:
        engine = make_engine(spec.engine, duck=_duck(duck, config_path), spark=spark)
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
                result = engine.write(target, plan.key_of(target), run.run_id)
                run.writes.append({"view": name, "target": target.where, "mode": target.mode, **result,
                                   "seconds": round(time.perf_counter() - t1, 2)})
                logger.info("  wrote %s → %s (%s)", name, target.where, target.mode)
        current = None
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

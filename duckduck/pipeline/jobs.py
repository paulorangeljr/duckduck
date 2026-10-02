"""
Several ingestions in one file, grouped by domain — and a class to run them.

A domain file:

    {
      "domain": "inventory",
      "layer": "raw",
      "load": "full",
      "target": {"mode": "append", "partition_by": "_load_date"},
      "jobs": {
        "assets": {"primary_key": "hostname", "sql": "SELECT * FROM assets"},
        "owners": {"primary_key": "hostname", "sql": "SELECT * FROM owners"}
      }
    }

Every key next to ``jobs`` is a default each job can override (``target``,
``sip``, ``parameters``, ``keys``, ``catalogs``, ``aws`` and an object ``load``
merge key by key). A job writing to a layer gets ``database`` = the domain
and ``table_name`` = its own name unless it says otherwise, and its pipeline
name — its state file, its sip folder — is ``{layer}_{domain}_{job}``
(``raw_inventory_assets``), so the same domain in two layers never shares
state. A plain pipeline file (``"pipeline": …``) is a domain of one job.

    from duckduck.pipeline import Pipelines

    pipelines = Pipelines(config="duckduck.json")
    raw = pipelines.domain("raw_inventory.json")
    raw.run()                 # every job, in the file's order
    raw.run("assets")         # one
    raw.plan("owners")        # what it would do
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, List, Optional, Union

import pandas as pd

from .analysis import Plan
from .runner import PipelineRun, plan_pipeline, run_pipeline
from .spec import NAME_RE, PipelineError, PipelineSpec, load_spec

DOMAIN_KEYS = {"domain", "description", "jobs"}
_MERGED = ("target", "sip", "parameters", "keys", "catalogs", "aws", "load")


def _merge(defaults: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(defaults)
    for k, v in job.items():
        if k in _MERGED and isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = copy.deepcopy(v)
    return out


class Domain:
    """The jobs of one file (``domain`` + ``jobs``, or a plain pipeline file as one job)."""

    def __init__(self, source: Union[str, os.PathLike, Dict[str, Any]], pipelines: Optional["Pipelines"] = None,
                 base_dir: Optional[str] = None):
        if isinstance(source, dict):
            data, self.file = source, None
            self.base_dir = base_dir or os.getcwd()
        else:
            self.file = os.path.abspath(os.fspath(source))
            try:
                with open(self.file, encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                raise PipelineError(f"no pipeline file at {self.file}") from None
            except json.JSONDecodeError as exc:
                raise PipelineError(f"{self.file} isn't valid JSON: {exc}") from None
            self.base_dir = base_dir or os.path.dirname(self.file)
        if not isinstance(data, dict):
            raise PipelineError("a pipeline file is a JSON object")
        self.pipelines = pipelines or Pipelines()
        self.description = data.get("description")
        self._specs: Dict[str, PipelineSpec] = {}
        if "jobs" not in data:  # a plain pipeline file: one job, named after its pipeline
            spec = load_spec(data, base_dir=self.base_dir)
            self.name = None
            self._specs[spec.name] = spec
            return
        name = data.get("domain")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise PipelineError("'domain' names the group of jobs: letters, digits and _ (e.g. \"inventory\")")
        if "pipeline" in data:
            raise PipelineError("a domain file names its jobs under \"jobs\" — not \"pipeline\"")
        jobs = data["jobs"]
        if not isinstance(jobs, dict) or not jobs:
            raise PipelineError("'jobs' is an object: {\"assets\": {\"sql\": …}, \"owners\": {…}}")
        self.name = name
        defaults = {k: v for k, v in data.items() if k not in DOMAIN_KEYS}
        for job, raw in jobs.items():
            if not NAME_RE.match(str(job)):
                raise PipelineError(f"job {job!r}: a job's name is letters, digits and _")
            if not isinstance(raw, dict):
                raise PipelineError(f"job {job!r} is an object: {{\"sql\": …, \"primary_key\": …}}")
            merged = _merge(defaults, raw)
            target = merged.get("target")
            layer = (target.get("layer") if isinstance(target, dict) else None) or merged.get("layer")
            if layer and isinstance(target, dict) and not target.get("path"):
                target.setdefault("database", name)
                target.setdefault("table_name", job)
            elif layer and target is None and "targets" not in merged:
                merged["target"] = {"database": name, "table_name": job}
            merged.setdefault("pipeline", "_".join(p for p in (layer, name, job) if p))
            try:
                self._specs[job] = load_spec(merged, base_dir=self.base_dir)
            except PipelineError as exc:
                raise PipelineError(f"job {job!r} of {self.file or 'the domain'}: {exc}") from None

    @property
    def jobs(self) -> List[str]:
        return list(self._specs)

    def spec(self, job: str) -> PipelineSpec:
        if job not in self._specs:
            raise PipelineError(f"no job {job!r} here (jobs: {', '.join(self._specs)})")
        return self._specs[job]

    def _chosen(self, jobs) -> List[str]:
        return list(jobs) or self.jobs

    def run(self, *jobs: str, params: Optional[Dict[str, Any]] = None, dry_run: bool = False,
            run_id: Optional[str] = None) -> "Runs":
        """Runs the jobs named (every one when none is, in the file's order), one after the other."""
        return Runs(self.pipelines.run(self.spec(j), params=params, dry_run=dry_run, run_id=run_id)
                    for j in self._chosen(jobs))

    def plan(self, job: Optional[str] = None, params: Optional[Dict[str, Any]] = None) -> Union[Plan, List[Plan]]:
        """What a job would do (every job's, as a list, when none is named) — nothing runs."""
        if job is not None:
            return self.pipelines.plan(self.spec(job), params=params)
        return [self.pipelines.plan(self.spec(j), params=params) for j in self.jobs]

    def __repr__(self) -> str:
        return f"Domain({self.name or self.jobs[0]!r}, jobs={self.jobs})"


class Runs(list):
    """The runs of ``Domain.run`` — a list of ``PipelineRun``, with one report for them all."""

    def report(self) -> str:
        return "\n".join(r.report() for r in self)


class Pipelines:
    """
    Runs pipeline jobs with one configuration — the connectors the SQL reads
    and the ``lake`` settings (layers, catalog, state, sip store, AWS
    account) of ``config`` (default: ``DUCKDUCK_CONFIG`` / ``duckduck.json``).

    Each job gets the connectors as the lake is *now* (a job reads what the
    one before it wrote), unless ``duck`` is given, which every job then
    shares. ``spark``: the session for ``"engine": "spark"`` jobs.
    """

    def __init__(self, config: Optional[str] = None, duck: Any = None, spark: Any = None,
                 params: Optional[Dict[str, Any]] = None):
        self.config = os.path.abspath(config) if config else None
        self.given_duck = duck
        self.spark = spark
        self.params = dict(params or {})

    # -- jobs -------------------------------------------------------------------------------------------------------

    def domain(self, file: Union[str, os.PathLike, Dict[str, Any]], base_dir: Optional[str] = None) -> Domain:
        """The jobs of a file (a domain of several, or a plain pipeline file)."""
        return Domain(file, self, base_dir=base_dir)

    def run(self, source: Union[str, os.PathLike, Dict[str, Any], PipelineSpec], job: Optional[str] = None,
            params: Optional[Dict[str, Any]] = None, dry_run: bool = False,
            run_id: Optional[str] = None) -> PipelineRun:
        """One job — a spec, a plain pipeline file, or ``job`` of a domain file."""
        spec = self._spec_of(source, job)
        return run_pipeline(spec, duck=self.connect(), spark=self.spark, config_path=self.config,
                            params={**self.params, **(params or {})}, dry_run=dry_run, run_id=run_id)

    def plan(self, source: Union[str, os.PathLike, Dict[str, Any], PipelineSpec], job: Optional[str] = None,
             params: Optional[Dict[str, Any]] = None) -> Plan:
        spec = self._spec_of(source, job)
        return plan_pipeline(spec, params={**self.params, **(params or {})}, duck=self.given_duck,
                             config_path=self.config)

    def _spec_of(self, source: Any, job: Optional[str]) -> PipelineSpec:
        if isinstance(source, PipelineSpec):
            return source
        domain = self.domain(source)
        if job is None:
            if len(domain.jobs) > 1:
                raise PipelineError(f"{domain.file or 'the domain'} has {len(domain.jobs)} jobs "
                                    f"({', '.join(domain.jobs)}) — name one, or use .domain(file).run()")
            job = domain.jobs[0]
        return domain.spec(job)

    # -- the connectors, the lake -----------------------------------------------------------------------------------

    def connect(self) -> Any:
        """The DuckAPI a job reads through: the one given, else a fresh one from the config."""
        if self.given_duck is not None:
            return self.given_duck
        from .runner import _duck

        self._local_lake_folders()
        return _duck(None, self.config)  # a connector that fails to start is a warning: other jobs may not need it

    def lake(self) -> Dict[str, Any]:
        from .settings import lake_settings

        return lake_settings(self.given_duck, self.config)

    def _local_lake_folders(self) -> None:
        """A ``files`` connector over a local lake folder that nothing wrote yet would fail to start: the folder
        is created (empty) first — only inside a local layer of ``lake.layers``."""
        from .settings import _find_config

        path = self.config or os.environ.get("DUCKDUCK_CONFIG") or _find_config()
        if not path or not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        base = os.path.dirname(os.path.abspath(path))
        layers = [os.path.normpath(os.path.join(base, p)) for p in ((config.get("lake") or {}).get("layers") or {})
                  .values() if isinstance(p, str) and "://" not in p]
        for svc in (config.get("services") or {}).values():
            if not isinstance(svc, dict) or svc.get("connector") != "files" or not isinstance(svc.get("path"), str):
                continue
            folder = os.path.normpath(os.path.join(base, svc["path"]))
            if any(folder == layer or folder.startswith(layer + os.sep) for layer in layers):
                os.makedirs(folder, exist_ok=True)

    def sip(self, key: Optional[str] = None, pipeline: Optional[str] = None,
            run_id: Optional[str] = None) -> pd.DataFrame:
        """The sip kept in the lake's ``sip_store`` — one key's way through every job, say."""
        from .sip import read_sip

        lake = self.lake()
        if not lake.get("sip_store"):
            raise PipelineError("no \"sip_store\" in the config's \"lake\" — read_sip(store, …) takes one")
        return read_sip(lake["sip_store"], pipeline=pipeline, key=key, run_id=run_id, aws=lake.get("aws"))

    def __repr__(self) -> str:
        return f"Pipelines(config={self.config!r})"


__all__ = ["Domain", "Pipelines", "Runs"]

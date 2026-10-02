"""
Several tables in one file, grouped by domain — and a class to run them.

A domain file:

    {
      "domain": "inventory",
      "domain_description": "Hosts and who owns them.",
      "layer": "raw",
      "sip": {"rate": 0.02, "max_rows": 50},
      "tables": {
        "assets": {"description": "The assets API.", "primary_key": "hostname", "sql": "SELECT * FROM assets",
                   "load": "full", "target": {"mode": "append", "partition_by": "_load_date"}},
        "owners": {"primary_key": "ip", "sql": "SELECT * FROM owners", "load": "full",
                   "target": {"mode": "overwrite"}, "sip": {"watch": ["10.0.0.1"]}}
      }
    }

Each table is a whole pipeline of its own — load, target, key, its own
``description``, everything. Only ``domain`` (and ``domain_description``),
``layer`` and ``sip`` may sit next to ``tables``, and a table can set any of
them itself (its ``sip`` merged key by key over the file's, ``"sip": false``
turns it off; a table with its own target ``path`` stays out of the file's
layer). A table in a layer is written to ``database`` = its domain and
``table_name`` = its own name unless its target says otherwise, and its
pipeline name — its state file, its sip folder — is
``{layer}_{domain}_{table}`` (``raw_inventory_assets``), so the same domain
in two layers never shares state. A plain pipeline file (``"pipeline": …``)
is a domain of one table.

    from duckduck.pipeline import Pipelines

    pipelines = Pipelines(config="duckduck.json")
    raw = pipelines.domain("raw_inventory.json")
    raw.run()                 # every table, in the file's order
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

DOMAIN_KEYS = {"domain", "domain_description", "layer", "sip", "tables"}  # all a domain file shares


def _sip(shared: Any, own: Any) -> Any:
    """The table's sip over the file's: key by key when both are objects; else the table's own when it has one."""
    if own is None:
        return copy.deepcopy(shared)
    if isinstance(shared, dict) and isinstance(own, dict):
        return {**shared, **own}
    if own is True and isinstance(shared, dict):
        return copy.deepcopy(shared)
    return copy.deepcopy(own)


class Domain:
    """The tables of one file (``domain`` + ``tables``, or a plain pipeline file as one table)."""

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
        self.description = data.get("domain_description") if "tables" in data else data.get("description")
        self._specs: Dict[str, PipelineSpec] = {}
        if "tables" not in data:  # a plain pipeline file: one table, named after its pipeline
            spec = load_spec(data, base_dir=self.base_dir)
            self.name = None
            self._specs[spec.name] = spec
            return
        name = data.get("domain")
        if name is not None and (not isinstance(name, str) or not NAME_RE.match(name)):
            raise PipelineError("'domain' names the group of tables: letters, digits and _ (e.g. \"inventory\")")
        if "pipeline" in data:
            raise PipelineError("a domain file names its tables under \"tables\" — not \"pipeline\"")
        if "description" in data:
            raise PipelineError("the domain's own description is \"domain_description\" — \"description\" belongs "
                                "to each table")
        shared = sorted(set(data) - DOMAIN_KEYS)
        if shared:
            raise PipelineError(f"{shared} go inside each table — a domain file shares only \"domain\", \"layer\" "
                                "and \"sip\" (and a table can set those too)")
        tables = data["tables"]
        if not isinstance(tables, dict) or not tables:
            raise PipelineError("'tables' is an object: {\"assets\": {\"sql\": …}, \"owners\": {…}}")
        self.name = name
        for table, raw in tables.items():
            if not NAME_RE.match(str(table)):
                raise PipelineError(f"table {table!r}: a table's name is letters, digits and _")
            if not isinstance(raw, dict):
                raise PipelineError(f"table {table!r} is an object: {{\"sql\": …, \"primary_key\": …}}")
            self._specs[table] = self._table(table, raw, data)

    def _table(self, table: str, raw: Dict[str, Any], data: Dict[str, Any]) -> PipelineSpec:
        merged = copy.deepcopy(raw)
        domain = merged.pop("domain", self.name)
        if domain is not None and (not isinstance(domain, str) or not NAME_RE.match(domain)):
            raise PipelineError(f"table {table!r}: 'domain' is letters, digits and _")
        target = merged.get("target")
        own_path = isinstance(target, str) or (isinstance(target, dict) and target.get("path")) or "targets" in merged
        if "layer" not in merged and data.get("layer") is not None and not own_path:
            merged["layer"] = data["layer"]  # a table written to its own path keeps out of the layers
        sip = _sip(data.get("sip"), merged.get("sip"))
        if sip is not None:
            merged["sip"] = sip
        layer = (target.get("layer") if isinstance(target, dict) else None) or merged.get("layer")
        if layer and isinstance(target, dict) and not target.get("path"):
            if domain:
                target.setdefault("database", domain)
            target.setdefault("table_name", table)
        elif layer and domain and target is None and "targets" not in merged:
            merged["target"] = {"database": domain, "table_name": table}
        merged.setdefault("pipeline", "_".join(p for p in (layer, domain, table) if p))
        try:
            return load_spec(merged, base_dir=self.base_dir)
        except PipelineError as exc:
            raise PipelineError(f"table {table!r} of {self.file or 'the domain'}: {exc}") from None

    @property
    def tables(self) -> List[str]:
        return list(self._specs)

    def spec(self, table: str) -> PipelineSpec:
        if table not in self._specs:
            raise PipelineError(f"no table {table!r} here (tables: {', '.join(self._specs)})")
        return self._specs[table]

    def run(self, *tables: str, params: Optional[Dict[str, Any]] = None, dry_run: bool = False,
            run_id: Optional[str] = None) -> "Runs":
        """Runs the tables named (every one when none is, in the file's order), one after the other."""
        return Runs(self.pipelines.run(self.spec(t), params=params, dry_run=dry_run, run_id=run_id)
                    for t in (list(tables) or self.tables))

    def plan(self, table: Optional[str] = None, params: Optional[Dict[str, Any]] = None) -> Union[Plan, List[Plan]]:
        """What a table's run would do (every table's, as a list, when none is named) — nothing runs."""
        if table is not None:
            return self.pipelines.plan(self.spec(table), params=params)
        return [self.pipelines.plan(self.spec(t), params=params) for t in self.tables]

    def __repr__(self) -> str:
        return f"Domain({self.name or self.tables[0]!r}, tables={self.tables})"


class Runs(list):
    """The runs of ``Domain.run`` — a list of ``PipelineRun``, with one report for them all."""

    def report(self) -> str:
        return "\n".join(r.report() for r in self)


class Pipelines:
    """
    Runs pipeline tables with one configuration — the connectors the SQL reads
    and the ``lake`` settings (layers, catalog, state, sip store, AWS
    account) of ``config`` (default: ``DUCKDUCK_CONFIG`` / ``duckduck.json``).

    Each run gets the connectors as the lake is *now* (a table reads what the
    one before it wrote), unless ``duck`` is given, which every run then
    shares. ``spark``: the session for ``"engine": "spark"`` tables.
    """

    def __init__(self, config: Optional[str] = None, duck: Any = None, spark: Any = None,
                 params: Optional[Dict[str, Any]] = None):
        self.config = os.path.abspath(config) if config else None
        self.given_duck = duck
        self.spark = spark
        self.params = dict(params or {})

    # -- tables -----------------------------------------------------------------------------------------------------

    def domain(self, file: Union[str, os.PathLike, Dict[str, Any]], base_dir: Optional[str] = None) -> Domain:
        """The tables of a file (a domain of several, or a plain pipeline file)."""
        return Domain(file, self, base_dir=base_dir)

    def run(self, source: Union[str, os.PathLike, Dict[str, Any], PipelineSpec], table: Optional[str] = None,
            params: Optional[Dict[str, Any]] = None, dry_run: bool = False,
            run_id: Optional[str] = None) -> PipelineRun:
        """One table — a spec, a plain pipeline file, or ``table`` of a domain file."""
        spec = self._spec_of(source, table)
        return run_pipeline(spec, duck=self.connect(), spark=self.spark, config_path=self.config,
                            params={**self.params, **(params or {})}, dry_run=dry_run, run_id=run_id)

    def plan(self, source: Union[str, os.PathLike, Dict[str, Any], PipelineSpec], table: Optional[str] = None,
             params: Optional[Dict[str, Any]] = None) -> Plan:
        spec = self._spec_of(source, table)
        return plan_pipeline(spec, params={**self.params, **(params or {})}, duck=self.given_duck,
                             config_path=self.config)

    def _spec_of(self, source: Any, table: Optional[str]) -> PipelineSpec:
        if isinstance(source, PipelineSpec):
            return source
        domain = self.domain(source)
        if table is None:
            if len(domain.tables) > 1:
                raise PipelineError(f"{domain.file or 'the domain'} has {len(domain.tables)} tables "
                                    f"({', '.join(domain.tables)}) — name one, or use .domain(file).run()")
            table = domain.tables[0]
        return domain.spec(table)

    # -- the connectors, the lake -----------------------------------------------------------------------------------

    def connect(self) -> Any:
        """The DuckAPI a run reads through: the one given, else a fresh one from the config."""
        if self.given_duck is not None:
            return self.given_duck
        from .runner import _duck

        self._local_lake_folders()
        return _duck(None, self.config)  # a connector that fails to start is a warning: other tables may not need it

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
        """The sip kept in the lake's ``sip_store`` — one key's way through every table, say."""
        from .sip import read_sip

        lake = self.lake()
        if not lake.get("sip_store"):
            raise PipelineError("no \"sip_store\" in the config's \"lake\" — read_sip(store, …) takes one")
        return read_sip(lake["sip_store"], pipeline=pipeline, key=key, run_id=run_id, aws=lake.get("aws"))

    def __repr__(self) -> str:
        return f"Pipelines(config={self.config!r})"


__all__ = ["Domain", "Pipelines", "Runs"]

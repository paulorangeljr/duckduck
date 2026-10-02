"""
duckduck's own state as tables — ``duckduck.services``, ``duckduck.tables``,
``duckduck.saved_tables``, ``duckduck.settings``, ``duckduck.pipeline_runs``.

Read-only listings of what this process knows: the connectors in the config
and whether they started, every registered table, the saved tables, the
configuration (secrets masked, by the same rule as the web app's Config tab)
and each pipeline's last run (the ``lake.state`` folder). Registered by
``DuckAPI.auto_register`` (``"system_tables": false`` in the config turns
them off) or by hand with ``DuckAPI.register_system_tables()``.

Nothing here ever returns a secret: a value whose key reads like one
(password, token, api key, connection string…) is ``***``, and
``"$secret.<key>"`` references stay (they say where a secret is, not what).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd

from .kinds import catalog
from .sparkplan import spark_plan

SERVICE = "duckduck"  # the service name, the table prefix and the address: duckduck.services
CONFIG_KEY = "system_tables"  # top-level config key: false turns these tables off

MASK = "***"
_SECRETISH = re.compile(r"pass(word|wd)?|secret|token|api[-_]?key|private[-_]?key|connection[-_]?string|"
                        r"\bsas\b|signature|credential", re.I)
_NOT_SECRET_SUFFIX = ("_id", "_env", "_file", "_path", "_url", "_name", "_type")

_WHY = "duckduck's own state: a small listing read in this process"


def is_secret_key(key: Any) -> bool:
    key = str(key)
    return bool(_SECRETISH.search(key)) and not key.lower().endswith(_NOT_SECRET_SUFFIX)


def mask(data: Any) -> Any:
    """Secret values (by key name) → ``"***"``; ``"$secret.<key>"`` references stay (they aren't secrets)."""
    if isinstance(data, dict):
        return {k: (MASK if is_secret_key(k) and isinstance(v, str) and v and not v.startswith("$secret.")
                    else mask(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [mask(v) for v in data]
    return data


def _flatten(value: Any, path: str = "") -> Iterator[Tuple[str, Any]]:
    if isinstance(value, dict) and value:
        for k, v in value.items():
            yield from _flatten(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list) and value and any(isinstance(v, (dict, list)) for v in value):
        for i, v in enumerate(value):
            yield from _flatten(v, f"{path}[{i}]")
    else:
        yield path, value


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "text"


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def _frame(rows: List[Dict[str, Any]], columns: List[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns).astype(object)


class SystemTables:
    """The tables, bound to one ``DuckAPI`` — they read it (and its config file) on every query."""

    def __init__(self, duck: Any):
        self.duck = duck

    # -- the configuration ------------------------------------------------------------------------------------------

    def _config(self) -> Dict[str, Any]:
        """The config file as it is now (a save from the web app shows at once), else what was given in code."""
        path = getattr(self.duck, "_config_path", None)
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
        return {"services": dict(getattr(self.duck, "configured_services", None) or {}),
                "views": dict(self.duck.views or {})}

    # -- tables ------------------------------------------------------------------------------------------------------

    @catalog
    @spark_plan("driver", why=_WHY)
    def services(self, name: Optional[str] = None, connector: Optional[str] = None,
                 started: Optional[bool] = None) -> pd.DataFrame:
        """Each connector of the config: whether it started (and why not), its tables, how it signs in."""
        config = self._config().get("services") or {}
        failed = self.duck.failed_services
        counts: Dict[str, int] = {}
        for svc in self.duck.service_of.values():
            counts[svc] = counts.get(svc, 0) + 1
        names = list(config) + sorted(n for n in set(counts) | set(failed)
                                      if n not in config and n not in (SERVICE, "saved tables", "taken over"))
        rows = []
        for svc in names:
            conf = config.get(svc) if isinstance(config.get(svc), dict) else {}
            auth = conf.get("authentication") if isinstance(conf.get("authentication"), dict) else {}
            f = failed.get(svc)
            options = {k: v for k, v in conf.items() if k not in ("connector", "authentication", "table_prefix")}
            rows.append({
                "name": svc,
                "connector": (f or {}).get("connector") or conf.get("connector") or svc,
                "started": f is None,
                "error": (f or {}).get("error"),
                "tables": counts.get(svc, 0),
                "table_prefix": self.duck.service_prefix.get(svc, conf.get("table_prefix", svc)),
                "auth_type": auth.get("type", "local") if auth else None,
                "secret_id": auth.get("secret_id"),
                "options": _text(mask(options)) if options else None,
            })
        df = _frame(rows, ["name", "connector", "started", "error", "tables", "table_prefix", "auth_type",
                           "secret_id", "options"])
        if name is not None:
            df = df[df["name"] == name]
        if connector is not None:
            df = df[df["connector"] == connector]
        if started is not None:
            df = df[df["started"] == bool(started)]
        return df.reset_index(drop=True)

    @catalog
    @spark_plan("driver", why=_WHY)
    def tables(self, service: Optional[str] = None, kind: Optional[str] = None) -> pd.DataFrame:
        """Every registered table: its service, kind, address, how to call it and what it pushes down."""
        from .addresses import address_of

        df = self.duck.list_tables()
        services = [self.duck.service_of.get(str(n).lower()) for n in df["name"]]
        addresses = []
        for n in df["name"]:
            try:
                addresses.append(address_of(self.duck, n))
            except Exception:  # noqa: BLE001 — an address is a nicety, never a failure
                addresses.append(None)
        df.insert(1, "service", services)
        df.insert(2, "address", addresses)
        df = df.astype(object)
        if service is not None:
            df = df[df["service"] == service]
        if kind is not None:
            df = df[df["kind"] == kind]
        return df.reset_index(drop=True)

    @catalog
    @spark_plan("driver", why=_WHY)
    def saved_tables(self, name: Optional[str] = None) -> pd.DataFrame:
        """The saved tables (``views``): bound to a table with fixed arguments, or a query — and the ones that failed."""
        defs: Dict[str, Any] = {}
        for n, d in (self._config().get("views") or {}).items():
            defs[n] = d
        for n, d in (self.duck.views or {}).items():
            defs.setdefault(n, d)
        failed = self.duck.failed_views or {}
        rows = []
        for n in sorted(set(defs) | set(failed)):
            d = defs.get(n) if isinstance(defs.get(n), dict) else {}
            rows.append({
                "name": n,
                "kind": "bound" if d.get("table") else ("query" if d.get("sql") else None),
                "table": d.get("table"),
                "args": _text(mask(d["args"])) if d.get("args") else None,
                "sql": d.get("sql"),
                "description": d.get("description"),
                "registered": n in (self.duck.views or {}) and n not in failed,
                "error": failed.get(n),
            })
        df = _frame(rows, ["name", "kind", "table", "args", "sql", "description", "registered", "error"])
        if name is not None:
            df = df[df["name"] == name]
        return df.reset_index(drop=True)

    @catalog
    @spark_plan("driver", why=_WHY)
    def settings(self, section: Optional[str] = None, key_ilike: Optional[str] = None) -> pd.DataFrame:
        """The configuration, one row per setting (``services.servicenow.timezone``), secrets as ``***``."""
        from .pushdown import require_like

        config = mask(self._config())
        rows = []
        for key, value in _flatten(config):
            top = key.split(".", 1)[0].split("[", 1)[0]
            parts = key.split(".")
            rows.append({
                "key": key,
                "value": _text(value),
                "type": _type_of(value),
                "section": top,
                "service": parts[1] if top == "services" and len(parts) > 1 else None,
                "secret": value == MASK,
            })
        df = _frame(rows, ["key", "value", "type", "section", "service", "secret"])
        if section is not None:
            df = df[df["section"] == section]
        if key_ilike is not None:
            like = require_like(key_ilike, "key_ilike")
            low, text = df["key"].str.lower(), like.text.lower()
            hit = {"equals": low == text, "startswith": low.str.startswith(text),
                   "endswith": low.str.endswith(text), "contains": low.str.contains(text, regex=False)}[like.kind]
            df = df[hit]
        return df.reset_index(drop=True)

    @catalog
    @spark_plan("driver", why=_WHY)
    def pipeline_runs(self, pipeline: Optional[str] = None) -> pd.DataFrame:
        """Each pipeline's last successful run and watermarks — what the ``lake.state`` folder holds."""
        columns = ["pipeline", "last_run_id", "last_success_at", "load", "watermarks", "previous_watermarks",
                   "state_file"]
        location = self._state_location()
        if not location:
            return _frame([], columns)
        try:
            import pyarrow.fs as pafs

            from .pipeline.sip import _filesystem
        except ImportError as exc:
            raise RuntimeError("duckduck.pipeline_runs reads the lake.state folder with pyarrow: "
                               "pip install \"duckduck[pipeline]\"") from exc
        from .pipeline import aws

        lake = self._config().get("lake")
        with aws.using(aws.account_of(lake.get("aws") if isinstance(lake, dict) else None, self.duck)):
            return self._runs(location, pipeline, columns, _filesystem, pafs)

    def _runs(self, location, pipeline, columns, _filesystem, pafs) -> pd.DataFrame:
        fs, base = _filesystem(location)
        try:
            infos = fs.get_file_info(pafs.FileSelector(base.rstrip("/"), allow_not_found=True))
        except (OSError, ValueError):
            infos = []
        rows = []
        for info in sorted(infos, key=lambda i: i.path):
            if info.type != pafs.FileType.File or not info.path.endswith(".json"):
                continue
            name = os.path.basename(info.path)[:-5]
            if pipeline is not None and name != pipeline:
                continue
            try:
                with fs.open_input_stream(info.path) as f:
                    state = json.loads(f.read().decode("utf-8"))
            except (OSError, ValueError):
                continue
            load = state.get("load")
            rows.append({
                "pipeline": name,
                "last_run_id": state.get("last_run_id"),
                "last_success_at": state.get("last_success_at"),
                "load": (load.get("type") if isinstance(load, dict) else load),
                "watermarks": _text(state.get("watermarks")) if state.get("watermarks") else None,
                "previous_watermarks": _text(state.get("previous_watermarks")) if state.get("previous_watermarks")
                else None,
                "state_file": info.path if "://" not in location else location.rstrip("/") + "/" + name + ".json",
            })
        return _frame(rows, columns)

    def _state_location(self) -> Optional[str]:
        lake = self._config().get("lake")
        state = lake.get("state") if isinstance(lake, dict) else None
        if not isinstance(state, str) or not state:
            return None
        path = getattr(self.duck, "_config_path", None)
        if "://" in state or os.path.isabs(state) or not path:
            return state
        return os.path.normpath(os.path.join(os.path.dirname(path), state))


TABLES = ("services", "tables", "saved_tables", "settings", "pipeline_runs")


def register(duck: Any) -> SystemTables:
    """Registers ``duckduck_<table>`` for each system table (addresses ``duckduck.<table>``)."""
    system = SystemTables(duck)
    duck.service_prefix[SERVICE] = SERVICE
    for t in TABLES:
        name = f"{SERVICE}_{t}"
        duck.register_api_function(name, getattr(system, t))
        duck.service_of[name] = SERVICE
    return system

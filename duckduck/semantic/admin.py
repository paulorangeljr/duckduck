"""
The web app's admin side: a SQL console over the registered tables, and the
``duckduck.json`` editor (every option documented, secrets masked).

**SQL console** (``SQLConsole``). Runs ``duck.sql(...)`` — push-down and all
— on a *separate* ``DuckAPI`` that shares the registered functions but whose
own DuckDB connection is locked: ``enable_external_access = false`` (no
files, no network, no ``COPY`` / ``ATTACH`` / ``INSTALL``, no stored
secrets) and ``lock_configuration = true`` (the SQL can't turn it back on).
Only read statements are accepted (``SELECT`` / ``WITH`` / ``FROM`` /
``SHOW`` / ``DESCRIBE`` / ``SUMMARIZE`` / ``EXPLAIN`` / ``VALUES``, one at a
time). The ``duckduck`` log of that one call (which WHERE / LIMIT reached
each API) comes back with the rows.

**Config** (``config_reference``, ``mask``, ``unmask``, ``validate_config``,
``save_config``). The reference lists every option: each connector's
parameters (from ``SERVICE_REGISTRY`` and the wrapper's constructor), the
``authentication`` block, ``ai_providers`` entries and the whole
``semantic`` section (from the Pydantic models, descriptions from their
``#:`` comments). Secret values travel masked (``"***"``) and are put back
from the file on save; a save is validated first and keeps a ``.bak``.
"""

import datetime as dt
import inspect
import json
import logging
import os
import re
import shutil
import threading
import time
import typing
from typing import Any, Dict, List, Optional, Tuple

MASK = "***"
_SECRETISH = re.compile(r"pass(word|wd)?|secret|token|api[-_]?key|private[-_]?key|connection[-_]?string|"
                        r"\bsas\b|signature|credential", re.I)
_NOT_SECRET_SUFFIX = ("_id", "_env", "_file", "_path", "_url", "_name", "_type")

_READ_STATEMENTS = ("select", "with", "from", "show", "describe", "summarize", "explain", "values", "table",
                    "list", "pivot", "unpivot", "(")


# ---------------------------------------------------------------------------
# SQL console
# ---------------------------------------------------------------------------


class SQLConsole:
    """``duck.sql`` for the page — read-only, locked away from files and network."""

    def __init__(self, duck: Any, max_rows: int = 1000, timeout: float = 120.0):
        from duckduck import DuckAPI

        console = DuckAPI()
        console.functions = duck.functions  # shared: what's registered later shows up here too
        console.service_of = duck.service_of
        console._streaming_functions = getattr(duck, "_streaming_functions", {})
        console.conn.execute("SET enable_external_access = false")
        console.conn.execute("SET lock_configuration = true")
        self.duck = console
        self.max_rows = max_rows
        self.timeout = timeout
        self._lock = threading.Lock()  # one DuckDB connection: one query at a time

    def run(self, query: str) -> Dict[str, Any]:
        reason = read_only_reason(query)
        if reason:
            return {"error": reason, "columns": [], "rows": [], "row_count": 0, "log": []}
        started = time.perf_counter()
        log: List[str] = []
        with self._lock, _captured_log(log):
            timer = threading.Timer(self.timeout, self.duck.conn.interrupt)
            timer.start()
            try:
                df = self.duck.sql(query).df()
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}", "columns": [], "rows": [], "row_count": 0,
                        "log": log, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}
            finally:
                timer.cancel()
        shown = df.head(self.max_rows)
        return {
            "columns": [str(c) for c in df.columns],
            "rows": shown.astype(object).where(shown.notna(), None).values.tolist(),
            "row_count": int(len(df)), "truncated": len(df) > self.max_rows,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "log": log,
        }

    def tables(self) -> List[Dict[str, Any]]:
        df = self.duck.list_tables()
        kinds = {name: source_kind(self.duck.functions.get(name)) for name in df["name"]}
        out = df[["name", "kind", "usage", "pushdown", "source", "description"]].astype(object)
        records = out.where(out.notna(), None).to_dict(orient="records")
        for r in records:
            r["icon"] = kinds.get(r["name"], "api")
            r["service"] = self.duck.service_of.get(r["name"])
        return records


def read_only_reason(query: str) -> Optional[str]:
    """Why ``query`` isn't accepted (not a single read statement), or ``None``."""
    text = re.sub(r"(--[^\n]*\n?|/\*.*?\*/)", " ", query or "", flags=re.S).strip()
    if not text:
        return "empty query"
    first = re.match(r"\(|[A-Za-z]+", text)
    if not first or first.group(0).lower() not in _READ_STATEMENTS:
        return "only read queries: SELECT, WITH, FROM, SHOW, DESCRIBE, SUMMARIZE, EXPLAIN, VALUES"
    if len(_statements(text)) > 1:
        return "one statement at a time"
    if first.group(0).lower() == "with" and re.search(r"\)\s*(insert|update|delete|merge)\b", text, re.I):
        return "only read queries: this WITH writes"
    return None


def _statements(text: str) -> List[str]:
    """Splits on ``;`` outside quotes and comments."""
    parts, buf, quote = [], [], None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


class _ThreadLog(logging.Handler):
    def __init__(self, sink: List[str]):
        super().__init__(logging.INFO)
        self.sink, self.thread = sink, threading.get_ident()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread and len(self.sink) < 500:
            try:
                self.sink.append(self.format(record))
            except Exception:
                pass


class _captured_log:
    """The ``duckduck`` log lines this thread writes during the block (at INFO, whatever the global level)."""

    def __init__(self, sink: List[str]):
        self.sink = sink

    def __enter__(self):
        self.logger = logging.getLogger("duckduck")
        self.handler = _ThreadLog(self.sink)
        self.level = self.logger.level
        self.logger.addHandler(self.handler)
        if self.logger.getEffectiveLevel() > logging.INFO:
            self.logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.level)


#: A registered function's module → the kind of source it is (the page draws an icon per kind).
SOURCE_KINDS = {
    "duckduck.sharepoint": "sharepoint", "duckduck.rapid7": "insightvm", "duckduck.servicenow": "servicenow",
    "duckduck.axonius": "axonius", "duckduck.database": "database", "duckduck.glue": "glue",
    "duckduck.blob_storage": "blob_storage", "duckduck.adx": "adx", "duckduck.local_files": "files",
    "duckduck.python_source": "python",
}


def source_kind(fn: Any) -> str:
    module = getattr(fn, "__module__", None) or getattr(type(fn), "__module__", "") or ""
    for prefix, kind in SOURCE_KINDS.items():
        if module == prefix or module.startswith(prefix + "."):
            return kind
    return "api"


# ---------------------------------------------------------------------------
# Config: reference
# ---------------------------------------------------------------------------


def config_reference() -> Dict[str, Any]:
    """Every option of ``duckduck.json``, documented — what the Config tab lists."""
    from duckduck.registry import SERVICE_REGISTRY

    from .config import AIProviderConfig, SemanticConfig

    connectors = []
    for name, spec in SERVICE_REGISTRY.items():
        cls = getattr(spec.factory, "__self__", None)
        doc = _first_paragraph(inspect.getdoc(spec.factory) or inspect.getdoc(cls) or "")
        options = []
        if cls is not None:
            for p in list(inspect.signature(cls.__init__).parameters.values())[1:]:
                if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                options.append({"name": p.name, "type": _type_name(p.annotation),
                                "default": None if p.default is p.empty else _jsonable(p.default),
                                "required": p.default is p.empty, "secret": _is_secret_key(p.name)})
        connectors.append({
            "connector": name, "class": cls.__name__ if cls else "", "description": doc, "options": options,
            "tables": sorted(spec.tables), "dynamic_tables": bool(spec.dynamic_tables),
            "requires_authentication": spec.requires_authentication, "path_options": list(spec.path_options),
        })
    return {
        "top_level": [
            {"name": "services", "type": "object", "description": "One entry per connection: {name: {connector, "
             "authentication, ...options}}. Its tables are registered as <name>_<table> (or table_prefix)."},
            {"name": "on_error", "type": "raise | warn", "default": "raise",
             "description": "warn: a service that fails to connect is skipped with a warning instead of stopping."},
            {"name": "ai_providers", "type": "object", "description": "Named LLMs and decision engines, "
             "referenced by name from semantic (default_llm, decision_engine.ai_provider, ...)."},
            {"name": "semantic", "type": "object", "description": "Natural-language search: catalog, decision "
             "engine, extractor, catalog generation, feedback, thresholds..."},
        ],
        "service_common": [
            {"name": "connector", "type": "string", "description": "Which connector (below). Default: the "
             "service's own name."},
            {"name": "table_prefix", "type": "string", "description": "Tables are registered as <prefix>_<table>; "
             "\"\" for bare table names. Default: the service's name."},
            {"name": "authentication", "type": "object", "description": "Where credentials come from — below."},
        ],
        "authentication": [
            {"name": "type", "type": "local | aws | azure", "default": "local",
             "description": "local: the other keys of this block are the credentials. aws: AWS Secrets Manager. "
             "azure: Azure Key Vault."},
            {"name": "secret_id", "type": "string", "description": "aws / azure: the secret's name or ARN."},
            {"name": "region_name", "type": "string", "description": "aws: the Secrets Manager region."},
            {"name": "profile_name", "type": "string", "description": "aws: a named profile from ~/.aws."},
            {"name": "vault_url", "type": "string", "description": "azure: https://<vault>.vault.azure.net."},
            {"name": "tenant_id", "type": "string", "description": "azure: pin DefaultAzureCredential to a tenant."},
            {"name": "<any key>", "type": "value | \"$secret.<key>\"",
             "description": "Added on top of the fetched secret; \"$secret.<key>\" copies <key> from it."},
        ],
        "connectors": connectors,
        "ai_provider": _model_options(AIProviderConfig),
        "semantic": _model_options(SemanticConfig, skip={"ai_providers", "base_dir", "config_file"}),
    }


def _model_options(model: Any, skip: Tuple[str, ...] = (), depth: int = 0) -> List[Dict[str, Any]]:
    from pydantic import BaseModel

    docs = _attr_docs(model)
    out = []
    for name, field in model.model_fields.items():
        if name in skip:
            continue
        key = field.alias or name
        entry: Dict[str, Any] = {
            "name": key, "type": _type_name(field.annotation), "description": docs.get(name, ""),
            "default": None if field.is_required() else _jsonable(field.get_default(call_default_factory=True)),
            "required": field.is_required(),
        }
        nested = _nested_model(field.annotation)
        if nested is not None and issubclass(nested, BaseModel) and depth < 3:
            entry["options"] = _model_options(nested, depth=depth + 1)
            entry["default"] = None
            entry["description"] = entry["description"] or _first_paragraph(inspect.getdoc(nested) or "")
        out.append(entry)
    return out


def _nested_model(annotation: Any) -> Any:
    from pydantic import BaseModel

    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        found = _nested_model(arg)
        if found is not None:
            return found
    return None


def _attr_docs(cls: Any) -> Dict[str, str]:
    """``#:`` comments right above each attribute of a class (and its bases)."""
    docs: Dict[str, str] = {}
    for klass in reversed(cls.__mro__):
        try:
            lines = inspect.getsource(klass).splitlines()
        except (OSError, TypeError):
            continue
        pending: List[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#:"):
                pending.append(stripped[2:].strip())
                continue
            m = re.match(r"([A-Za-z_]\w*)\s*:", stripped)
            if m and pending:
                docs[m.group(1)] = " ".join(pending)
            if stripped and not stripped.startswith("#"):
                pending = []
    return docs


def _type_name(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty or annotation is None:
        return ""
    if inspect.isclass(annotation):
        return annotation.__name__
    text = str(annotation).replace("typing.", "")
    text = re.sub(r"\b[\w.]+\.(\w+)", r"\1", text)
    return text.replace("NoneType", "None")


def _first_paragraph(doc: str) -> str:
    return doc.strip().split("\n\n")[0].replace("\n", " ") if doc else ""


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return str(value)


# ---------------------------------------------------------------------------
# Config: masking, validation, saving
# ---------------------------------------------------------------------------


def _is_secret_key(key: str) -> bool:
    key = str(key)
    return bool(_SECRETISH.search(key)) and not key.lower().endswith(_NOT_SECRET_SUFFIX)


def mask(data: Any) -> Any:
    """Secret values (by key name) → ``"***"``; ``"$secret.<key>"`` references stay (they aren't secrets)."""
    if isinstance(data, dict):
        return {k: (MASK if _is_secret_key(k) and isinstance(v, str) and v and not v.startswith("$secret.")
                    else mask(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [mask(v) for v in data]
    return data


def unmask(edited: Any, saved: Any, path: str = "") -> Any:
    """Puts the saved secret back wherever the edited config still says ``"***"``."""
    if isinstance(edited, dict):
        return {k: unmask(v, saved.get(k) if isinstance(saved, dict) else None, f"{path}.{k}" if path else k)
                for k, v in edited.items()}
    if isinstance(edited, list):
        return [unmask(v, saved[i] if isinstance(saved, list) and i < len(saved) else None, f"{path}[{i}]")
                for i, v in enumerate(edited)]
    if edited == MASK:
        if not isinstance(saved, str) or saved == MASK:
            raise ValueError(f"{path}: \"***\" stands for a saved secret, but there is none there — type the value")
        return saved
    return edited


def validate_config(data: Any, path: str = "") -> Dict[str, List[str]]:
    """``{"errors": [...], "warnings": [...]}`` — structure, connectors, authentication, the semantic section."""
    from duckduck.registry import SERVICE_REGISTRY

    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(data, dict):
        return {"errors": ["the config must be a JSON object"], "warnings": []}
    known = {"services", "on_error", "ai_providers", "semantic"}
    for key in sorted(set(data) - known):
        warnings.append(f"unknown top-level key {key!r} (known: {', '.join(sorted(known))})")
    if data.get("on_error") not in (None, "raise", "warn"):
        errors.append("on_error must be \"raise\" or \"warn\"")
    services = data.get("services", {})
    if not isinstance(services, dict):
        errors.append("services must be an object: {name: {connector, authentication, ...}}")
        services = {}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            errors.append(f"services.{name} must be an object")
            continue
        connector = svc.get("connector", name)
        spec = SERVICE_REGISTRY.get(connector)
        if spec is None:
            errors.append(f"services.{name}: unknown connector {connector!r} "
                          f"(known: {', '.join(sorted(SERVICE_REGISTRY))})")
            continue
        auth = svc.get("authentication")
        if auth is None:
            if spec.requires_authentication:
                errors.append(f"services.{name}: an authentication block is required for {connector!r}")
        elif not isinstance(auth, dict):
            errors.append(f"services.{name}.authentication must be an object")
        else:
            kind = auth.get("type", "local")
            if kind not in ("local", "aws", "azure"):
                errors.append(f"services.{name}.authentication.type must be local, aws or azure")
            if kind in ("aws", "azure") and not auth.get("secret_id"):
                errors.append(f"services.{name}.authentication: {kind} needs secret_id")
            if kind == "azure" and not auth.get("vault_url"):
                errors.append(f"services.{name}.authentication: azure needs vault_url")
        cls = getattr(spec.factory, "__self__", None)
        if cls is not None:
            params = set(list(inspect.signature(cls.__init__).parameters)[1:])
            takes_any = any(p.kind == p.VAR_KEYWORD for p in inspect.signature(cls.__init__).parameters.values())
            extra = set(svc) - {"connector", "authentication", "table_prefix"} - params
            if extra and not takes_any:
                warnings.append(f"services.{name}: {', '.join(sorted(extra))} — not an option of {connector!r} "
                                f"(options: {', '.join(sorted(params))})")
    if "semantic" in data:
        from .config import SemanticConfig

        try:
            SemanticConfig.from_file_data(data, path)
        except Exception as exc:  # pydantic ValidationError or ValueError — both read well
            errors.append(f"semantic: {exc}")
    return {"errors": errors, "warnings": warnings}


def save_config(path: str, edited: Dict[str, Any]) -> Dict[str, Any]:
    """Unmasks, validates and writes the config (keeping ``<path>.bak``). Raises ``ValueError`` if invalid."""
    saved = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
    data = unmask(edited, saved)
    report = validate_config(data, path)
    if report["errors"]:
        raise ValueError("; ".join(report["errors"]))
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return {"saved": path, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "warnings": report["warnings"]}

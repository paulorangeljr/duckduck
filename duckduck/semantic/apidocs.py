"""
API documentation as context for catalog generation.

A table backed by an HTTP API is best described by the API's own docs: what
each response field means, its allowed values, its format. The data sample
shows what's there; the docs say what it means. ``catalog_generation.api_docs``
points tables (by the same selectors as ``force``/``only``) at their docs, by
file path, URL, or inline ``content`` (internal APIs rarely publish theirs):
an OpenAPI/Swagger spec (JSON or YAML), a simple field-docs JSON written by
hand, or any text (Markdown, HTML, plain).

- **OpenAPI / Swagger**: the operation behind the table is found (named with
  ``operation``, else matched by the table's name, the connector method's name
  and its structural args), and only that operation is sent: its summary, its
  query parameters, and the response fields that match the table's columns —
  nested objects flattened with ``_``, like DuckAPI's normalization.
- **Field docs** (hand-written, for an API with no spec)::

      {"description": "...", "notes": "...",
       "fields": {"status": {"description": "...", "values": {"A": ["active"]}},
                  "risk": "Risk score, 0-1000"}}

  or several tables in one document: ``{"tables": {"assets": {...}, ...}}``
  (the entry named like the table is used). Fields are matched to columns
  like a spec's; ``enum``/``values`` go into the catalog's value lists.
- **Text**: the sections that mention the table or its columns, within
  ``max_chars``; a short document is sent whole.

Nothing here calls an LLM; documented enums are also merged into the
catalog's value lists deterministically (see ``generation._apply_docs``).
"""

import html
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .text import stem

logger = logging.getLogger("duckduck.semantic.apidocs")

#: Refuse documents larger than this (bytes).
MAX_DOC_BYTES = 30 * 1024 * 1024
_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
#: Properties that usually wrap the list of records in a paged response.
_WRAPPERS = {"resources", "value", "result", "results", "data", "items", "records", "entries", "hits", "content",
             "elements", "list", "rows"}
#: Name parts too generic to tell operations apart.
_GENERIC = {stem(w) for w in ("table", "tables", "list", "get", "api", "query", "item", "items", "all", "data",
                              "record", "records", "iter", "fetch", "search", "now", "v1", "v2", "v3", "id")}
_MAX_DESCRIPTION = 300
_MAX_ENUM = 50
_MAX_PARAMETERS = 30
_MAX_DEPTH = 3


@dataclass
class DocsRef:
    """Where a table's docs are, and optionally which operation documents it."""

    #: File path (relative to the config file's folder) or http(s) URL.
    location: Optional[str] = None
    #: ``"GET /api/3/assets"``, a bare path, or an operationId. Omitted → matched by name.
    operation: Optional[str] = None
    #: The docs themselves, inline: a spec or field-docs dict, or text.
    content: Any = None

    def __post_init__(self):
        if (self.location is None) == (self.content is None):
            raise ValueError("api docs need exactly one of 'location' (file or URL) or 'content' (inline)")


class ApiDocs:
    """One loaded document: an OpenAPI/Swagger spec, or text."""

    def __init__(self, location: str, text: str, spec: Optional[Dict[str, Any]] = None,
                 fields_doc: Optional[Dict[str, Any]] = None):
        self.location = location
        self.text = text
        #: An OpenAPI/Swagger spec.
        self.spec = spec
        #: A hand-written field-docs document (see the module docstring).
        self.fields_doc = fields_doc

    @classmethod
    def from_content(cls, content: Any, label: str = "inline") -> "ApiDocs":
        """Docs given inline: a dict (spec or field docs) or text (JSON/YAML text is parsed too)."""
        if isinstance(content, dict):
            return cls._from_data(label, json.dumps(content), content)
        if not isinstance(content, str):
            raise ValueError(f"inline api docs must be a JSON object or text, not {type(content).__name__}")
        return cls._from_raw(label, content)

    @classmethod
    def _from_raw(cls, location: str, raw: str) -> "ApiDocs":
        data = _parse_data(raw, location)
        if data is not None:
            docs = cls._from_data(location, raw, data)
            if docs.spec is not None or docs.fields_doc is not None:
                return docs
        if _looks_html(raw, location):
            raw = html_to_text(raw)
        return cls(location, raw)

    @classmethod
    def _from_data(cls, location: str, raw: str, data: Any) -> "ApiDocs":
        if isinstance(data, dict) and ("openapi" in data or "swagger" in data) and isinstance(data.get("paths"), dict):
            return cls(location, raw, spec=data)
        if isinstance(data, dict) and (isinstance(data.get("fields"), dict) or isinstance(data.get("tables"), dict)):
            return cls(location, raw, fields_doc=data)
        if isinstance(data, dict):
            raise ValueError(f"api docs '{location}': a JSON document must be an OpenAPI/Swagger spec, or have "
                             f"'fields' or 'tables' (field docs) — found keys {sorted(data)[:8]}")
        return cls(location, raw)

    @classmethod
    def load(cls, location: str, base_dir: str = ".", timeout: float = 30.0) -> "ApiDocs":
        if re.match(r"^https?://", location, re.I):
            import requests

            response = requests.get(location, timeout=timeout)
            response.raise_for_status()
            raw = response.text
        else:
            path = location if os.path.isabs(location) else os.path.join(base_dir, location)
            if not os.path.exists(path):
                raise FileNotFoundError(f"api docs '{location}' not found (looked at {os.path.abspath(path)})")
            if os.path.getsize(path) > MAX_DOC_BYTES:
                raise ValueError(f"api docs '{location}' is larger than {MAX_DOC_BYTES // (1024 * 1024)} MB")
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        if len(raw) > MAX_DOC_BYTES:
            raise ValueError(f"api docs '{location}' is larger than {MAX_DOC_BYTES // (1024 * 1024)} MB")
        return cls._from_raw(location, raw)

    @property
    def kind(self) -> str:
        return "openapi" if self.spec is not None else "field docs" if self.fields_doc is not None else "text"

    # ------------------------------------------------------------------

    def for_table(
        self,
        names: Iterable[str],
        columns: Iterable[str],
        args: Optional[Dict[str, Any]] = None,
        operation: Optional[str] = None,
        max_chars: int = 6000,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        ``(context for the LLM, note)`` — the context is ``None`` when nothing
        in the document could be tied to this table (the note says why).
        """
        columns = [str(c) for c in columns]
        wanted = _wanted(list(names) + [str(v) for v in (args or {}).values() if isinstance(v, (str, int))])
        if self.spec is not None:
            return self._openapi_context(wanted, columns, len(args or {}), operation)
        if self.fields_doc is not None:
            return self._fields_context(list(names), wanted, columns)
        return self._text_context(wanted, columns, max_chars)

    # -- hand-written field docs ---------------------------------------

    def _fields_context(self, names: List[str], wanted: Set[str], columns: List[str]):
        doc = self.fields_doc
        if isinstance(doc.get("tables"), dict):
            tables = doc["tables"]
            lowered = {str(k).lower(): k for k in tables}
            key = next((lowered[n.lower()] for n in names if n and n.lower() in lowered), None)
            if key is None:
                scored = sorted(((len(wanted & _wanted([k])), k) for k in tables), key=lambda x: -x[0])
                key = scored[0][1] if scored and scored[0][0] > 0 else None
            if key is None:
                return None, f"api docs '{self.location}': no entry in 'tables' is named like this table ({sorted(tables)[:10]})"
            entry = tables[key] if isinstance(tables[key], dict) else {"description": tables[key]}
            table = str(key)
        else:
            entry, table = doc, None
        fields = {str(k): _field_doc(v) for k, v in (entry.get("fields") or {}).items()}
        matched = _match_columns(fields, columns)
        context = {
            "from": self.location,
            "table": table,
            "description": _trim(entry.get("description") or (doc.get("description") if table else None), 1200),
            "notes": _trim(entry.get("notes"), 1200),
            "fields": matched,
        }
        context = {k: v for k, v in context.items() if v}
        note = None
        if fields and not matched:
            note = f"api docs '{self.location}': its fields match none of this table's columns"
        return context, note

    # -- OpenAPI -------------------------------------------------------

    def _openapi_context(self, wanted: Set[str], columns: List[str], nargs: int, operation: Optional[str]):
        ops = list(_operations(self.spec))
        if not ops:
            return None, f"api docs '{self.location}': the spec has no paths"
        if operation:
            found = _find_operation(ops, operation)
            if found is None:
                return None, f"api docs '{self.location}': operation '{operation}' isn't in the spec"
        else:
            scored = sorted(((_score(op, wanted, nargs), op) for op in ops), key=lambda x: (-x[0], len(x[1][1])))
            if not scored or scored[0][0] <= 0:
                return None, (f"api docs '{self.location}': no operation matches {sorted(wanted) or 'this table'} "
                              f"— name one with \"operation\"")
            found = scored[0][1]
        method, path, op, common = found
        fields = _flatten(self.spec, _row_schema(self.spec, _response_schema(self.spec, op)))
        matched = _match_columns(fields, columns)
        context: Dict[str, Any] = {
            "from": self.location,
            "api": _trim((self.spec.get("info") or {}).get("title"), 100),
            "operation": f"{method} {path}",
            "summary": _trim(op.get("summary"), _MAX_DESCRIPTION),
            "description": _trim(op.get("description"), 1200),
            "parameters": _parameters(self.spec, [*common, *(op.get("parameters") or [])]),
            "fields": matched,
        }
        context = {k: v for k, v in context.items() if v}
        note = None
        if not matched:
            note = (f"api docs '{self.location}': {method} {path} documents none of this table's columns "
                    f"— only its summary and parameters were used")
        return context, note

    # -- text ----------------------------------------------------------

    def _text_context(self, wanted: Set[str], columns: List[str], max_chars: int):
        text = self.text.strip()
        if not text:
            return None, f"api docs '{self.location}' is empty"
        if len(text) <= max_chars:
            return {"from": self.location, "excerpt": text}, None
        sections = _sections(text)
        col_res = [re.compile(rf"(?<![A-Za-z0-9_]){re.escape(c)}(?![A-Za-z0-9_])", re.I) for c in columns if len(c) > 2]
        scored = []
        for i, (heading, body) in enumerate(sections):
            head_stems = {stem(t) for t in re.findall(r"[a-z0-9]+", heading.lower())}
            body_stems = {stem(t) for t in re.findall(r"[a-z0-9]+", body.lower())}
            score = 3 * len(wanted & head_stems) + len(wanted & body_stems)
            score += sum(1 for r in col_res if r.search(body))
            if score > 0:
                scored.append((score, i))
        if not scored:
            return None, f"api docs '{self.location}': no section mentions this table or its columns"
        chosen, used = [], 0
        for score, i in sorted(scored, key=lambda x: (-x[0], x[1])):
            block = (sections[i][0] + "\n" + sections[i][1]).strip()
            if used + len(block) > max_chars:
                if not chosen:  # the best section alone is too long: keep its start
                    chosen.append((i, block[:max_chars] + "…"))
                continue
            chosen.append((i, block))
            used += len(block)
        excerpt = "\n…\n".join(b for _, b in sorted(chosen))
        return {"from": self.location, "excerpt": excerpt}, None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_data(raw: str, location: str) -> Any:
    """JSON (``{...}``) or YAML (``.yaml``/``.yml``, or ``openapi:``/``swagger:``/``fields:``/``tables:`` first) → data."""
    head = raw.lstrip()[:200]
    if head.startswith("{"):
        try:
            return json.loads(raw)
        except ValueError as exc:
            if location.lower().endswith(".json"):
                raise ValueError(f"api docs '{location}' isn't valid JSON: {exc}") from exc
            return None
    if location.lower().endswith((".yaml", ".yml")) or re.match(r"^(openapi|swagger|fields|tables)\s*:", head):
        try:
            import yaml

            return yaml.safe_load(raw)
        except Exception:
            return None
    return None


def _field_doc(value: Any) -> Dict[str, Any]:
    """A hand-written field entry: ``"text"`` or ``{description, type, format, enum, values}``."""
    if not isinstance(value, dict):
        return {"description": _trim(value, _MAX_DESCRIPTION)} if value not in (None, "") else {}
    values = value.get("values")
    entry = {
        "type": value.get("type"),
        "format": value.get("format"),
        "description": _trim(value.get("description"), _MAX_DESCRIPTION),
        "enum": [str(v) for v in (value.get("enum") or [])][:_MAX_ENUM],
        "values": ({str(k): [str(s) for s in (v if isinstance(v, list) else [v] if v else [])]
                    for k, v in list(values.items())[:_MAX_ENUM]} if isinstance(values, dict)
                   else {str(v): [] for v in values[:_MAX_ENUM]} if isinstance(values, list) else None),
    }
    return {k: v for k, v in entry.items() if v}


def _looks_html(raw: str, location: str) -> bool:
    return location.lower().split("?")[0].endswith((".html", ".htm")) or bool(
        re.search(r"<(html|body|h[1-6]|p|div)\b", raw[:5000], re.I)
    )


def html_to_text(raw: str) -> str:
    """HTML → text, headings kept as Markdown ``#`` lines so sections can be found."""
    text = re.sub(r"(?is)<(script|style|nav|footer|header)\b.*?</\1>", " ", raw)
    text = re.sub(r"(?is)<h([1-6])\b[^>]*>(.*?)</h\1>", lambda m: "\n" + "#" * int(m.group(1)) + " " + m.group(2) + "\n", text)
    text = re.sub(r"(?i)<(br|/p|/li|/tr|/div|/pre|/table)\b[^>]*>", "\n", text)
    text = re.sub(r"(?i)<(td|th)\b[^>]*>", " | ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _sections(text: str) -> List[Tuple[str, str]]:
    """``[(heading, body)]`` by Markdown headings; paragraph groups when there are none."""
    parts = re.split(r"(?m)^(#{1,6}\s.*)$", text)
    if len(parts) >= 3:
        sections = [("", parts[0])] if parts[0].strip() else []
        sections += [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
        return sections
    sections, current = [], ""
    for para in re.split(r"\n\s*\n", text):
        if current and len(current) + len(para) > 1500:
            sections.append(("", current))
            current = ""
        current = (current + "\n\n" + para).strip()
    if current:
        sections.append(("", current))
    return sections


# ---------------------------------------------------------------------------
# OpenAPI helpers
# ---------------------------------------------------------------------------


def _words(text: Any) -> List[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))  # getAssetGroups → get Asset Groups
    return re.findall(r"[a-z0-9]+", text.lower())


def _wanted(names: Iterable[str]) -> Set[str]:
    return {stem(w) for n in names for w in _words(n) if len(w) > 1} - _GENERIC


def _operations(spec: Dict[str, Any]):
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        common = item.get("parameters") or []
        for method, op in item.items():
            if str(method).lower() in _METHODS and isinstance(op, dict):
                yield str(method).upper(), str(path), op, common


def _find_operation(ops, wanted: str):
    text = wanted.strip()
    method, _, path = text.partition(" ")
    if not path:
        method, path = "", text
    norm = path.rstrip("/").lower()
    for op in ops:
        if op[2].get("operationId") == text:
            return op
    candidates = [op for op in ops if op[1].rstrip("/").lower() == norm and (not method or op[0] == method.upper())]
    candidates.sort(key=lambda op: op[0] != "GET")
    return candidates[0] if candidates else None


def _score(op, wanted: Set[str], nargs: int) -> float:
    method, path, body, _ = op
    static = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    last = {stem(w) for w in _words(static[-1])} if static else set()
    rest = {stem(w) for s in static[:-1] for w in _words(s)}
    op_id = {stem(w) for w in _words(body.get("operationId"))}
    about = {stem(w) for w in _words(" ".join([str(body.get("summary") or ""), *map(str, body.get("tags") or [])]))}
    score = 3 * len(wanted & last) + 2 * len(wanted & op_id) + len(wanted & about) + 0.5 * len(wanted & rest)
    if score <= 0:
        return 0
    if method == "GET":
        score += 1
    if path.rstrip("/").endswith("}"):  # one item by id, not the listing
        score -= 1
    return score - 0.5 * abs(path.count("{") - nargs)


def _resolve(spec: Dict[str, Any], node: Any, seen: Optional[Set[str]] = None) -> Any:
    seen = set() if seen is None else seen
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if not ref.startswith("#/") or ref in seen:
            return {}
        seen.add(ref)
        target: Any = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, dict) else None
        node = target if target is not None else {}
    return node if isinstance(node, dict) else {}


def _merged(spec: Dict[str, Any], node: Any, seen: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Resolves ``$ref`` and folds ``allOf`` (and the first ``oneOf``/``anyOf``) into one schema."""
    node = _resolve(spec, node, seen)
    if not any(k in node for k in ("allOf", "oneOf", "anyOf")):
        return node
    out = {k: v for k, v in node.items() if k not in ("allOf", "oneOf", "anyOf")}
    parts = list(node.get("allOf") or []) + list((node.get("oneOf") or node.get("anyOf") or [])[:1])
    props = dict(out.get("properties") or {})
    for part in parts:
        sub = _merged(spec, part, seen)
        props.update(sub.get("properties") or {})
        for k, v in sub.items():
            if k != "properties":
                out.setdefault(k, v)
    if props:
        out["properties"] = props
    return out


def _response_schema(spec: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    responses = op.get("responses") or {}
    for code in ("200", 200, "201", 201, "2XX", "2xx", "default"):
        if code in responses:
            response = _resolve(spec, responses[code])
            break
    else:
        return {}
    if "schema" in response:  # Swagger 2
        return _merged(spec, response["schema"])
    content = response.get("content") or {}
    media = next((m for m in content if "json" in m.lower()), next(iter(content), None))
    return _merged(spec, (content.get(media) or {}).get("schema")) if media else {}


def _is_array(schema: Dict[str, Any]) -> bool:
    return schema.get("type") == "array" or "items" in schema


def _row_schema(spec: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    """The schema of one record: unwraps arrays and paged envelopes (``{"resources": [...], "page": ...}``)."""
    for _ in range(4):
        if _is_array(schema):
            schema = _merged(spec, schema.get("items"))
            continue
        props = schema.get("properties") or {}
        arrays = [k for k, v in props.items() if _is_array(_merged(spec, v))]
        wrapper = [k for k in arrays if k.lower() in _WRAPPERS]
        pick = wrapper[0] if wrapper else (arrays[0] if len(arrays) == 1 and len(props) <= 3 else None)
        if pick is None:
            obj = [k for k, v in props.items() if k.lower() in _WRAPPERS and (_merged(spec, v).get("properties"))]
            if len(obj) == 1 and len(props) <= 3:  # {"data": {...record...}}
                schema = _merged(spec, props[obj[0]])
                continue
            break
        schema = _merged(spec, _merged(spec, props[pick]).get("items"))
    return schema


def _flatten(spec: Dict[str, Any], schema: Dict[str, Any], prefix: str = "", depth: int = 0) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for name, sub in (schema.get("properties") or {}).items():
        key = f"{prefix}_{name}" if prefix else str(name)
        resolved = _merged(spec, sub)
        if resolved.get("properties") and depth < _MAX_DEPTH:
            out.update(_flatten(spec, resolved, key, depth + 1))
        else:
            out[key] = _leaf(resolved)
    return out


def _leaf(schema: Dict[str, Any]) -> Dict[str, Any]:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind == "array":
        item = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        kind = f"array of {item.get('type') or 'objects'}"
    leaf = {
        "type": kind,
        "format": schema.get("format"),
        "description": _trim(schema.get("description") or schema.get("title"), _MAX_DESCRIPTION),
        "enum": [str(v) for v in (schema.get("enum") or []) if v is not None][:_MAX_ENUM],
        "example": _trim(schema.get("example"), 80) if not isinstance(schema.get("example"), (dict, list)) else None,
    }
    return {k: v for k, v in leaf.items() if v}


def _match_columns(fields: Dict[str, Dict[str, Any]], columns: List[str]) -> Dict[str, Dict[str, Any]]:
    """Documented fields keyed by the table's column names (exact, case-insensitive, or a unique suffix match)."""
    lowered = {k.lower(): k for k in fields}
    out = {}
    for col in columns:
        low = col.lower()
        key = lowered.get(low)
        if key is None:
            hits = [k for k in fields if k.lower().endswith("_" + low) or low.endswith("_" + k.lower())]
            key = hits[0] if len(hits) == 1 else None
        if key is not None and fields[key]:
            out[col] = fields[key]
    return out


def _parameters(spec: Dict[str, Any], params: List[Any]) -> List[Dict[str, Any]]:
    out, seen = [], set()
    for p in params:
        p = _resolve(spec, p)
        name, where = p.get("name"), p.get("in")
        if not name or where not in ("query", "path") or (name, where) in seen:
            continue
        seen.add((name, where))
        schema = _merged(spec, p.get("schema")) if "schema" in p else p
        entry = {
            "name": name, "in": where,
            "description": _trim(p.get("description"), 200),
            "enum": [str(v) for v in (schema.get("enum") or [])][:_MAX_ENUM],
        }
        out.append({k: v for k, v in entry.items() if v})
        if len(out) >= _MAX_PARAMETERS:
            break
    return out


def _trim(value: Any, limit: int) -> Optional[str]:
    if value is None or value == "":
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[:limit] + "…"

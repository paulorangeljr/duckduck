"""
LLM-drafted semantic catalog.

Two passes, so it scales past a handful of tables:

1. **Per source** — profile the table through DuckAPI (columns, dtypes,
   a few sample rows, the connector's own description) and ask the LLM
   to describe it: field meanings, semantic types, roles, enumerated
   values, which entities/activities it covers.
2. **Across sources** — show the LLM every drafted source (fields +
   semantic types only) and ask for the shared vocabulary: entity and
   activity definitions with keywords, and the relationships (joins)
   between sources.

The draft is then sanitized deterministically against reality (a field
must be an actual column, a relationship must reference real fields, an
entity/activity a source claims must be defined...), every drop recorded
in ``GenerationResult.warnings``, and validated as a ``Catalog``. Output
is YAML for a person to review and commit — generation is a starting
point, not a replacement for review.

Privacy: sample rows are sent to the LLM. ``sample_rows=0`` sends only
column names and types.
"""

import datetime as _dt
import fnmatch
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from ..kinds import CATALOG, TABLE, TABLE_FUNCTION, kind_of, lists_of, required_params
from ..local_files import table_name_for
from .catalog import Catalog, FieldType
from .llm import LLMClient

logger = logging.getLogger("duckduck.semantic.generation")

DEFAULT_SOURCE_PROMPT = """\
You document one table of a security data platform for a semantic catalog
that a query planner reasons over. You receive the table's columns with
their types, a few sample rows, and any notes from its owner.

Describe the table and every column worth querying:
- description: one or two plain sentences on what a row represents.
- fields: use the exact column names. For each, pick the storage type,
  a short description, and a semantic_type saying what the value means
  (reuse common names: user, host, ip_address, domain, url, email,
  event_time, file, process, ...; null when none applies). Set role when
  the table has several fields of one semantic type (e.g. source vs
  destination IP). Use match "contains" for values people refer to by
  fragment (domains, URLs, titles), else "eq". For low-cardinality
  status-like columns, list the stored values with the words people use
  for them (e.g. DENY -> denied, blocked).
- time_field: the column holding when the row happened, if any.
- entities: the kinds of things this table can answer "which X?" about.
  Include "event" if rows are individual events/records.
- activities: short snake_case names for what the rows record happening
  (e.g. web_access, authentication, network_connection).
- examples: two or three realistic questions this table answers.
"""

DEFAULT_LINK_PROMPT = """\
You receive the drafted sources of a semantic catalog: each with its
fields, semantic types, and the entity and activity names it claims.

Define the shared vocabulary and the joins:
- entities: define every entity name used by any source, with a
  description and the keywords/synonyms people use for it in questions
  (plural forms included). Mark row_level true only for the entity that
  means "the records themselves" (e.g. event).
- activities: define every activity name used by any source, with a
  description, the verbs/nouns people use for it, the semantic_type of
  the thing the activity is about (resource), and the roles of the
  resource and of the actor fields.
- relationships: pairs of fields in *different* sources that hold the
  same value and can be joined (same_entity / references /
  parent_child), each with a confidence in [0, 1] reflecting how sure
  you are they truly match. Only propose joins you'd trust.
"""


# ---------------------------------------------------------------------------
# LLM output shapes (lists instead of free-form dicts: schema-friendly)
# ---------------------------------------------------------------------------


class GenEnumValue(BaseModel):
    stored: str
    synonyms: List[str] = Field(default_factory=list)


class GenField(BaseModel):
    name: str
    type: FieldType = "string"
    description: str = ""
    semantic_type: Optional[str] = None
    role: Optional[str] = None
    match: Optional[Literal["eq", "contains"]] = None
    values: List[GenEnumValue] = Field(default_factory=list)


class GenSource(BaseModel):
    description: str
    fields: List[GenField]
    time_field: Optional[str] = None
    entities: List[str] = Field(default_factory=list)
    activities: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)


class GenEntity(BaseModel):
    name: str
    description: str = ""
    keywords: List[str] = Field(default_factory=list)
    row_level: bool = False


class GenActivity(BaseModel):
    name: str
    description: str = ""
    keywords: List[str] = Field(default_factory=list)
    resource: Optional[str] = None
    resource_role: Optional[str] = None
    actor_role: Optional[str] = None


class GenRelationship(BaseModel):
    from_field: str = Field(description="source.field")
    to_field: str = Field(description="source.field in another source")
    type: Literal["same_entity", "references", "parent_child"] = "same_entity"
    confidence: float


class GenVocabulary(BaseModel):
    entities: List[GenEntity] = Field(default_factory=list)
    activities: List[GenActivity] = Field(default_factory=list)
    relationships: List[GenRelationship] = Field(default_factory=list)


# ---------------------------------------------------------------------------


class TableSpec(BaseModel):
    """One table to catalog: its source name, the DuckAPI table, and structural args."""

    name: str
    table: str
    args: Dict[str, Any] = Field(default_factory=dict)
    #: Free-text context from whoever owns the table — passed to the LLM.
    notes: str = ""


@dataclass
class GenerationResult:
    catalog: Catalog
    warnings: List[str] = field(default_factory=list)
    #: Where the YAML was written, once it has been.
    path: Optional[str] = None

    def to_yaml(self) -> str:
        import yaml

        data = self.catalog.model_dump(by_alias=True, exclude_defaults=True)
        header = (
            f"# Semantic catalog drafted by an LLM on {_dt.date.today().isoformat()}.\n"
            "# Review every source, field and relationship before relying on it.\n"
        )
        if self.warnings:
            header += "# Generation notes:\n" + "".join(f"#   - {w}\n" for w in self.warnings)
        return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_yaml())
        self.path = path

    def summary(self) -> str:
        """What ``python -m duckduck.semantic generate-catalog`` prints."""
        where = f"wrote {self.path}: " if self.path else ""
        lines = [f"{where}{len(self.catalog.sources)} sources, {len(self.catalog.relationships)} relationships"]
        lines += [f"  note: {w}" for w in self.warnings]
        return "\n".join(lines)


class CatalogGenerator:
    def __init__(
        self,
        llm: LLMClient,
        duck: Any,
        source_prompt: str = DEFAULT_SOURCE_PROMPT,
        link_prompt: str = DEFAULT_LINK_PROMPT,
        sample_rows: int = 5,
        discover: bool = True,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        max_tables: int = 50,
        link_llm: Optional[LLMClient] = None,
    ):
        self.llm = llm
        #: The final vocabulary/joins call (one call, over every table) — ``llm`` when omitted.
        self.link_llm = link_llm or llm
        self.duck = duck
        self.source_prompt = source_prompt
        self.link_prompt = link_prompt
        self.sample_rows = sample_rows
        self.discover = discover
        self.include = list(include or [])
        self.exclude = list(exclude or [])
        self.max_tables = max_tables

    # ------------------------------------------------------------------

    def default_specs(self) -> List[TableSpec]:
        """The tables ``generate()`` drafts when none are given (see ``plan_specs``)."""
        return self.plan_specs()[0]

    def plan_specs(self) -> Tuple[List[TableSpec], List[str]]:
        """
        What to draft when no explicit table list is given, plus notes:

        1. every plain data table (``kind == "table"``) — catalogs and raw
           queries are never data to describe;
        2. every table *behind* a connector, discovered through its catalog:
           a catalog declaring ``lists=`` (``glue_tables`` → ``glue_table``,
           ``adx_tables`` → ``adx_table``, ``<db>_tables`` → ``<db>_table``)
           is called, and each row becomes a call of that table function,
           its required arguments taken from the row's columns.

        ``include`` / ``exclude`` (fnmatch patterns, case-insensitive) match
        the registered name for plain tables, or the joined arguments for
        discovered ones (``security.proxy_logs`` for Glue, ``ProxyLogs`` for
        ADX); ``max_tables`` caps the total — each table is an LLM call.
        """
        notes: List[str] = []
        candidates: List[Tuple[str, TableSpec]] = []  # (pattern label, spec)
        undiscoverable: List[str] = []

        for name, fn in self.duck.functions.items():
            kind = kind_of(fn)
            if kind == TABLE:
                candidates.append((name, TableSpec(name=name, table=name)))
            elif kind == TABLE_FUNCTION and not self._has_catalog(fn):
                undiscoverable.append(name)

        if self.discover:
            for catalog_name, catalog_fn in self.duck.functions.items():
                target_method = lists_of(catalog_fn)
                if kind_of(catalog_fn) != CATALOG or not target_method:
                    continue
                target_name = self._sibling_name(catalog_fn, target_method)
                if target_name is None:
                    notes.append(f"catalog '{catalog_name}' lists '{target_method}', which isn't registered — skipped")
                    continue
                required = [p.name for p in required_params(self.duck.functions[target_name])]
                try:
                    listing = self.duck.fetch(catalog_name)
                except Exception as exc:
                    notes.append(f"catalog '{catalog_name}' failed ({exc.__class__.__name__}: {exc}) — its tables skipped")
                    continue
                missing = [r for r in required if r not in listing.columns]
                if missing:
                    notes.append(f"catalog '{catalog_name}' has no column(s) {missing} for '{target_name}' — skipped")
                    continue
                for row in listing[required].to_dict(orient="records"):
                    args = {r: row[r] for r in required}
                    label = ".".join(str(v) for v in args.values())
                    candidates.append((label, TableSpec(name=table_name_for(label), table=target_name, args=args)))

        chosen = [(label, spec) for label, spec in candidates if self._wanted(label)]
        if len(chosen) > self.max_tables:
            notes.append(
                f"{len(chosen)} tables matched; drafting only the first {self.max_tables} "
                f"(raise max_tables or narrow include/exclude)"
            )
            chosen = chosen[: self.max_tables]
        if undiscoverable:
            notes.append(
                "table functions with no catalog to discover their arguments were skipped: "
                + ", ".join(undiscoverable) + " — list them in catalog_generation.tables with their args"
            )

        specs: List[TableSpec] = []
        used: Dict[str, int] = {}
        for _, spec in chosen:
            base = spec.name
            used[base] = used.get(base, 0) + 1
            if used[base] > 1:  # two sources discovered under the same name
                spec = spec.model_copy(update={"name": f"{base}_{used[base]}"})
            specs.append(spec)
        return specs, notes

    def _wanted(self, label: str) -> bool:
        low = label.lower()
        if self.include and not any(fnmatch.fnmatch(low, p.lower()) for p in self.include):
            return False
        return not any(fnmatch.fnmatch(low, p.lower()) for p in self.exclude)

    def _has_catalog(self, fn: Any) -> bool:
        owner = getattr(fn, "__self__", None)
        return owner is not None and any(
            getattr(c, "__self__", None) is owner and lists_of(c) == getattr(fn, "__name__", None)
            for c in self.duck.functions.values()
        )

    def _sibling_name(self, catalog_fn: Any, method: str) -> Optional[str]:
        """The registered name of ``method`` on the same connector instance as ``catalog_fn``."""
        owner = getattr(catalog_fn, "__self__", None)
        for name, fn in self.duck.functions.items():
            if owner is not None and getattr(fn, "__self__", None) is owner and getattr(fn, "__name__", None) == method:
                return name
        return None

    def profile(self, spec: TableSpec) -> Dict[str, Any]:
        """Columns, dtypes, sample rows and connector description for one table."""
        fn = self.duck.functions.get(spec.table.lower())
        if fn is None:
            raise KeyError(f"table '{spec.table}' isn't registered in DuckAPI")
        kwargs = dict(spec.args)
        if "limit" in inspect.signature(fn).parameters:
            kwargs["limit"] = max(self.sample_rows, 1)
        df = self.duck.fetch(spec.table, **kwargs)
        rows = []
        if self.sample_rows > 0 and not df.empty:
            sample = df.head(self.sample_rows).astype(str).apply(lambda col: col.str.slice(0, 120))
            rows = sample.to_dict(orient="records")
        described = self.duck.list_tables().set_index("name")
        meta = described.loc[spec.table.lower()] if spec.table.lower() in described.index else None
        return {
            "source_name": spec.name,
            "connector": None if meta is None else meta["source"],
            "connector_description": None if meta is None else meta["description"],
            "columns": {c: str(t) for c, t in df.dtypes.items()},
            "sample_rows": rows,
            "owner_notes": spec.notes,
        }

    def generate(self, specs: Optional[List[TableSpec]] = None) -> GenerationResult:
        warnings: List[str] = []
        if specs is None:
            specs, notes = self.plan_specs()
            warnings.extend(notes)
        if not specs:
            raise ValueError(
                "nothing to catalog: no table specs given, no plain tables registered, and no catalog "
                "discovered any table" + (f" ({'; '.join(warnings)})" if warnings else "")
            )
        drafts: Dict[str, GenSource] = {}
        columns: Dict[str, List[str]] = {}
        for spec in specs:
            try:
                profile = self.profile(spec)
            except Exception as exc:
                warnings.append(f"skipped '{spec.name}': couldn't profile it ({exc})")
                continue
            columns[spec.name] = list(profile["columns"])
            prompt = "Table profile:\n" + json.dumps(profile, indent=1, default=str)
            drafts[spec.name] = self.llm.generate(self.source_prompt, prompt, GenSource)
            logger.info("drafted source %s", spec.name)
        if not drafts:
            raise ValueError("no source could be drafted:\n  - " + "\n  - ".join(warnings))

        overview = {
            name: {
                "fields": {f.name: f.semantic_type for f in d.fields},
                "roles": {f.name: f.role for f in d.fields if f.role},
                "entities": d.entities,
                "activities": d.activities,
            }
            for name, d in drafts.items()
        }
        vocab = self.link_llm.generate(self.link_prompt, "Drafted sources:\n" + json.dumps(overview, indent=1), GenVocabulary)

        by_name = {s.name: s for s in specs}
        catalog = self._assemble(drafts, vocab, columns, by_name, warnings)
        return GenerationResult(catalog=catalog, warnings=warnings)

    # ------------------------------------------------------------------

    def _assemble(self, drafts, vocab: GenVocabulary, columns, specs, warnings: List[str]) -> Catalog:
        entities = {
            e.name: {"description": e.description, "keywords": e.keywords, "row_level": e.row_level}
            for e in vocab.entities if _is_identifier(e.name)
        }
        semantic_types = {f.semantic_type for d in drafts.values() for f in d.fields if f.semantic_type}
        activities = {}
        for a in vocab.activities:
            if not _is_identifier(a.name):
                continue
            resource = a.resource
            if resource and resource not in semantic_types:
                warnings.append(f"activity '{a.name}': resource '{resource}' matches no field — dropped it")
                resource = None
            activities[a.name] = {
                "description": a.description, "keywords": a.keywords, "resource": resource,
                "resource_role": a.resource_role, "actor_role": a.actor_role,
            }

        sources = {}
        for name, draft in drafts.items():
            fields = {}
            for f in draft.fields:
                if f.name not in columns[name] or not _is_identifier(f.name):
                    warnings.append(f"{name}: field '{f.name}' isn't a column of the table — dropped")
                    continue
                fields[f.name] = {
                    "type": f.type, "description": f.description, "semantic_type": f.semantic_type,
                    "role": f.role, "match": f.match,
                    "values": {v.stored: v.synonyms for v in f.values},
                }
            if not fields:
                warnings.append(f"{name}: no usable fields — source dropped")
                continue
            time_field = draft.time_field
            if time_field and (time_field not in fields or fields[time_field]["type"] not in ("datetime", "date")):
                warnings.append(f"{name}: time_field '{time_field}' isn't a datetime field — dropped")
                time_field = None
            for kind, known, claimed in (("entity", entities, draft.entities), ("activity", activities, draft.activities)):
                for item in claimed:
                    if item not in known:
                        warnings.append(f"{name}: {kind} '{item}' was never defined — dropped")
            spec = specs[name]
            sources[name] = {
                "table": spec.table, "args": spec.args, "description": draft.description,
                "entities": [e for e in draft.entities if e in entities],
                "activities": [a for a in draft.activities if a in activities],
                "time_field": time_field, "examples": draft.examples, "fields": fields,
            }

        relationships = []
        for r in vocab.relationships:
            ok = all(
                "." in ref and ref.split(".")[0] in sources and ref.split(".")[1] in sources[ref.split(".")[0]]["fields"]
                for ref in (r.from_field, r.to_field)
            )
            if not ok or r.from_field.split(".")[0] == r.to_field.split(".")[0]:
                warnings.append(f"relationship {r.from_field} -> {r.to_field}: not two real fields of different sources — dropped")
                continue
            relationships.append({
                "from": r.from_field, "to": r.to_field, "type": r.type,
                "confidence": min(max(r.confidence, 0.0), 1.0),
            })

        # An activity may have lost its resource's only field along with a
        # dropped source; re-check before validating.
        live_types = {f["semantic_type"] for s in sources.values() for f in s["fields"].values()}
        for name, a in activities.items():
            if a["resource"] and a["resource"] not in live_types:
                warnings.append(f"activity '{name}': resource '{a['resource']}' no longer matches a field — dropped it")
                a["resource"] = None

        return Catalog.model_validate({
            "sources": sources, "entities": entities, "activities": activities, "relationships": relationships,
        })


def _is_identifier(name: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""))

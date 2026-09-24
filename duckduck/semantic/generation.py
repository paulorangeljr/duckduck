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
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

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
    ):
        self.llm = llm
        self.duck = duck
        self.source_prompt = source_prompt
        self.link_prompt = link_prompt
        self.sample_rows = sample_rows

    # ------------------------------------------------------------------

    def default_specs(self) -> List[TableSpec]:
        """Every registered table callable without structural args."""
        specs = []
        for name, fn in self.duck.functions.items():
            required = [
                p for p in inspect.signature(fn).parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
            ]
            if not required:
                specs.append(TableSpec(name=name, table=name))
        return specs

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
        specs = specs if specs is not None else self.default_specs()
        if not specs:
            raise ValueError("nothing to catalog: no table specs and no registered table callable without args")
        warnings: List[str] = []
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
        vocab = self.llm.generate(self.link_prompt, "Drafted sources:\n" + json.dumps(overview, indent=1), GenVocabulary)

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

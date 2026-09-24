"""
Semantic Catalog — the source of truth the whole semantic layer reasons
over: which sources exist, what their fields mean, which entities and
activities they cover, and how they relate to each other.

Maintained by hand in YAML (or JSON) for now; ``Catalog.load(path)``
parses and validates it (structure via Pydantic, cross-references via
``Catalog._check_references``) so a typo fails at load time, not in the
middle of planning a query.

Physical binding: every source points at something DuckDB can scan —
either a table registered in ``DuckAPI`` (``table:`` — any connector from
``auto_register()``, i.e. the virtualization layer) or a native DuckDB
relation expression (``relation:`` — e.g. ``read_parquet('s3://...')`` or
an ``ATTACH``-ed database's table). ``relation`` is spliced into SQL
verbatim, so the catalog must stay admin-maintained (never user input).
"""

import json
import os
from typing import Any, Dict, Iterator, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

#: SQL-safe identifier. Every name the compiler emits as an identifier
#: goes through this pattern — the first line of defense against
#: injection through the catalog or a plan.
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]

FieldType = Literal["string", "integer", "float", "boolean", "datetime", "date"]

#: Relationship types that mean "these two fields hold the same value" —
#: i.e. usable as an equality join.
JOINABLE_RELATIONSHIPS = frozenset({"same_entity", "references", "parent_child"})

RelationshipType = Literal[
    "same_entity", "represents", "references", "temporal_match", "parent_child", "derived_from"
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FieldDef(_Strict):
    type: FieldType = "string"
    #: What the value *means* (``user``, ``ip_address``, ``domain``,
    #: ``event_time``, ...). A field whose semantic_type equals an entity
    #: name represents that entity.
    semantic_type: Optional[str] = None
    description: str = ""
    #: Which side of an activity this field is on (``source`` /
    #: ``destination`` / ``actor`` ...) — disambiguates e.g. src_ip vs
    #: dst_ip, both ``ip_address``. Matched against an activity's
    #: ``resource_role`` / ``actor_role``.
    role: Optional[str] = None
    #: Physical column name in the scanned result, when it differs from
    #: the logical field name (after DuckAPI's ``.`` → ``_`` normalization).
    column: Optional[Identifier] = None
    #: API keyword argument that filters this field with *equality*
    #: server-side. Defaults to the physical column name when the
    #: registered function's signature accepts it.
    param: Optional[Identifier] = None
    #: How a free-text value extracted from the question is matched
    #: against this field. Default ``eq``; use ``contains`` for fields
    #: people refer to by fragment (domains, URLs, titles).
    match: Optional[Literal["eq", "contains"]] = None
    #: Enumerated values: ``{stored_value: [synonyms people say]}`` —
    #: e.g. ``{"DENY": ["denied", "blocked"]}``. Lets the extractor turn
    #: "denied connections" into ``action = 'DENY'`` deterministically.
    values: Dict[str, List[str]] = Field(default_factory=dict)


class SourceDef(_Strict):
    #: Informational backend kind (``postgres``, ``servicenow``, ``parquet``...).
    type: Optional[str] = None
    #: Name of a table registered in DuckAPI.
    table: Optional[Identifier] = None
    #: Structural (URL/path-building) arguments for ``table``'s function.
    args: Dict[str, Any] = Field(default_factory=dict)
    #: Native DuckDB relation expression — alternative to ``table``.
    relation: Optional[str] = None
    description: str = ""
    entities: List[str] = Field(default_factory=list)
    activities: List[str] = Field(default_factory=list)
    fields: Dict[Identifier, FieldDef]
    time_field: Optional[Identifier] = None
    examples: List[str] = Field(default_factory=list)
    #: Security-sensitive source: its relevance must clear the stricter
    #: ``critical`` threshold before it's used automatically.
    critical: bool = False

    @model_validator(mode="after")
    def _check(self) -> "SourceDef":
        if bool(self.table) == bool(self.relation):
            raise ValueError("a source needs exactly one of 'table' or 'relation'")
        if self.args and not self.table:
            raise ValueError("'args' only applies to a 'table' source")
        if not self.fields:
            raise ValueError("a source needs at least one field")
        if self.time_field is not None:
            if self.time_field not in self.fields:
                raise ValueError(f"time_field '{self.time_field}' is not one of its fields")
            if self.fields[self.time_field].type not in ("datetime", "date"):
                raise ValueError(f"time_field '{self.time_field}' must be datetime/date")
        return self

    @property
    def resolved_time_field(self) -> Optional[str]:
        """``time_field``, or the first field whose semantic_type is ``event_time``."""
        if self.time_field:
            return self.time_field
        for name, f in self.fields.items():
            if f.semantic_type == "event_time" and f.type in ("datetime", "date"):
                return name
        return None

    def physical_column(self, field_name: str) -> str:
        return self.fields[field_name].column or field_name


class EntityDef(_Strict):
    description: str = ""
    keywords: List[str] = Field(default_factory=list)
    #: The entity *is* the rows themselves ("events", "connections") —
    #: asking for it selects the records, not a distinct list of a field.
    row_level: bool = False


class ActivityDef(_Strict):
    description: str = ""
    keywords: List[str] = Field(default_factory=list)
    #: Semantic type of the thing the activity is *about* ("accessed
    #: github" → domain). Free-text values in the question filter a field
    #: of this semantic type.
    resource: Optional[str] = None
    resource_role: Optional[str] = None
    #: Role of the field identifying who/what performed the activity.
    actor_role: Optional[str] = None


class RelationshipDef(_Strict):
    from_: str = Field(alias="from")
    to: str
    type: RelationshipType
    confidence: float = Field(ge=0.0, le=1.0)


def split_ref(ref: str) -> Tuple[str, str]:
    """``"source.field"`` → ``("source", "field")``; ValueError otherwise."""
    parts = ref.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"'{ref}' is not a 'source.field' reference")
    return parts[0], parts[1]


class Catalog(_Strict):
    sources: Dict[Identifier, SourceDef]
    entities: Dict[Identifier, EntityDef] = Field(default_factory=dict)
    activities: Dict[Identifier, ActivityDef] = Field(default_factory=dict)
    relationships: List[RelationshipDef] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "Catalog":
        """Loads a catalog from a ``.yaml``/``.yml`` or ``.json`` file."""
        with open(path, "r", encoding="utf-8") as f:
            if os.path.splitext(path)[1].lower() == ".json":
                data = json.load(f)
            else:
                import yaml

                data = yaml.safe_load(f)
        return cls.model_validate(data or {})

    # ------------------------------------------------------------------
    # Validation of cross-references
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _check_references(self) -> "Catalog":
        issues = list(self._reference_issues())
        if issues:
            raise ValueError("invalid catalog:\n  - " + "\n  - ".join(issues))
        return self

    def _reference_issues(self) -> Iterator[str]:
        semantic_types = {
            f.semantic_type for s in self.sources.values() for f in s.fields.values()
        }
        for name, src in self.sources.items():
            for entity in src.entities:
                if self.entities and entity not in self.entities:
                    yield f"source '{name}': unknown entity '{entity}'"
            for activity in src.activities:
                if activity not in self.activities:
                    yield f"source '{name}': unknown activity '{activity}'"
        for name, act in self.activities.items():
            if act.resource and act.resource not in semantic_types:
                yield f"activity '{name}': no field has semantic_type '{act.resource}'"
        for i, rel in enumerate(self.relationships):
            where = f"relationship #{i} ({rel.from_} -> {rel.to})"
            if not self.has_field(rel.from_):
                yield f"{where}: '{rel.from_}' is not an existing source.field"
            if rel.type in JOINABLE_RELATIONSHIPS:
                if not self.has_field(rel.to):
                    yield f"{where}: a '{rel.type}' relationship needs a source.field on both sides"
                elif rel.from_.split(".")[0] == rel.to.split(".")[0]:
                    yield f"{where}: self-joins are not supported"
            elif rel.type == "represents":
                if rel.to not in self.entities and not self.has_field(rel.to):
                    yield f"{where}: '{rel.to}' is neither an entity nor a source.field"

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def has_field(self, ref: str) -> bool:
        try:
            source, field = split_ref(ref)
        except ValueError:
            return False
        return source in self.sources and field in self.sources[source].fields

    def field(self, ref: str) -> FieldDef:
        source, field = split_ref(ref)
        return self.sources[source].fields[field]

    def fields_by_semantic_type(self, source: str, semantic_type: str) -> List[str]:
        """Field names in ``source`` whose semantic_type is ``semantic_type``, in catalog order."""
        return [
            name
            for name, f in self.sources[source].fields.items()
            if f.semantic_type == semantic_type
        ]

    def entity_fields(self, entity: str) -> Dict[str, float]:
        """
        Every ``source.field`` that represents ``entity`` → confidence:
        fields whose semantic_type is the entity (1.0) plus explicit
        ``represents`` relationships (their own confidence).
        """
        found: Dict[str, float] = {}
        for name, src in self.sources.items():
            for field_name in self.fields_by_semantic_type(name, entity):
                found[f"{name}.{field_name}"] = 1.0
        for rel in self.relationships:
            if rel.type == "represents" and rel.to == entity:
                found[rel.from_] = max(found.get(rel.from_, 0.0), rel.confidence)
        return found

    def describe_source(self, name: str) -> str:
        """One-paragraph natural-language description of a source (fed to the decision engine)."""
        src = self.sources[name]
        fields = ", ".join(
            f"{fname} ({f.semantic_type or f.type}{': ' + f.description if f.description else ''})"
            for fname, f in src.fields.items()
        )
        parts = [f"{name}: {src.description.strip()}", f"Fields: {fields}."]
        if src.entities:
            parts.append("Entities: " + "; ".join(
                self._with_keywords(e, self.entities.get(e)) for e in src.entities
            ) + ".")
        if src.activities:
            parts.append("Activities: " + "; ".join(
                self._with_keywords(a, self.activities.get(a)) for a in src.activities
            ) + ".")
        return " ".join(parts)

    @staticmethod
    def _with_keywords(name: str, definition: Optional[BaseModel]) -> str:
        keywords = getattr(definition, "keywords", None)
        return f"{name} ({', '.join(keywords)})" if keywords else name

    def source_texts(self, name: str) -> List[str]:
        """All catalog text attached to a source (for lexical matching/indexing)."""
        src = self.sources[name]
        texts = [name, src.description, *src.examples]
        for fname, f in src.fields.items():
            texts += [fname, f.description, f.semantic_type or ""]
        for entity in src.entities:
            texts.append(entity)
            if entity in self.entities:
                texts += [self.entities[entity].description, *self.entities[entity].keywords]
        for activity in src.activities:
            act = self.activities[activity]
            texts += [activity, act.description, *act.keywords]
        return texts

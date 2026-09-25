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
in ``GenerationResult.warnings``, and validated as a ``Catalog``.

**Incremental.** Given the ``existing`` catalog, only what needs it is
drafted: tables not in it yet, generated sources older than ``max_age``,
and whatever ``force`` names (``True`` → every generated source). Sources
without ``generated_at`` were written by hand and are kept as they are
unless forced by name. The result is the existing catalog with those
sources replaced/added, each stamped ``generated_at``/``generated_by``;
existing entity/activity definitions win over the LLM's (they may have
been edited), new ones are added, and relationships are kept while
their fields still exist. Nothing to draft → no LLM call at all.

Privacy: sample rows are sent to the LLM. ``sample_rows=0`` sends only
column names and types.
"""

import datetime as _dt
import fnmatch
import re
import time
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field

from ..logs import human_seconds
from ..kinds import CATALOG, TABLE, TABLE_FUNCTION, kind_of, lists_of, required_params
from ..local_files import table_name_for
from .catalog import Catalog, FieldType
from .llm import LLMClient
from .profiling import IDENTIFIER_TYPES, SENSITIVE_TYPES, profile_frame

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
- entities: the kinds of things this table can answer "which X?" about —
  only ones one of its fields holds (that field's semantic_type is the
  entity's name). If people say "hosts" but the table identifies them by
  IP, the entity is ip_address, not host.
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
  means "the records themselves" (e.g. event). Every entity must be held
  by some field (a field whose semantic_type is its name): when people's
  word for something is held by a field of another type — hosts known by
  their IP — don't define a separate entity; put the word in that
  entity's keywords instead (ip_address: host, hosts, machine).
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
    #: Sources drafted in this run → why (``new``, ``forced``, ``older than 7d``).
    drafted: Dict[str, str] = field(default_factory=dict)
    #: Sources taken as they were from the existing catalog.
    kept: List[str] = field(default_factory=list)
    #: Sources that needed drafting but went over ``max_tables`` — next run.
    deferred: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.drafted)

    def to_yaml(self) -> str:
        import yaml

        data = _with_note_slots(self.catalog.model_dump(by_alias=True, exclude_defaults=True))
        header = (
            "# Semantic catalog. Sources with generated_at are maintained by generate-catalog\n"
            "# (redrafted when older than max_age, or when forced); sources without it are\n"
            "# hand-written and left alone. Review drafted sources before relying on them.\n"
            "# Write your observations in `notes` (on any source or field): the LLM never\n"
            "# writes them, they survive every redraft and are given to the LLM as context.\n"
            "# Comments starting with # are NOT kept — the file is rewritten.\n"
            f"# Last generation run: {_dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')}\n"
        )
        if self.warnings:
            header += "# Notes from that run:\n" + "".join(f"#   - {w}\n" for w in self.warnings)
        class _NoAliases(yaml.SafeDumper):  # shared values (one generated_at) stay literal, not &id001 / *id001
            def ignore_aliases(self, data):
                return True

        return header + yaml.dump(data, Dumper=_NoAliases, sort_keys=False, allow_unicode=True)

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_yaml())
        self.path = path

    def summary(self) -> str:
        """What ``python -m duckduck.semantic generate-catalog`` prints."""
        if not self.drafted:
            lines = [f"up to date: {len(self.catalog.sources)} sources, nothing to redraft"]
        else:
            where = f"wrote {self.path}: " if self.path else ""
            lines = [f"{where}{len(self.catalog.sources)} sources, {len(self.catalog.relationships)} relationships"]
            lines += [f"  drafted {name} ({why})" for name, why in self.drafted.items()]
            if self.kept:
                lines.append(f"  kept {len(self.kept)} as they were")
        if self.deferred:
            lines.append(f"  deferred to the next run (max_tables): {', '.join(self.deferred)}")
        lines += [f"  note: {w}" for w in self.warnings]
        return "\n".join(lines)


def _with_note(entry: Dict[str, Any]) -> Dict[str, Any]:
    """``entry`` with a ``notes`` key right after ``description`` (empty when unset)."""
    note = entry.get("notes", "")
    out: Dict[str, Any] = {}
    placed = False
    for key, value in entry.items():
        if key == "notes":
            continue
        out[key] = value
        if key == "description":
            out["notes"] = note
            placed = True
    if not placed:
        out = {"notes": note, **out}
    return out


def _with_note_slots(data: Dict[str, Any]) -> Dict[str, Any]:
    """Every source and field shows a ``notes`` slot in the YAML, so it's obvious where yours go."""
    sources = {}
    for name, source in data.get("sources", {}).items():
        source = _with_note(source)
        source["fields"] = {f: _with_note(d) for f, d in source.get("fields", {}).items()}
        sources[name] = source
    return {**data, "sources": sources}


def parse_age(value: Union[int, float, str, _dt.timedelta, None]) -> Optional[_dt.timedelta]:
    """``"7d"`` / ``"12h"`` / ``"30m"`` / ``"45s"`` / ``"2w"`` / seconds → ``timedelta``."""
    if value is None or isinstance(value, _dt.timedelta):
        return value
    if isinstance(value, (int, float)):
        return _dt.timedelta(seconds=value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*", str(value).lower())
    if not m:
        raise ValueError(f"max_age {value!r}: use a number of seconds or '<n>s|m|h|d|w' (e.g. '7d', '12h')")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)]
    return _dt.timedelta(**{unit: float(m.group(1))})


def _selectors(value: Union[bool, str, List[str], None]) -> List[str]:
    if value in (None, False, True, "", []):
        return []
    return [value] if isinstance(value, str) else [str(v) for v in value]


def _debug_json(label: str, payload: Any) -> None:
    if logger.isEnabledFor(logging.DEBUG):
        text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        logger.debug("    %s: %s", label, text if len(text) <= 4000 else text[:4000] + "…")


def _progress(started: float, done: int, total: int) -> str:
    elapsed = time.perf_counter() - started
    text = f"{human_seconds(elapsed)} elapsed"
    if done < total:
        text += f" · ~{human_seconds(elapsed / done * (total - done))} left"
    return text


def _age_text(delta: _dt.timedelta) -> str:
    seconds = int(delta.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _utc(moment: _dt.datetime) -> _dt.datetime:
    return moment.replace(tzinfo=_dt.timezone.utc) if moment.tzinfo is None else moment


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
        max_age: Union[int, str, _dt.timedelta, None] = None,
        llm_label: Optional[str] = None,
        clock: Optional[Callable[[], _dt.datetime]] = None,
        link_llm_label: Optional[str] = None,
        profile_rows: int = 1000,
        sample_values: int = 0,
        sample_sensitive: bool = False,
        max_enum_values: int = 20,
    ):
        self.llm = llm
        #: A generated source older than this is redrafted; ``None`` → never expires.
        self.max_age = parse_age(max_age)
        #: Stamped as each drafted source's ``generated_by``; also shown in verbose output.
        self.llm_label = llm_label
        self.link_llm_label = link_llm_label
        #: Rows read per table to compute each field's profile (0: no profiles).
        self.profile_rows = profile_rows
        #: Real example values stored per field in the catalog (0: none).
        self.sample_values = sample_values
        #: Store examples even for user/email fields.
        self.sample_sensitive = sample_sensitive
        #: A text column with up to this many distinct values becomes a value list.
        self.max_enum_values = max_enum_values
        self.clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
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
        ADX). ``max_tables`` isn't applied here: it caps how many sources
        one ``generate()`` run drafts.
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
                logger.info("catalog: discovering tables through %s …", catalog_name)
                try:
                    listing = self.duck.fetch(catalog_name)
                except Exception as exc:
                    notes.append(f"catalog '{catalog_name}' failed ({exc.__class__.__name__}: {exc}) — its tables skipped")
                    logger.info("  %s failed: %s", catalog_name, exc)
                    continue
                logger.info("  %s: %d tables → %s(%s)", catalog_name, len(listing), target_name, ", ".join(required))
                missing = [r for r in required if r not in listing.columns]
                if missing:
                    notes.append(f"catalog '{catalog_name}' has no column(s) {missing} for '{target_name}' — skipped")
                    continue
                for row in listing[required].to_dict(orient="records"):
                    args = {r: row[r] for r in required}
                    label = ".".join(str(v) for v in args.values())
                    candidates.append((label, TableSpec(name=table_name_for(label), table=target_name, args=args)))

        chosen = [(label, spec) for label, spec in candidates if self._wanted(label)]
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

    def profile(self, spec: TableSpec, previous: Optional[Any] = None) -> Dict[str, Any]:
        """
        Columns, dtypes, sample rows and connector description for one
        table, plus the owner's notes: ``spec.notes`` and, when redrafting,
        the ``notes`` written on the source and its fields in the catalog.
        """
        fn = self.duck.functions.get(spec.table.lower())
        if fn is None:
            raise KeyError(f"table '{spec.table}' isn't registered in DuckAPI")
        kwargs = dict(spec.args)
        if "limit" in inspect.signature(fn).parameters:
            kwargs["limit"] = max(self.sample_rows, self.profile_rows, 1)
        df = self.duck.fetch(spec.table, **kwargs)
        rows = []
        if self.sample_rows > 0 and not df.empty:
            sample = df.head(self.sample_rows).astype(str).apply(lambda col: col.str.slice(0, 120))
            rows = sample.to_dict(orient="records")
        stats = profile_frame(df, self.max_enum_values, max(self.sample_values, 3)) if self.profile_rows else {}
        # for the LLM: counts and ranges; the value lists only when data values may be sent at all
        column_stats = {
            c: {k: v for k, v in st.items() if k != "examples" and (k != "values" or self.sample_rows > 0)}
            for c, st in stats.items()
        }
        described = self.duck.list_tables().set_index("name")
        meta = described.loc[spec.table.lower()] if spec.table.lower() in described.index else None
        return {
            "source_name": spec.name,
            "connector": None if meta is None else meta["source"],
            "connector_description": None if meta is None else meta["description"],
            "columns": {c: str(t) for c, t in df.dtypes.items()},
            "sample_rows": rows,
            "rows_profiled": len(df) if stats else 0,
            "column_stats": column_stats,
            "owner_notes": "\n".join(n for n in (spec.notes, getattr(previous, "notes", "")) if n),
            "field_notes": {f: d.notes for f, d in (previous.fields.items() if previous else ()) if d.notes},
            "_stats": stats,  # kept out of the prompt
        }

    def generate(
        self,
        specs: Optional[List[TableSpec]] = None,
        existing: Optional[Catalog] = None,
        force: Union[bool, str, List[str], None] = False,
        only: Union[str, List[str], None] = None,
    ) -> GenerationResult:
        """
        Drafts what needs drafting (see the module docstring) and returns
        the resulting catalog.

        Selectors (``force`` / ``only`` entries, fnmatch patterns,
        case-insensitive): ``"proxy_logs"`` matches a table by its catalog
        source name, registered table, joined args (``security.proxy_logs``)
        or any single arg (``security``), or a whole ``auto_register``
        service (``glue``); ``"glue:security.proxy_logs"`` /
        ``"glue:security.*"`` / ``"adx:Proxy*"`` — service, then table.

        - ``force=True`` → redraft every generated source (plus new ones);
          ``force=<selectors>`` → redraft exactly those, hand-written ones
          included, and nothing else this run.
        - ``only=<selectors>`` → the run only looks at those tables (new /
          expired / forced among them); everything else is kept as is.
        """
        warnings: List[str] = []
        if specs is None:
            specs, notes = self.plan_specs()
            warnings.extend(notes)
        specs = self._align_with(specs, existing)
        only_patterns = _selectors(only)
        if only_patterns:
            matched_only = set()
            specs = [s for s in specs if self._matches(s, only_patterns, matched_only)]
            warnings += [f"only: '{p}' matched no table" for p in only_patterns if p not in matched_only]
            logger.info("catalog: only %s → %d tables", ", ".join(only_patterns), len(specs))
        todo, kept = self._select(specs, existing, force, warnings)
        if not todo and existing is None:
            raise ValueError(
                "nothing to catalog: no table specs given, no plain tables registered, and no catalog "
                "discovered any table" + (f" ({'; '.join(warnings)})" if warnings else "")
            )
        deferred = [spec.name for spec, _ in todo[self.max_tables:]]
        if deferred:
            warnings.append(
                f"{len(todo)} sources needed drafting; drafted {self.max_tables} (max_tables), "
                f"the rest on the next run"
            )
        todo = todo[: self.max_tables]
        kept_names = [n for n in (existing.sources if existing else {}) if n not in {s.name for s, _ in todo}]
        self._log_plan(specs, todo, kept_names, deferred)
        if not todo:
            return GenerationResult(catalog=existing, warnings=warnings, kept=kept_names, deferred=deferred)

        drafts: Dict[str, GenSource] = {}
        columns: Dict[str, List[str]] = {}
        stats: Dict[str, tuple] = {}
        reasons: Dict[str, str] = {}
        started = time.perf_counter()
        for i, (spec, reason) in enumerate(todo, 1):
            call = f"{spec.table}({', '.join(f'{k}={v!r}' for k, v in spec.args.items())})"
            logger.info("[%d/%d] %s (%s) — profiling %s", i, len(todo), spec.name, reason, call)
            try:
                profile = self.profile(spec, existing.sources.get(spec.name) if existing else None)
            except Exception as exc:
                warnings.append(f"skipped '{spec.name}': couldn't profile it ({exc})")
                logger.info("  skipped: couldn't profile it (%s)", exc)
                continue
            columns[spec.name] = list(profile["columns"])
            stats[spec.name] = (profile.pop("_stats"), profile["rows_profiled"])
            logger.info(
                "  %d columns, %d sample rows%s — asking %s", len(profile["columns"]), len(profile["sample_rows"]),
                ", with your notes" if profile["owner_notes"] or profile["field_notes"] else "", self.llm_label or "the LLM",
            )
            prompt = "Table profile:\n" + json.dumps(profile, indent=1, default=str)
            _debug_json("sent to the LLM", profile)
            drafts[spec.name] = draft = self.llm.generate(self.source_prompt, prompt, GenSource)
            _debug_json("draft", draft.model_dump_json())
            reasons[spec.name] = reason
            logger.info(
                "  → %d fields, entities %s, activities %s · %s",
                len(draft.fields), ", ".join(draft.entities) or "none", ", ".join(draft.activities) or "none",
                _progress(started, i, len(todo)),
            )
        if not drafts:
            if existing is not None:
                return GenerationResult(catalog=existing, warnings=warnings, kept=kept_names, deferred=deferred)
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
        if existing is not None:  # the rest of the catalog, so joins to it can be found too
            for name, src in existing.sources.items():
                if name not in drafts:
                    overview[name] = {
                        "fields": {f: d.semantic_type for f, d in src.fields.items()},
                        "roles": {f: d.role for f, d in src.fields.items() if d.role},
                        "entities": src.entities,
                        "activities": src.activities,
                    }
        prompt = "Drafted sources:\n" + json.dumps(overview, indent=1)
        if existing is not None and (existing.entities or existing.activities):
            prompt += "\n\nAlready defined — reuse these names rather than inventing synonyms:\n" + json.dumps(
                {"entities": sorted(existing.entities), "activities": sorted(existing.activities)}, indent=1
            )
        logger.info(
            "linking %d sources (%d drafted, %d kept) — asking %s for entities, activities and joins",
            len(overview), len(drafts), len(overview) - len(drafts), self.link_llm_label or self.llm_label or "the LLM",
        )
        link_started = time.perf_counter()
        _debug_json("sent to the LLM", prompt)
        vocab = self.link_llm.generate(self.link_prompt, prompt, GenVocabulary)
        _debug_json("vocabulary", vocab.model_dump_json())
        logger.info(
            "  → %d entities, %d activities, %d relationships (%s)", len(vocab.entities), len(vocab.activities),
            len(vocab.relationships), human_seconds(time.perf_counter() - link_started),
        )

        by_name = {s.name: s for s, _ in todo}
        stamp = {"generated_at": self.clock().replace(microsecond=0), "generated_by": self.llm_label}
        noted = len(warnings)
        catalog = self._assemble(drafts, vocab, columns, by_name, warnings, existing, stamp, stats)
        logger.info(
            "merged: %d sources, %d entities, %d activities, %d relationships · %s total",
            len(catalog.sources), len(catalog.entities), len(catalog.activities), len(catalog.relationships),
            human_seconds(time.perf_counter() - started),
        )
        for w in warnings[noted:]:
            logger.info("  dropped: %s", w)
        return GenerationResult(
            catalog=catalog, warnings=warnings, drafted=reasons,
            kept=[n for n in catalog.sources if n not in drafts], deferred=deferred,
        )

    def service_of(self, spec: TableSpec) -> Optional[str]:
        """The ``auto_register`` service that registered the spec's table, when known."""
        return getattr(self.duck, "service_of", {}).get(spec.table.lower())

    def _matches(self, spec: TableSpec, patterns: List[str], matched: set) -> bool:
        """Does ``spec`` match any selector (see ``generate``)? Records which patterns matched."""
        service = (self.service_of(spec) or "").lower()
        values = [str(v) for v in spec.args.values()]
        labels = {x.lower() for x in (spec.name, spec.table, ".".join(values), *values) if x}
        hit = False
        for p in patterns:
            low = p.lower()
            if ":" in low:
                svc_pat, tbl_pat = low.split(":", 1)
                ok = bool(service) and fnmatch.fnmatch(service, svc_pat) and any(fnmatch.fnmatch(l, tbl_pat) for l in labels)
            else:
                ok = any(fnmatch.fnmatch(l, low) for l in labels | ({service} if service else set()))
            if ok:
                matched.add(p)
                hit = True
        return hit

    def _apply_profile(self, field: Dict[str, Any], st: Optional[Dict[str, Any]]) -> None:
        """A field's computed profile, and its category values merged into its value list."""
        if not st:
            return
        if not field.get("semantic_type") and st.get("shape"):
            field["semantic_type"] = st["shape"]  # nearly every value is an IP / email / URL / domain
        profile = {k: st[k] for k in ("distinct", "null_ratio", "min", "max", "shape") if st.get(k) is not None}
        sensitive = field.get("semantic_type") in SENSITIVE_TYPES and not self.sample_sensitive
        if self.sample_values and not sensitive and st.get("examples"):
            profile["examples"] = st["examples"][: self.sample_values]
        if profile:
            field["profile"] = profile
        if (st.get("values") and field.get("type", "string") == "string" and field.get("match") != "contains"
                and field.get("semantic_type") not in IDENTIFIER_TYPES):
            values = field.setdefault("values", {})
            for v in st["values"]:
                values.setdefault(v, [])  # the LLM's synonyms stay; values it missed are added

    @staticmethod
    def _source_profile(fields: Dict[str, Any], time_field: Optional[str], stats: Optional[tuple]) -> Dict[str, Any]:
        if not stats or not stats[0]:
            return {}
        column_stats, rows = stats
        profile: Dict[str, Any] = {"rows_sampled": rows}
        tf = time_field or next((n for n, f in fields.items() if f.get("semantic_type") == "event_time"), None)
        st = column_stats.get(tf) if tf else None
        if st and st.get("min"):
            profile["time_min"], profile["time_max"] = st["min"], st["max"]
        return profile

    @staticmethod
    def _log_plan(specs, todo, kept_names, deferred) -> None:
        if not todo:
            logger.info("catalog: %d tables, all up to date — nothing to draft", len(specs))
            return
        kinds: Dict[str, int] = {}
        for _, why in todo:
            key = "expired" if "max_age" in why else why
            kinds[key] = kinds.get(key, 0) + 1
        logger.info(
            "catalog: %d tables — drafting %d (%s), keeping %d%s", len(specs), len(todo),
            ", ".join(f"{n} {k}" for k, n in kinds.items()), len(kept_names),
            f", deferring {len(deferred)} (max_tables)" if deferred else "",
        )

    def _align_with(self, specs: List[TableSpec], existing: Optional[Catalog]) -> List[TableSpec]:
        """A table already in the catalog under another name (hand-written) keeps that name."""
        if existing is None:
            return specs
        known = {(src.table, json.dumps(src.args, sort_keys=True, default=str)): name
                 for name, src in existing.sources.items() if src.table}
        aligned = []
        for spec in specs:
            name = known.get((spec.table, json.dumps(spec.args, sort_keys=True, default=str)))
            aligned.append(spec.model_copy(update={"name": name}) if name and name != spec.name else spec)
        return aligned

    def _select(self, specs, existing, force, warnings) -> Tuple[List[Tuple[TableSpec, str]], List[str]]:
        """(spec, reason) to draft — forced first, then new, then stale (oldest first) — and the kept names."""
        force_all = force is True
        patterns = [] if force in (None, False, True) else _selectors(force)
        matched = set()

        now = _utc(self.clock())
        forced, new, stale, kept = [], [], [], []
        for spec in specs:
            current = existing.sources.get(spec.name) if existing else None
            if patterns:  # forcing by name: exactly those, nothing else this run
                if self._matches(spec, patterns, matched):
                    forced.append((spec, "forced"))
                else:
                    kept.append(spec.name)
            elif current is None:
                new.append((spec, "new"))
            elif current.generated_at is None:
                kept.append(spec.name)  # hand-written
            elif force_all:
                forced.append((spec, "forced"))
            elif self.max_age is not None and now - _utc(current.generated_at) > self.max_age:
                age = now - _utc(current.generated_at)
                stale.append((spec, f"{_age_text(age)} old, max_age {_age_text(self.max_age)}", age))
            else:
                kept.append(spec.name)
        for p in patterns:
            if p not in matched:
                warnings.append(f"force: '{p}' matched no table")
        stale.sort(key=lambda item: item[2], reverse=True)
        return forced + new + [(spec, why) for spec, why, _ in stale], kept

    # ------------------------------------------------------------------

    def _assemble(self, drafts, vocab: GenVocabulary, columns, specs, warnings: List[str],
                  existing: Optional[Catalog] = None, stamp: Optional[Dict[str, Any]] = None,
                  stats: Optional[Dict[str, tuple]] = None) -> Catalog:
        base = existing.model_dump(by_alias=True, exclude_defaults=True) if existing else {}
        old_sources = {n: s for n, s in base.get("sources", {}).items() if n not in drafts}
        entities = {
            e.name: {"description": e.description, "keywords": e.keywords, "row_level": e.row_level}
            for e in vocab.entities if _is_identifier(e.name)
        }
        entities.update(base.get("entities", {}))  # existing definitions win — they may have been edited
        semantic_types = {f.semantic_type for d in drafts.values() for f in d.fields if f.semantic_type}
        semantic_types |= {f.get("semantic_type") for s in old_sources.values() for f in s["fields"].values()}
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
        activities.update(base.get("activities", {}))

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
                kept_note = base.get("sources", {}).get(name, {}).get("fields", {}).get(f.name, {}).get("notes")
                if kept_note:
                    fields[f.name]["notes"] = kept_note
                self._apply_profile(fields[f.name], (stats or {}).get(name, ({}, 0))[0].get(f.name))
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
            source_profile = self._source_profile(fields, time_field, (stats or {}).get(name))
            previous = base.get("sources", {}).get(name, {})
            for gone in set(previous.get("fields", {})) - set(fields):
                if previous["fields"][gone].get("notes"):
                    warnings.append(f"{name}: field '{gone}' is gone after the redraft, and its notes with it: "
                                    f"{previous['fields'][gone]['notes']!r}")
            sources[name] = {
                "table": spec.table, "args": spec.args, "description": draft.description,
                "entities": [e for e in draft.entities if e in entities],
                "activities": [a for a in draft.activities if a in activities],
                "time_field": time_field, "examples": draft.examples, "fields": fields,
                # what a person wrote survives the redraft
                **({"notes": previous["notes"]} if previous.get("notes") else {}),
                **({"critical": previous["critical"]} if previous.get("critical") else {}),
                **({"profile": source_profile} if source_profile else {}),
                **(stamp or {}),
            }
        # existing order first (redrafted sources in place), then the new ones
        merged = {n: sources.get(n, old_sources.get(n)) for n in base.get("sources", {}) if n in sources or n in old_sources}
        merged.update({n: s for n, s in sources.items() if n not in merged})
        sources = merged

        relationships, seen = [], set()
        for r in base.get("relationships", []):
            refs = (r["from"], r["to"])
            if all("." in ref and ref.split(".")[0] in sources and ref.split(".")[1] in sources[ref.split(".")[0]]["fields"]
                   for ref in refs):
                relationships.append(r)
                seen.add(frozenset(refs))
            else:
                warnings.append(f"relationship {refs[0]} -> {refs[1]}: a field no longer exists — dropped")
        for r in vocab.relationships:
            ok = all(
                "." in ref and ref.split(".")[0] in sources and ref.split(".")[1] in sources[ref.split(".")[0]]["fields"]
                for ref in (r.from_field, r.to_field)
            )
            if not ok or r.from_field.split(".")[0] == r.to_field.split(".")[0]:
                warnings.append(f"relationship {r.from_field} -> {r.to_field}: not two real fields of different sources — dropped")
                continue
            if frozenset((r.from_field, r.to_field)) in seen:
                continue  # already in the catalog
            seen.add(frozenset((r.from_field, r.to_field)))
            relationships.append({
                "from": r.from_field, "to": r.to_field, "type": r.type,
                "confidence": min(max(r.confidence, 0.0), 1.0),
            })

        # An activity may have lost its resource's only field along with a
        # dropped source; re-check before validating.
        live_types = {f.get("semantic_type") for s in sources.values() for f in s["fields"].values()}
        for name, a in activities.items():
            if a.get("resource") and a["resource"] not in live_types:  # kept ones may omit it (defaults aren't dumped)
                warnings.append(f"activity '{name}': resource '{a['resource']}' no longer matches a field — dropped it")
                a["resource"] = None

        _tidy_entities(sources, entities, relationships, warnings)
        return Catalog.model_validate({
            "sources": sources, "entities": entities, "activities": activities, "relationships": relationships,
        })


_MAX_KEYWORDS = 25


def _tidy_entities(sources: Dict[str, Any], entities: Dict[str, Any], relationships: List[Dict[str, Any]],
                   warnings: List[str]) -> None:
    """
    Deterministic clean-up of the vocabulary, over the whole catalog:

    - an entity **no field holds** (no field of its semantic_type, no
      ``represents`` link) can never be answered — it only makes questions
      ask back. It's merged into the entity the data does hold for it
      (named in its description, or held by the sources that claim it),
      its name and keywords becoming that entity's keywords
      ("host" → ip_address: host, hosts);
    - every entity gets the names of the fields that hold it as keywords
      ("src ip", "owner").
    """
    held: Dict[str, List[str]] = {}
    for sname, src in sources.items():
        for fname, f in src.get("fields", {}).items():
            if f.get("semantic_type"):
                held.setdefault(f["semantic_type"], []).append(fname)
    for r in relationships:
        if r.get("type") == "represents":
            held.setdefault(r["to"], []).append(r["from"].split(".")[-1])

    for name in list(entities):
        e = entities[name]
        if name in held or e.get("row_level"):
            continue
        target = _merge_target(name, e, entities, sources, held)
        if target is None:
            warnings.append(f"entity '{name}': no field holds it — questions about it will be asked back")
            continue
        words = [name.replace("_", " "), *(e.get("keywords") or [])]
        _add_keywords(entities[target], words)
        for src in sources.values():
            claimed = src.get("entities") or []
            if name in claimed:
                src["entities"] = list(dict.fromkeys(target if x == name else x for x in claimed))
        del entities[name]
        warnings.append(f"entity '{name}': no field holds it — merged into '{target}' "
                        f"(now a keyword of it: {', '.join(words[:4])})")

    for name, e in entities.items():
        _add_keywords(e, [f.replace("_", " ") for f in held.get(name, [])])


def _merge_target(name: str, entity: Dict[str, Any], entities: Dict[str, Any], sources: Dict[str, Any],
                  held: Dict[str, List[str]]) -> Optional[str]:
    """The held entity an orphan stands for: named in its description/keywords, else held where it's claimed."""
    text = " " + " ".join([entity.get("description") or "", *(entity.get("keywords") or [])]).lower() + " "
    scores: Dict[str, float] = {}
    for other, oe in entities.items():
        if other == name or other not in held or oe.get("row_level"):
            continue
        words = {other.replace("_", " "), *[k.lower() for k in (oe.get("keywords") or [])]}
        scores[other] = 2.0 * sum(1 for w in words if w and re.search(rf"\b{re.escape(w)}\b", text))
    for src in sources.values():
        if name not in (src.get("entities") or []):
            continue
        for f in src.get("fields", {}).values():
            if f.get("semantic_type") in scores:
                scores[f["semantic_type"]] += 1.0
    best = max(scores.items(), key=lambda kv: kv[1], default=(None, 0.0))
    return best[0] if best[1] > 0 else None


def _add_keywords(entity: Dict[str, Any], words: List[str]) -> None:
    keywords = list(entity.get("keywords") or [])
    seen = {k.lower() for k in keywords}
    for w in words:
        w = w.strip()
        if w and w.lower() not in seen and len(keywords) < _MAX_KEYWORDS:
            keywords.append(w)
            seen.add(w.lower())
    entity["keywords"] = keywords


def _is_identifier(name: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""))

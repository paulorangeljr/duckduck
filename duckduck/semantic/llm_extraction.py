"""
LLM-backed question interpretation: the *extraction* half of it (which
values the question mentions, of what type, over what time window).

Per the architecture, the LLM only extracts; the judgments (entity,
activity, source relevance...) stay with the decision engine (JEV), and
nothing the LLM says reaches SQL unchecked: an enumerated value must be a
stored value of a real catalog field, a semantic type must exist in the
catalog, a time range must parse. Anything else is dropped (and logged).

On any LLM failure it falls back to ``RuleBasedExtractor`` (with a
``RuntimeWarning``) unless ``on_error="raise"``. The rule-based pass
always runs anyway, for the lexical terms the baseline engine uses.

**Translation** (``translate=True``, the default): the same call also
returns the question read in English (``english_question``), so wording,
retrieval and value synonyms only need to exist in English. Values are
protected: IPs, e-mails, domains and quoted text are replaced by ``⟦n⟧``
placeholders before the LLM sees the question and put back after; every
other extracted value must appear in the original question and, verbatim,
in the translation. A translation that loses a placeholder or a value is
discarded (logged) and the question is read as asked. The rule-based pass
then runs over the English reading (terms, enumerated synonyms, time).

**Reading** (``extract(..., reading=True)``, the ``llm`` reader): the same
call also returns how it read the question — the kind of answer, what it is
about (an entity, a field, a table or a value) and the grouping — using only
names from the catalog it is shown; anything else is dropped. It is
evidence for the decision engine, never a decision (see ``Reading``).

**Cache**: a question read in the last ``cache_ttl`` seconds isn't sent
again — the page's preview while typing and the search that follows share
one call.
"""

import json
import logging
import threading
import time
import warnings
from collections import OrderedDict
from datetime import datetime
import re
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .catalog import Catalog
from .extraction import EnumMatch, ExtractedLiteral, Extraction, Reading, RuleBasedExtractor, TimeRange
from .llm import LLMClient

logger = logging.getLogger("duckduck.semantic.llm")

DEFAULT_EXTRACTION_PROMPT = """\
You extract filter values from questions asked of a security data platform.
You receive the question, the current UTC time, and a catalog summary: the
semantic types that fields carry, the enumerated fields with their stored
values and synonyms, and the entity/activity vocabulary.

Return:
- values: every concrete value the question filters on (a username, a
  hostname, an IP, a domain or fragment of one, a file name ...), exactly as
  written in the question, each with the catalog semantic type it belongs to
  (null if none fits). Never include words that name what is being asked for
  ("users", "hosts") or the activity ("accessed", "logged in").
- enum_values: words that correspond to a stored value of an enumerated
  field; use the exact source.field and the exact stored value from the
  catalog summary.
- time_range: the time window, as last_hours for relative windows ("last
  24hrs" -> 24) or start/end as ISO-8601 UTC timestamps for absolute ones;
  null if the question gives none.

Only use semantic types, fields and stored values that appear in the
catalog summary. When unsure, leave a value out rather than guessing.
"""


class LLMValue(BaseModel):
    value: str
    semantic_type: Optional[str] = None


class LLMEnumValue(BaseModel):
    field: str = Field(description="source.field from the catalog summary")
    value: str = Field(description="exact stored value")
    term: str = Field(description="the words in the question that matched")


class LLMTimeRange(BaseModel):
    last_hours: Optional[float] = None
    start: Optional[str] = None
    end: Optional[str] = None
    text: str = ""


#: Appended to the system prompt (yours included) when translating, unless it already asks for it.
TRANSLATION_INSTRUCTIONS = """
Also return english_question: the question in English — a faithful
translation if it is written in another language, or the question
unchanged if it is already English. Keep every ⟦n⟧ placeholder, and every
name, username, hostname, file name or other value, exactly as written:
never translate, respell or re-case them. Translate only the words around
them. The values you return stay as written in the original question.
"""


class LLMExtractionOutput(BaseModel):
    values: List[LLMValue] = Field(default_factory=list)
    enum_values: List[LLMEnumValue] = Field(default_factory=list)
    time_range: Optional[LLMTimeRange] = None
    english_question: str = ""


class LLMReading(BaseModel):
    answer: str = Field(description="one of the answer kinds listed")
    about_kind: str = Field(description="entity, field, table, value or none")
    about: str = Field(default="", description="the entity name, source.field, table name, or the value as written")
    group_by: str = Field(default="", description="source.field a count per group is grouped by, else empty")


class LLMExtractionWithReading(LLMExtractionOutput):
    reading: Optional[LLMReading] = None


#: Appended when the reading is asked for (``extract(..., reading=True)``); {kinds} = the answer kinds.
READING_INSTRUCTIONS = """
Also return reading: how you read the question, using only names from the
catalog summary.
- answer: the kind of answer asked for, one of:
{kinds}
- about_kind and about: what the answer is about —
  "entity" + an entity name when it lists or counts things of that kind
  ("which users…" → user); "field" + source.field when it asks for a
  column's values or counts them ("the different severities", "how many
  departments" → the field); "table" + a table name when it asks for a table
  itself ("show me table owners"); "value" + the value as written when it
  asks about a given value ("everything about 10.0.0.5"); "none" otherwise.
- group_by: source.field for a count per group ("per rule"), else empty.
"""


class LLMExtractor:
    def __init__(
        self,
        catalog: Catalog,
        llm: LLMClient,
        system_prompt: str = DEFAULT_EXTRACTION_PROMPT,
        on_error: str = "fallback",
        translate: bool = True,
        cache_ttl: float = 300.0,
    ):
        if on_error not in ("fallback", "raise"):
            raise ValueError("on_error must be 'fallback' or 'raise'")
        self.catalog = catalog
        self.llm = llm
        #: Read questions asked in other languages in English (see the module docstring).
        self.translate = translate
        if translate and "english_question" not in system_prompt:
            system_prompt = system_prompt.rstrip() + "\n" + TRANSLATION_INSTRUCTIONS
        self.system_prompt = system_prompt
        self.on_error = on_error
        self.rules = RuleBasedExtractor(catalog)
        self._summary = json.dumps(self._catalog_summary(catalog), indent=1, sort_keys=True)
        self._semantic_types = {
            f.semantic_type for s in catalog.sources.values() for f in s.fields.values() if f.semantic_type
        }
        from .shapes import ANSWER_SHAPES

        self._answer_kinds = [k for k in ANSWER_SHAPES]
        self._reading_prompt = READING_INSTRUCTIONS.format(
            kinds="\n".join(f"  {k}: {d}" for k, d in ANSWER_SHAPES.items()))
        self._tables = json.dumps({
            name: {"about": (src.description or "").strip().split(". ")[0], "fields": list(src.fields)}
            for name, src in catalog.sources.items()
        }, indent=1)
        self.cache_ttl = cache_ttl
        self._cache: "OrderedDict[Tuple[str, bool], Tuple[float, Extraction]]" = OrderedDict()
        self._cache_lock = threading.Lock()

    @staticmethod
    def _catalog_summary(catalog: Catalog) -> dict:
        semantic_types = sorted({
            f.semantic_type for s in catalog.sources.values() for f in s.fields.values()
            if f.semantic_type and f.semantic_type != "event_time"
        })
        enums = {
            f"{sname}.{fname}": {stored: syns for stored, syns in f.values.items()}
            for sname, src in catalog.sources.items() for fname, f in src.fields.items() if f.values
        }
        return {
            "semantic_types": semantic_types,
            "enumerated_fields": enums,
            "entities": {n: e.keywords for n, e in catalog.entities.items()},
            "activities": {n: a.keywords for n, a in catalog.activities.items()},
        }

    def extract(self, question: str, now: datetime, reading: bool = False) -> Extraction:
        key = (_normalized(question), reading)
        if self.cache_ttl > 0:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit is not None and time.monotonic() - hit[0] < self.cache_ttl:
                    self._cache.move_to_end(key)
                    logger.info("llm extraction: %r read %.0fs ago — reusing it", question, time.monotonic() - hit[0])
                    return hit[1].model_copy(deep=True)
        result = self._extract(question, now, reading)
        if self.cache_ttl > 0 and (not reading or result.reading is not None):  # a fallback isn't cached
            with self._cache_lock:
                self._cache[key] = (time.monotonic(), result.model_copy(deep=True))
                while len(self._cache) > 256:
                    self._cache.popitem(last=False)
        return result

    def _extract(self, question: str, now: datetime, reading: bool) -> Extraction:
        base = self.rules.extract(question, now)  # lexical terms + fallback
        masked, placeholders = _mask(question, base.literals) if self.translate else (question, {})
        prompt = (
            f"Current UTC time: {now.isoformat()}\n\n"
            f"Catalog summary:\n{self._summary}\n\n"
            + (f"Tables and their fields:\n{self._tables}\n\n" if reading else "")
            + f"Question: {masked}"
        )
        system = self.system_prompt + ("\n" + self._reading_prompt if reading else "")
        try:
            out = self.llm.generate(system, prompt, LLMExtractionWithReading if reading else LLMExtractionOutput)
        except Exception as exc:
            if self.on_error == "raise":
                raise
            with warnings.catch_warnings():
                warnings.simplefilter("always", RuntimeWarning)
                warnings.warn(
                    f"LLM extraction failed ({exc.__class__.__name__}: {exc}) — using rule-based extraction.",
                    RuntimeWarning, stacklevel=2,
                )
            return base
        for v in out.values:
            v.value = _unmask(v.value, placeholders)
        for e in out.enum_values:
            e.term = _unmask(e.term, placeholders)
        english = self._english(question, out, placeholders) if self.translate else None
        if english:
            logger.info("llm extraction: read as %r", english)
            base = self.rules.extract(english, now)  # terms, enum synonyms and time, in the catalog's language
        result = self._merge(base, out, now, question)
        result.english_question = english
        if reading:
            result.reading = self._reading(getattr(out, "reading", None), question, placeholders)
            logger.info("llm extraction: reading %s", result.reading.as_fact() if result.reading else None)
        return result

    def _reading(self, raw: Optional[LLMReading], question: str, placeholders: Dict[str, str]) -> Optional[Reading]:
        """The LLM's reading with every name checked against the catalog (what doesn't check out is dropped)."""
        if raw is None:
            return None
        answer = (raw.answer or "").strip().lower()
        kind = (raw.about_kind or "").strip().lower()
        about = _unmask((raw.about or "").strip(), placeholders)
        out = Reading(answer=answer if answer in self._answer_kinds else None)
        if kind == "entity":
            name = next((e for e in self.catalog.entities if e.lower() == about.lower()), None)
            if name:
                out.about_kind, out.about = "entity", name
        elif kind == "field":
            ref = self._field_ref(about)
            if ref:
                out.about_kind, out.about = "field", ref
        elif kind == "table":
            name = next((n for n, src in self.catalog.sources.items()
                         if about.lower() in (n.lower(), (src.table or "").lower())), None)
            if name:
                out.about_kind, out.about = "table", name
        elif kind == "value" and about and about.lower() in question.lower():
            out.about_kind, out.about = "value", about
        if kind and kind != "none" and out.about is None:
            logger.info("llm extraction: dropped reading about %s %r (not in the catalog/question)", kind, about)
        out.group_by = self._field_ref((raw.group_by or "").strip())
        if raw.answer and out.answer is None:
            logger.info("llm extraction: dropped unknown answer kind %r", raw.answer)
        return out if (out.answer or out.about or out.group_by) else None

    def _field_ref(self, text: str) -> Optional[str]:
        """``source.field`` as the catalog has it — or a bare field name that exists in exactly one table."""
        if not text:
            return None
        if "." in text:
            src, _, fld = text.partition(".")
            for name, source in self.catalog.sources.items():
                if name.lower() == src.lower():
                    match = next((f for f in source.fields if f.lower() == fld.lower()), None)
                    return f"{name}.{match}" if match else None
            return None
        hits = [f"{n}.{f}" for n, src in self.catalog.sources.items() for f in src.fields if f.lower() == text.lower()]
        return hits[0] if len(hits) == 1 else None

    def _english(self, question: str, out: LLMExtractionOutput, placeholders: Dict[str, str]) -> Optional[str]:
        """The validated English reading, or ``None`` (already English, empty, or it lost a value)."""
        text = (out.english_question or "").strip()
        if not text:
            return None
        missing = [p for p in placeholders if p not in text]
        text = _unmask(text, placeholders)
        lost = [v.value for v in out.values
                if v.value and v.value.lower() in question.lower() and v.value.lower() not in text.lower()]
        if missing or lost:
            logger.warning(
                "llm extraction: translation %r dropped %s — reading the question as asked", text,
                ", ".join(repr(placeholders[p]) for p in missing) or ", ".join(repr(v) for v in lost),
            )
            return None
        if _normalized(text) == _normalized(question):
            return None
        return text

    def _merge(self, base: Extraction, out: LLMExtractionOutput, now: datetime,
               question: Optional[str] = None) -> Extraction:
        result = base.model_copy(deep=True)

        # Shape-recognized literals (IPs, domains, e-mails) stay authoritative;
        # the LLM's values replace the rule-based free-text guesses.
        literals = [lit for lit in base.literals if lit.kind != "term"]
        seen = {lit.value.lower() for lit in literals}
        for v in out.values:
            value = v.value.strip()
            if not value or value.lower() in seen:
                continue
            if question is not None and value.lower() not in question.lower():
                logger.info("llm extraction: dropped value %r (not in the question as written)", value)
                continue
            sem_type = v.semantic_type if v.semantic_type in self._semantic_types else None
            if v.semantic_type and sem_type is None:
                logger.info("llm extraction: dropped unknown semantic_type %r for %r", v.semantic_type, value)
            literals.append(ExtractedLiteral(value=value, kind="term", text=value, semantic_type=sem_type))
            seen.add(value.lower())
        result.literals = literals

        enum_matches: List[EnumMatch] = []
        for e in out.enum_values:
            if not self.catalog.has_field(e.field) or e.value not in self.catalog.field(e.field).values:
                logger.info("llm extraction: dropped enum %s=%r (not in catalog)", e.field, e.value)
                continue
            enum_matches.append(EnumMatch(term=e.term or e.value, field=e.field, value=e.value))
        if enum_matches or not base.enum_matches:
            result.enum_matches = enum_matches

        tr = out.time_range
        if tr is not None:
            try:
                if tr.last_hours is not None and tr.last_hours > 0:
                    result.time_range = TimeRange(last_hours=tr.last_hours, text=tr.text)
                elif tr.start or tr.end:
                    start = _parse_ts(tr.start)
                    end = _parse_ts(tr.end)
                    if start and end and start >= end:
                        raise ValueError("start after end")
                    result.time_range = TimeRange(start=start, end=end, text=tr.text)
            except ValueError as exc:
                logger.info("llm extraction: dropped time range %r (%s)", tr, exc)
        return result


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is not None:
        from datetime import timezone

        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def _mask(question: str, literals: List[ExtractedLiteral]) -> "tuple[str, Dict[str, str]]":
    """IPs, e-mails, domains and quoted text → ``⟦n⟧``, so a translation can't touch them."""
    placeholders: Dict[str, str] = {}
    masked = question
    for lit in literals:
        if lit.kind == "term" or not lit.text or lit.text not in masked:
            continue
        key = f"⟦{len(placeholders) + 1}⟧"
        placeholders[key] = lit.text
        masked = masked.replace(lit.text, key)
    return masked, placeholders


def _unmask(text: str, placeholders: Dict[str, str]) -> str:
    for key, original in placeholders.items():
        text = text.replace(key, original)
    return text


def _normalized(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text).strip().lower()

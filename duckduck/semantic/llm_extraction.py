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
"""

import json
import logging
import warnings
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from .catalog import Catalog
from .extraction import EnumMatch, ExtractedLiteral, Extraction, RuleBasedExtractor, TimeRange
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


class LLMExtractionOutput(BaseModel):
    values: List[LLMValue] = Field(default_factory=list)
    enum_values: List[LLMEnumValue] = Field(default_factory=list)
    time_range: Optional[LLMTimeRange] = None


class LLMExtractor:
    def __init__(
        self,
        catalog: Catalog,
        llm: LLMClient,
        system_prompt: str = DEFAULT_EXTRACTION_PROMPT,
        on_error: str = "fallback",
    ):
        if on_error not in ("fallback", "raise"):
            raise ValueError("on_error must be 'fallback' or 'raise'")
        self.catalog = catalog
        self.llm = llm
        self.system_prompt = system_prompt
        self.on_error = on_error
        self.rules = RuleBasedExtractor(catalog)
        self._summary = json.dumps(self._catalog_summary(catalog), indent=1, sort_keys=True)
        self._semantic_types = {
            f.semantic_type for s in catalog.sources.values() for f in s.fields.values() if f.semantic_type
        }

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

    def extract(self, question: str, now: datetime) -> Extraction:
        base = self.rules.extract(question, now)  # lexical terms + fallback
        prompt = (
            f"Current UTC time: {now.isoformat()}\n\n"
            f"Catalog summary:\n{self._summary}\n\n"
            f"Question: {question}"
        )
        try:
            out = self.llm.generate(self.system_prompt, prompt, LLMExtractionOutput)
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
        return self._merge(base, out, now)

    def _merge(self, base: Extraction, out: LLMExtractionOutput, now: datetime) -> Extraction:
        result = base.model_copy(deep=True)

        # Shape-recognized literals (IPs, domains, e-mails) stay authoritative;
        # the LLM's values replace the rule-based free-text guesses.
        literals = [lit for lit in base.literals if lit.kind != "term"]
        seen = {lit.value.lower() for lit in literals}
        for v in out.values:
            value = v.value.strip()
            if not value or value.lower() in seen:
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

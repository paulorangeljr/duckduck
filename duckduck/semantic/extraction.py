"""
Deterministic value extraction — pulls the *values* out of the question
(time ranges, IPs, domains, e-mails, quoted strings, enumerated field
values, bare free-text terms) so the decision engine only has to make
*judgments*, never generate text.

``ValueExtractor`` is the protocol; ``RuleBasedExtractor`` is the
regex/catalog-driven implementation. An LLM-backed extractor can replace
it for phrasing the rules don't cover — the rest of the pipeline only
sees ``Extraction``.
"""

import ipaddress
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Protocol, Set, Tuple

from pydantic import BaseModel, Field

from .catalog import Catalog
from .text import STOPWORDS, stem, tokenize, vocabulary


class TimeRange(BaseModel):
    #: Relative window ending now ("last 24 hours").
    last_hours: Optional[float] = None
    #: Absolute bounds (``start`` inclusive, ``end`` exclusive), naive UTC.
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    text: str = ""


class ExtractedLiteral(BaseModel):
    value: str
    #: ``ip_address`` / ``domain`` / ``email`` (recognized by shape),
    #: ``quoted`` (explicitly quoted), or ``term`` (leftover free text).
    kind: str
    text: str
    #: Stem of the catalog word right before the value ("user *alice*" →
    #: "user") — usually names the value's type.
    hint: Optional[str] = None


class EnumMatch(BaseModel):
    #: The words in the question that matched.
    term: str
    #: ``source.field`` whose enumerated values contain it.
    field: str
    #: The stored value to filter on.
    value: str


class Extraction(BaseModel):
    time_range: Optional[TimeRange] = None
    literals: List[ExtractedLiteral] = Field(default_factory=list)
    enum_matches: List[EnumMatch] = Field(default_factory=list)
    #: Stemmed terms that mean something to the catalog (entity/activity
    #: keywords, field/source names...) — evidence for the decisions.
    terms: List[str] = Field(default_factory=list)
    #: The question's head noun ("*users* who ...", "show me *connections*")
    #: — the strongest evidence for which entity is being asked for.
    focus_terms: List[str] = Field(default_factory=list)


class ValueExtractor(Protocol):
    def extract(self, question: str, now: datetime) -> Extraction:
        ...


_UNIT_HOURS = {
    "minute": 1 / 60, "minutes": 1 / 60, "min": 1 / 60, "mins": 1 / 60,
    "hour": 1, "hours": 1, "hr": 1, "hrs": 1, "h": 1,
    "day": 24, "days": 24, "d": 24,
    "week": 168, "weeks": 168, "w": 168,
    "month": 720, "months": 720,
}
_RELATIVE_RE = re.compile(
    r"\b(?:(?:in|during|over|within|for)\s+)?(?:the\s+)?(?:last|past|previous)\s+"
    r"(\d+(?:\.\d+)?)?\s*(" + "|".join(sorted(_UNIT_HOURS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_DAY_RE = re.compile(r"\b(today|yesterday)\b", re.IGNORECASE)
_QUOTED_RE = re.compile(r"'([^']+)'|\"([^\"]+)\"")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}\b", re.IGNORECASE)

#: Extra words that are never a filter value on their own.
_NON_VALUE_WORDS = {"many", "much", "most", "least", "some", "who", "whose", "whom"}


def _shape(value: str) -> str:
    """Classifies a literal by its shape."""
    try:
        ipaddress.ip_address(value)
        return "ip_address"
    except ValueError:
        pass
    if _EMAIL_RE.fullmatch(value):
        return "email"
    if _DOMAIN_RE.fullmatch(value):
        return "domain"
    return "quoted"


class RuleBasedExtractor:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        # Stemmed-phrase → [(source.field, stored value)] for enumerated fields.
        self._enum_phrases: Dict[Tuple[str, ...], List[Tuple[str, str]]] = {}
        for sname, src in catalog.sources.items():
            for fname, f in src.fields.items():
                for stored, synonyms in f.values.items():
                    for phrase in {stored, *synonyms}:
                        key = tuple(stem(t) for t in tokenize(phrase))
                        if key:
                            self._enum_phrases.setdefault(key, []).append((f"{sname}.{fname}", stored))
        # Words the catalog gives meaning to (never mistaken for values).
        texts: List[str] = []
        for name, e in catalog.entities.items():
            texts += [name, e.description, *e.keywords]
        for name, a in catalog.activities.items():
            texts += [name, a.description, *a.keywords]
        for sname, src in catalog.sources.items():
            texts.append(sname)
            for fname, f in src.fields.items():
                texts += [fname, f.semantic_type or ""]
        self.vocabulary: Set[str] = vocabulary(texts)

    def extract(self, question: str, now: datetime) -> Extraction:
        out = Extraction()
        text = question

        def consume(match: "re.Match") -> str:
            return " " * (match.end() - match.start())

        # 1. time range
        m = _RELATIVE_RE.search(text)
        if m:
            amount = float(m.group(1)) if m.group(1) else 1.0
            out.time_range = TimeRange(
                last_hours=amount * _UNIT_HOURS[m.group(2).lower()], text=m.group(0).strip()
            )
            text = text[: m.start()] + consume(m) + text[m.end():]
        else:
            m = _DAY_RE.search(text)
            if m:
                midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if m.group(1).lower() == "today":
                    out.time_range = TimeRange(start=midnight, text=m.group(0))
                else:
                    out.time_range = TimeRange(start=midnight - timedelta(days=1), end=midnight, text=m.group(0))
                text = text[: m.start()] + consume(m) + text[m.end():]

        # 2. literals, most specific shape first
        def take(pattern: "re.Pattern", kind_of) -> None:
            nonlocal text
            for m in list(pattern.finditer(text)):
                raw = next((g for g in m.groups() if g), None) if m.groups() else m.group(0)
                if kind_of == "ip_address":
                    try:
                        ipaddress.ip_address(raw)
                    except ValueError:
                        continue
                kind = kind_of(raw) if callable(kind_of) else kind_of
                out.literals.append(ExtractedLiteral(value=raw, kind=kind, text=m.group(0)))
                text = text[: m.start()] + consume(m) + text[m.end():]

        take(_QUOTED_RE, _shape)
        take(_EMAIL_RE, "email")
        take(_IPV4_RE, "ip_address")
        take(_DOMAIN_RE, "domain")

        # 3. enumerated values, then catalog terms / leftover free text
        tokens = tokenize(text)
        stems = [stem(t) for t in tokens]
        consumed = [t in STOPWORDS for t in tokens]
        for phrase in sorted(self._enum_phrases, key=len, reverse=True):
            n = len(phrase)
            for i in range(len(stems) - n + 1):
                if tuple(stems[i:i + n]) == phrase and not any(consumed[i:i + n]):
                    term = " ".join(tokens[i:i + n])
                    for field_ref, stored in self._enum_phrases[phrase]:
                        out.enum_matches.append(EnumMatch(term=term, field=field_ref, value=stored))
                    for j in range(i, i + n):
                        consumed[j] = True

        # Consecutive unknown words form one phrase; remember the catalog
        # term right before each phrase as a type hint.
        phrases: List[Tuple[List[str], Optional[str]]] = []
        current: List[str] = []
        previous_term: Optional[str] = None
        for tok, st, used in zip(tokens, stems, consumed):
            known = not used and st in self.vocabulary
            is_value = not used and not known and tok not in _NON_VALUE_WORDS and not tok.isdigit()
            if not used and not out.focus_terms:
                out.focus_terms.append(st)
            if known:
                out.terms.append(st)
            if is_value:
                if not current:
                    phrases.append((current, previous_term))
                current.append(tok)
            else:
                current = []
            previous_term = st if known else None
        for words, hint in phrases:
            phrase = " ".join(words)
            out.literals.append(ExtractedLiteral(value=phrase, kind="term", text=phrase, hint=hint))
        # The focus word is the thing being asked for, never a filter value.
        out.literals = [
            lit for lit in out.literals
            if not (lit.kind == "term" and stem(lit.value) in out.focus_terms)
        ]
        return out

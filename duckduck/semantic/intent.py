"""Data models shared by the interpreter, planner and search result."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .extraction import EnumMatch, ExtractedLiteral, TimeRange


class Thresholds(BaseModel):
    """Minimum probability for each kind of decision to be acted on automatically."""

    source: float = 0.80
    field: float = 0.85
    relationship: float = 0.90
    #: Replaces ``source`` for sources flagged ``critical: true``.
    critical: float = 0.98
    entity: float = 0.60
    activity: float = 0.60
    resource_type: float = 0.60
    #: Which kind of answer (list / count / different values / count per group), when the wording is ambiguous.
    answer_shape: float = 0.60
    #: "Is this about the data at all?" below this probability → a direct out-of-scope reply
    #: (low on purpose: in doubt, the question goes through the normal path).
    out_of_scope: float = 0.20
    #: A free-text reply to a clarification must pick one option this surely.
    reply: float = 0.70


class DecisionRecord(BaseModel):
    """One judgment made while answering a question — the audit trail."""

    #: ``source_relevance`` / ``entity`` / ``activity`` / ``resource_type``
    #: / ``field_relevance`` / ``relationship_relevance``.
    kind: str
    question: str
    subject: str = ""
    answer: Any
    probability: float
    threshold: Optional[float] = None
    #: ``engine`` (asked the decision engine), ``deterministic`` (decided
    #: by the extractor/catalog without asking) or ``user`` (pinned by an
    #: answer to a clarification).
    decided_by: str = "engine"
    alternatives: List[tuple] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.threshold is None or self.probability >= self.threshold


class ScoredSource(BaseModel):
    source: str
    confidence: float
    retrieval_score: float = 0.0


class ResourceFilter(BaseModel):
    """A value from the question to filter on, typed semantically."""

    type: str
    value: str
    confidence: float
    literal_kind: str


from .shapes import ACROSS_SHAPES, ANSWER_SHAPES, FIELD_SHAPES  # noqa: E402,F401  (re-exported)


class SemanticIntent(BaseModel):
    question: str
    #: The question read in English (``LLMExtractor``), when it was asked in another language —
    #: what the catalog's English wording, keywords and synonyms are matched against.
    english_question: Optional[str] = None
    #: What kind of answer — which SQL shape (see ``ANSWER_SHAPES``).
    answer_shape: str = "list"
    #: For ``catalog``: which part of it — tables / entities / activities / fields / relationships.
    catalog_topic: Optional[str] = None
    #: For ``small_talk``: greeting / thanks / goodbye.
    small_talk: Optional[str] = None
    #: For ``browse``: the catalog source the question names ("show me table owners") and how many
    #: rows it asked for ("the first 20 rows"), if it said.
    browse_source: Optional[str] = None
    row_limit: Optional[int] = None
    target_entity: Optional[str] = None
    target_confidence: float = 0.0
    activity: Optional[str] = None
    activity_confidence: float = 0.0
    resources: List[ResourceFilter] = Field(default_factory=list)
    value_filters: List[EnumMatch] = Field(default_factory=list)
    time_range: Optional[TimeRange] = None
    candidate_sources: List[ScoredSource] = Field(default_factory=list)
    literals: List[ExtractedLiteral] = Field(default_factory=list)
    #: Similar questions users confirmed or corrected before (``CaseMemory``) — evidence the
    #: decisions were given, never a rule.
    similar_cases: List[Dict[str, Any]] = Field(default_factory=list)

    @property
    def working_question(self) -> str:
        """What the pipeline reads: the English reading when there is one, else the question as asked."""
        return self.english_question or self.question

    def decision_state(self, **kwargs: Any):
        """A ``DecisionState`` over this question — the English reading, plus the original when translated."""
        from .decisions import DecisionState

        return DecisionState(query=self.working_question,
                             original_query=self.question if self.english_question else None, **kwargs)


class ClarificationOption(BaseModel):
    """One answer to a clarification, and the decisions choosing it pins."""

    value: str
    label: str
    #: What choosing it does, in plain words (yes/no questions).
    detail: str = ""
    #: Decision key → value, passed back as ``search(..., pinned=...)``:
    #: ``entity``, ``activity``, ``value:<literal>``, ``value_term``,
    #: ``source:<name>``, ``field:<source.field>``, ``join:<a.x>=<b.y>``.
    pins: Dict[str, Any] = Field(default_factory=dict)


class Clarification(BaseModel):
    """What to ask the user back — built from templates and the catalog, no LLM."""

    #: ``entity`` / ``value_type`` / ``value_term`` / ``source`` / ``field`` /
    #: ``join`` — or ``unresolvable`` (no answer would help: rephrase the
    #: question or fix the catalog), which has no options.
    kind: str
    question: str
    #: Why it's being asked, in plain words.
    context: str = ""
    options: List[ClarificationOption] = Field(default_factory=list)

    def option(self, reply: str) -> Optional[ClarificationOption]:
        """The option a reply names exactly: its number (1-based), value or label."""
        text = reply.strip().lower().rstrip(".)")
        if text.isdigit() and 1 <= int(text) <= len(self.options):
            return self.options[int(text) - 1]
        for opt in self.options:
            if text in (opt.value.lower(), opt.label.lower()):
                return opt
        return None

    def render(self) -> str:
        """The question, why it's asked, and the numbered options — ready to show."""
        lines = [self.question]
        if self.context:
            lines.append(self.context)
        lines += [f"  {i}) {o.label}" + (f" — {o.detail}" if o.detail else "") for i, o in enumerate(self.options, 1)]
        return "\n".join(lines)


class ClarificationNeeded(Exception):
    """
    A decision didn't clear its confidence threshold — the question must
    be clarified (or routed to a fallback) rather than executed on a guess.
    """

    def __init__(self, reason: str, decision: Optional[DecisionRecord] = None, options: Optional[List[str]] = None,
                 followup: Optional["Clarification"] = None):
        super().__init__(reason)
        self.reason = reason
        self.decision = decision
        self.options = options or []
        #: What to ask the user back; ``None`` → ``unresolvable``.
        self.followup = followup

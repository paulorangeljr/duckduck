"""Data models shared by the interpreter, planner and search result."""

from typing import Any, List, Optional

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
    #: ``engine`` (asked the decision engine) or ``deterministic``
    #: (decided by the extractor/catalog without asking).
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


class SemanticIntent(BaseModel):
    question: str
    target_entity: Optional[str] = None
    target_confidence: float = 0.0
    activity: Optional[str] = None
    activity_confidence: float = 0.0
    resources: List[ResourceFilter] = Field(default_factory=list)
    value_filters: List[EnumMatch] = Field(default_factory=list)
    time_range: Optional[TimeRange] = None
    candidate_sources: List[ScoredSource] = Field(default_factory=list)
    literals: List[ExtractedLiteral] = Field(default_factory=list)


class ClarificationNeeded(Exception):
    """
    A decision didn't clear its confidence threshold — the question must
    be clarified (or routed to a fallback) rather than executed on a guess.
    """

    def __init__(self, reason: str, decision: Optional[DecisionRecord] = None, options: Optional[List[str]] = None):
        super().__init__(reason)
        self.reason = reason
        self.decision = decision
        self.options = options or []

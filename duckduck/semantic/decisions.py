"""
Semantic decision engines — the "JEV" layer.

JEV answers *small, typed* questions about the user's query, each with a
probability: "is this source relevant?" (binary), "which entity is being
asked for?" (choice). It never writes SQL/KQL — the planner combines its
independent judgments deterministically.

Everything downstream talks to the ``DecisionEngine`` protocol:

- ``JEVAdapter`` wraps a real JEV client (anything implementing
  ``JEVBackend`` — two methods, ``decide``/``classify``) and adds retries,
  a timeout, probability normalization and logging.
- ``LexicalDecisionEngine`` is a deterministic, offline baseline (token
  overlap against catalog text) so the pipeline runs end to end — tests,
  demos, and before JEV is wired in. For structural questions the
  planner already has a deterministic ``prior`` for (field/relationship
  relevance), it just returns that prior.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .text import vocabulary

logger = logging.getLogger("duckduck.semantic.jev")


class DecisionState(BaseModel):
    """What a decision engine sees about the query for one decision."""

    query: str
    #: Stemmed content terms relevant to this decision — a hint for
    #: lexical engines; a real model should read ``query`` itself.
    terms: List[str] = Field(default_factory=list)
    #: Structured context (candidate kind, semantic types, ...).
    facts: Dict[str, Any] = Field(default_factory=dict)
    #: Deterministic prior probability, when the caller has one.
    prior: Optional[float] = None


class BinaryDecision(BaseModel):
    question: str
    subject: str
    answer: bool
    #: P(yes).
    probability: float


class Classification(BaseModel):
    question: str
    #: Option label → probability (sums to 1).
    probabilities: Dict[str, float]

    @property
    def choice(self) -> str:
        return max(self.probabilities, key=self.probabilities.__getitem__)

    @property
    def probability(self) -> float:
        return self.probabilities[self.choice]

    def ranked(self) -> List[tuple]:
        return sorted(self.probabilities.items(), key=lambda kv: kv[1], reverse=True)


@runtime_checkable
class DecisionEngine(Protocol):
    def decide(self, state: DecisionState, question: str, subject: str) -> BinaryDecision:
        """Yes/no judgment about ``subject`` (a natural-language description)."""

    def classify(
        self, state: DecisionState, question: str, options: Mapping[str, str]
    ) -> Classification:
        """Pick among ``options`` (label → natural-language description)."""


class DecisionEngineError(RuntimeError):
    """The decision engine failed (after retries) or returned nothing usable."""


# ---------------------------------------------------------------------------
# Probability normalization
# ---------------------------------------------------------------------------


def normalize_probabilities(raw: Mapping[str, Any], labels: List[str]) -> Dict[str, float]:
    """
    Maps a backend's raw scores onto ``labels`` (case-insensitive), clamps
    negatives/garbage to 0 and rescales to sum 1. All-zero → uniform.
    """
    lookup = {str(k).strip().lower(): v for k, v in (raw or {}).items()}
    scores: Dict[str, float] = {}
    for label in labels:
        try:
            value = float(lookup.get(label.lower(), 0.0))
        except (TypeError, ValueError):
            value = 0.0
        scores[label] = value if value > 0 and value == value else 0.0  # drops NaN too
    total = sum(scores.values())
    if total <= 0:
        return {label: 1.0 / len(labels) for label in labels}
    return {label: v / total for label, v in scores.items()}


# ---------------------------------------------------------------------------
# JEV adapter
# ---------------------------------------------------------------------------


class JEVBackend(Protocol):
    """
    The raw JEV client contract — the only thing to implement to plug the
    real service in. ``state`` is a JSON-serializable dict (the
    ``DecisionState`` plus ``subject`` or ``options`` descriptions);
    return a score per option label (any scale — the adapter normalizes).
    """

    def decide(self, state: Dict[str, Any], question: str, options: List[str]) -> Mapping[str, float]:
        ...

    def classify(self, state: Dict[str, Any], question: str, options: List[str]) -> Mapping[str, float]:
        ...


class JEVAdapter:
    """
    ``DecisionEngine`` on top of a ``JEVBackend``: retries with backoff,
    per-call timeout, normalized probabilities, one log line per decision.

    The timeout abandons the call (the worker thread can't be killed), so
    the backend client should also have its own network timeout.
    """

    YES, NO = "YES", "NO"

    def __init__(
        self,
        backend: JEVBackend,
        retries: int = 2,
        timeout: float = 10.0,
        backoff: float = 0.5,
    ):
        self.backend = backend
        self.retries = retries
        self.timeout = timeout
        self.backoff = backoff
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jev")

    def decide(self, state: DecisionState, question: str, subject: str) -> BinaryDecision:
        payload = {**state.model_dump(), "subject": subject}
        raw = self._call(self.backend.decide, payload, question, [self.YES, self.NO])
        p_yes = normalize_probabilities(raw, [self.YES, self.NO])[self.YES]
        logger.info("jev.decide %r on %r -> P(yes)=%.3f", question, subject[:80], p_yes)
        return BinaryDecision(question=question, subject=subject, answer=p_yes >= 0.5, probability=p_yes)

    def classify(
        self, state: DecisionState, question: str, options: Mapping[str, str]
    ) -> Classification:
        labels = list(options)
        payload = {**state.model_dump(), "options": dict(options)}
        raw = self._call(self.backend.classify, payload, question, labels)
        result = Classification(question=question, probabilities=normalize_probabilities(raw, labels))
        logger.info("jev.classify %r -> %s (%.3f)", question, result.choice, result.probability)
        return result

    def _call(self, fn: Callable, payload: Dict[str, Any], question: str, labels: List[str]):
        last_error: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            try:
                return self._pool.submit(fn, payload, question, labels).result(timeout=self.timeout)
            except FutureTimeout as exc:
                last_error = exc
                logger.warning("jev call timed out after %.1fs (attempt %d)", self.timeout, attempt + 1)
            except Exception as exc:  # backend/network error
                last_error = exc
                logger.warning("jev call failed (attempt %d): %s", attempt + 1, exc)
            if attempt < self.retries:
                time.sleep(self.backoff * (2 ** attempt))
        raise DecisionEngineError(
            f"JEV failed after {self.retries + 1} attempts on {question!r}: {last_error!r}"
        ) from last_error


# ---------------------------------------------------------------------------
# Lexical baseline
# ---------------------------------------------------------------------------


class LexicalDecisionEngine:
    """
    Deterministic offline stand-in for JEV.

    - ``decide``: returns ``state.prior`` when given; otherwise the share of
      ``state.terms`` found in the subject's vocabulary, mapped to
      ``[0.05, 0.95]``.
    - ``classify``: counts ``state.terms`` hits in each option's label +
      description, with additive smoothing — one clear hit among a few
      options lands around 0.85; no hits at all is a uniform (i.e.
      low-confidence) distribution.
    """

    def __init__(self, smoothing: float = 0.05):
        self.smoothing = smoothing

    def decide(self, state: DecisionState, question: str, subject: str) -> BinaryDecision:
        if state.prior is not None:
            p = state.prior
        elif not state.terms:
            p = 0.5
        else:
            vocab = vocabulary([subject])
            coverage = sum(1 for t in state.terms if t in vocab) / len(state.terms)
            p = 0.05 + 0.9 * coverage
        return BinaryDecision(question=question, subject=subject, answer=p >= 0.5, probability=p)

    def classify(
        self, state: DecisionState, question: str, options: Mapping[str, str]
    ) -> Classification:
        scores = {}
        for label, description in options.items():
            vocab = vocabulary([label.replace("_", " "), description])
            scores[label] = sum(1 for t in state.terms if t in vocab) + self.smoothing
        return Classification(
            question=question, probabilities=normalize_probabilities(scores, list(options))
        )

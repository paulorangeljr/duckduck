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

**Batches.** Independent questions about one user question go out
together: ``ask_all(engine, [Ask, ...])`` uses the engine's
``ask_batch`` when it has one (``JEVAdapter``: one Decisions API request
for every question — Jev answers them in parallel, each unaware of the
others) and falls back to one ``decide``/``classify`` per question
otherwise. Yes/no questions can carry ``criteria`` (what counts as
``true`` and as ``false``) — ``CRITERIA`` has the pipeline's.
"""

import json
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Union, runtime_checkable

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
    #: The question as the user wrote it, when ``query`` is its English reading.
    original_query: Optional[str] = None
    #: For a choice: the option to assume when there's no evidence either way — used by the
    #: offline lexical engine only (a real engine reads the question; never sent to it).
    default: Optional[str] = None


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


#: What counts as yes / no for the pipeline's yes/no questions — sent as a
#: noul question's ``criteria`` (a definition beats a bare "relevant?").
CRITERIA: Dict[str, Dict[str, str]] = {
    "source": {
        "true": "This source holds records that answer the question, or it is needed to connect those "
                "records to something the question asks for (such as who owns a host found elsewhere).",
        "false": "Nothing the question asks about, filters on, or needs to connect is in this source.",
    },
    "field": {
        "true": "Filtering or returning this field gives exactly what the question means.",
        "false": "What the question means belongs in a different field, or this field means something else.",
    },
    "relationship": {
        "true": "Joining these two fields connects records about the same real thing, and the answer "
                "needs that connection.",
        "false": "The join would connect unrelated records, or the answer doesn't need it.",
    },
}


class Ask(BaseModel):
    """One question in a batch: yes/no (``subject``) or pick-one (``options``)."""

    key: str
    question: str
    state: DecisionState
    subject: Optional[str] = None
    options: Optional[Dict[str, str]] = None
    #: Yes/no only: ``{"true": ..., "false": ...}``.
    criteria: Optional[Dict[str, str]] = None

    @property
    def is_choice(self) -> bool:
        return self.options is not None


Answer = Union[BinaryDecision, Classification]


def ask_all(engine: Any, asks: List[Ask]) -> Dict[str, Answer]:
    """Every question answered — in one batch when the engine supports it."""
    if not asks:
        return {}
    batch = getattr(engine, "ask_batch", None)
    if batch is not None:
        return batch(asks)
    return {
        a.key: engine.classify(a.state, a.question, a.options) if a.is_choice
        else engine.decide(a.state, a.question, a.subject or "")
        for a in asks
    }


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

    # Optional — mixed batches in one request (``JEVAdapter.ask_batch`` uses it when present):
    # def ask(self, state: Dict[str, Any], questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    #     questions: key → {"type": "noul" | "choice", "instructions", "criteria"}; the state's
    #     ``items[key]`` holds each question's subject/facts/prior. Returns key → P(yes) (noul) or
    #     {option: score} (choice).


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
        cache_size: int = 512,
    ):
        self.backend = backend
        self.retries = retries
        self.timeout = timeout
        self.backoff = backoff
        #: Answers to questions already asked (same question, subject/options, criteria and
        #: state) — asking twice in a session doesn't cost a second call. 0 disables it.
        self.cache_size = cache_size
        self._cache: "OrderedDict[str, Answer]" = OrderedDict()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jev")

    def ask_batch(self, asks: List[Ask]) -> Dict[str, Answer]:
        """
        Every question in one backend request (``backend.ask``), answers
        from the cache first. Backends without ``ask`` get one
        ``decide``/``classify`` per question.
        """
        backend_ask = getattr(self.backend, "ask", None)
        out: Dict[str, Answer] = {}
        pending: List[Ask] = []
        for a in asks:
            hit = self._cache.get(self._cache_key(a)) if self.cache_size else None
            if hit is not None:
                out[a.key] = hit.model_copy(update={"question": a.question})
                logger.info("jev.cached %r → %s", a.question, _brief(hit))
            else:
                pending.append(a)
        if pending and backend_ask is None:
            for a in pending:
                out[a.key] = self.classify(a.state, a.question, a.options) if a.is_choice \
                    else self.decide(a.state, a.question, a.subject or "")
        elif pending:
            payload = {"query": pending[0].state.query, "items": {a.key: _item(a) for a in pending}}
            if pending[0].state.original_query:
                payload["original_query"] = pending[0].state.original_query
            spec = {a.key: _question_spec(a) for a in pending}
            started = time.perf_counter()
            raw = self._call(lambda p, q, labels: backend_ask(p, spec), payload,
                             f"{len(pending)} questions", [])
            logger.info("jev.batch: %d questions in one request (%.2fs)", len(pending), time.perf_counter() - started)
            for a in pending:
                answer = self._answer(a, raw.get(a.key))
                out[a.key] = answer
                logger.info("jev.%s %r%s → %s", "classify" if a.is_choice else "decide", a.question,
                            f" on {a.subject[:60]!r}" if a.subject else "", _brief(answer))
                self._remember(a, answer)
        return {a.key: out[a.key] for a in asks}

    def _answer(self, a: Ask, raw: Any) -> Answer:
        if a.is_choice:
            labels = list(a.options)
            return Classification(question=a.question, probabilities=normalize_probabilities(raw or {}, labels))
        try:
            p = min(max(float(raw), 0.0), 1.0)
        except (TypeError, ValueError):
            raise DecisionEngineError(f"no yes/no probability for {a.question!r} in the batch answer: {raw!r}")
        return BinaryDecision(question=a.question, subject=a.subject or "", answer=p >= 0.5, probability=p)

    @staticmethod
    def _cache_key(a: Ask) -> str:
        return json.dumps(
            {"q": a.question, "s": a.subject, "o": a.options, "c": a.criteria,
             "state": a.state.model_dump(exclude={"terms"})},
            sort_keys=True, default=str,
        )

    def _remember(self, a: Ask, answer: Answer) -> None:
        if not self.cache_size:
            return
        self._cache[self._cache_key(a)] = answer
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

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

    def decide_many(
        self, state: DecisionState, question: str, subjects: Mapping[str, str]
    ) -> Dict[str, BinaryDecision]:
        """
        The same yes/no ``question`` about several subjects. One request when
        the backend has ``decide_batch`` (Jev's multi-question calls), else
        one ``decide`` per subject.
        """
        batch = getattr(self.backend, "decide_batch", None)
        if batch is None or not subjects:
            return {name: self.decide(state, question, subject) for name, subject in subjects.items()}
        keys = {f"q{i}": name for i, name in enumerate(subjects)}
        payload = {**state.model_dump(), "candidates": dict(subjects)}
        texts = {key: f"{question} Candidate: '{name}' (described in candidates.{name})." for key, name in keys.items()}
        raw = self._call(lambda p, q, labels: batch(p, texts), payload, question, list(keys))
        out = {}
        for key, name in keys.items():
            p = min(max(float(raw.get(key, 0.5)), 0.0), 1.0)
            out[name] = BinaryDecision(question=question, subject=subjects[name], answer=p >= 0.5, probability=p)
            logger.info("jev.decide_many %r on %r -> P(yes)=%.3f", question, name, p)
        return out

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
                if not getattr(exc, "retryable", True):
                    break  # bad key / bad request: retrying can't help
            if attempt < self.retries:
                time.sleep(self.backoff * (2 ** attempt))
        raise DecisionEngineError(
            f"JEV failed after {attempt + 1} attempt(s) on {question!r}: {last_error!r}"
        ) from last_error


def _item(a: Ask) -> Dict[str, Any]:
    """What the state carries for one batched question (under ``items[key]``)."""
    item: Dict[str, Any] = {}
    if a.subject:
        item["subject"] = a.subject
    if a.state.facts:
        item["facts"] = a.state.facts
    if a.state.prior is not None:
        item["catalog_prior_probability"] = a.state.prior
    return item


def _question_spec(a: Ask) -> Dict[str, Any]:
    if a.is_choice:
        text = a.question + (f" (context: items.{a.key})" if _item(a) else "")
        return {"type": "choice", "instructions": text, "criteria": dict(a.options)}
    spec: Dict[str, Any] = {"type": "noul", "instructions": f"{a.question} Judge items.{a.key}."}
    if a.criteria:
        spec["criteria"] = dict(a.criteria)
    return spec


def _brief(answer: Answer) -> str:
    if isinstance(answer, Classification):
        return f"{answer.choice} ({answer.probability:.3f})"
    return f"P(yes)={answer.probability:.3f}"


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

    def decide_many(
        self, state: DecisionState, question: str, subjects: Mapping[str, str]
    ) -> Dict[str, BinaryDecision]:
        return {name: self.decide(state, question, subject) for name, subject in subjects.items()}

    def classify(
        self, state: DecisionState, question: str, options: Mapping[str, str]
    ) -> Classification:
        scores = {}
        for label, description in options.items():
            vocab = vocabulary([label.replace("_", " "), description])
            scores[label] = sum(1 for t in state.terms if t in vocab) + self.smoothing
        if state.default in scores and max(scores.values()) <= self.smoothing:  # no evidence: the usual reading
            scores = {label: (0.9 if label == state.default else 0.1 / max(len(scores) - 1, 1)) for label in scores}
        return Classification(
            question=question, probabilities=normalize_probabilities(scores, list(options))
        )

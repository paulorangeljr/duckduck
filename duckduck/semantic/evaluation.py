"""
Evaluation harness — runs a labeled question set through ``SemanticSearch``
and scores each stage separately, so a regression points at the stage
that broke (retrieval/decisions vs planning vs execution vs answer).

Dataset: a JSON list of cases::

    {
      "question": "Which users accessed github in the last 24hrs?",
      "expected_sources": ["proxy_logs"],
      "expected_entity": "user",
      "expected_activity": "web_access",   # optional
      "expected_answer_shape": "list",     # optional: list / count / values / count_by / lookup ...
      "expected_results": ["alice", "bob"]  # optional: first result column, as a set
    }

``FeedbackStore.to_evaluation()`` builds one from what users said.
"""

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .engine import SearchResult, SemanticSearch
from .feedback import answer_sources


@dataclass
class CaseResult:
    question: str
    status: str
    source_ok: Optional[bool]
    entity_ok: Optional[bool]
    activity_ok: Optional[bool]
    answer_ok: Optional[bool]
    latency_ms: float
    error: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    shape_ok: Optional[bool] = None
    #: What the case cost (``SearchResult.usage``): engine/LLM calls, tokens, reported money.
    usage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationReport:
    cases: List[CaseResult]

    def _rate(self, attr: str) -> Optional[float]:
        values = [getattr(c, attr) for c in self.cases if getattr(c, attr) is not None]
        return sum(values) / len(values) if values else None

    @property
    def metrics(self) -> Dict[str, Optional[float]]:
        n = len(self.cases) or 1
        return {
            "source_accuracy": self._rate("source_ok"),
            "entity_accuracy": self._rate("entity_ok"),
            "activity_accuracy": self._rate("activity_ok"),
            "answer_shape_accuracy": self._rate("shape_ok"),
            "plan_validity": sum(c.status in ("ok", "planned") for c in self.cases) / n,
            "execution_success": sum(c.status == "ok" for c in self.cases) / n,
            "answer_accuracy": self._rate("answer_ok"),
            "mean_latency_ms": sum(c.latency_ms for c in self.cases) / n,
            "asked_back_rate": sum(c.status == "needs_clarification" for c in self.cases) / n,
            "mean_engine_calls": sum(c.usage.get("engine_calls", 0) for c in self.cases) / n,
            "mean_llm_calls": sum(c.usage.get("llm_calls", 0) for c in self.cases) / n,
            "mean_llm_tokens": sum(c.usage.get("llm_tokens_in", 0) + c.usage.get("llm_tokens_out", 0)
                                   for c in self.cases) / n,
            "reported_cost": (sum(c.usage["cost"] for c in self.cases if c.usage.get("cost") is not None)
                              if any(c.usage.get("cost") is not None for c in self.cases) else None),
        }


def load_dataset(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@contextmanager
def _measuring(search: SemanticSearch):
    """
    While measuring: nothing recorded as feedback searches, and no case
    memory — it would hand the engine the very answers users confirmed,
    and the numbers would flatter it.
    """
    store, memory = search.feedback_store, search.interpreter.memory
    search.feedback_store, search.interpreter.memory = None, None
    try:
        yield
    finally:
        search.feedback_store, search.interpreter.memory = store, memory


def evaluate(search: SemanticSearch, cases: List[Dict[str, Any]], execute: bool = True,
             reader: Optional[str] = None) -> EvaluationReport:
    """Every case through ``search`` (``reader``: which mode reads it — default: the search's)."""
    with _measuring(search):
        return _evaluate(search, cases, execute, reader)


def compare_readers(search: SemanticSearch, cases: List[Dict[str, Any]], readers: Optional[List[str]] = None,
                    execute: bool = False) -> Dict[str, EvaluationReport]:
    """
    The same cases through each mode (default: every mode this search has) —
    which one reads the questions best, how often it asks back, how fast and
    at what cost. ``llm`` / ``llm_decides`` make LLM calls per case.
    """
    return {reader: evaluate(search, cases, execute=execute, reader=reader) for reader in (readers or search.readers)}


def _evaluate(search: SemanticSearch, cases: List[Dict[str, Any]], execute: bool,
              reader: Optional[str] = None) -> EvaluationReport:
    out = []
    for case in cases:
        try:
            res: SearchResult = search.search(case["question"], execute=execute, reader=reader)
        except Exception as exc:  # an evaluation run must survive a broken case
            out.append(CaseResult(case["question"], "error", None, None, None, None, 0.0, error=repr(exc)))
            continue
        plan, intent = res.query_plan, res.intent
        sources = answer_sources(res)

        def check(key: str, actual) -> Optional[bool]:
            return None if key not in case else actual == case[key]

        answer_ok = None
        if "expected_results" in case:
            got = set()
            if res.results is not None and not res.results.empty:
                got = set(res.results.iloc[:, 0].dropna().tolist())
            answer_ok = got == set(case["expected_results"])

        out.append(CaseResult(
            question=case["question"],
            status=res.status,
            source_ok=None if "expected_sources" not in case
            else (bool(sources) and sorted(sources) == sorted(case["expected_sources"])),
            shape_ok=check("expected_answer_shape", intent.answer_shape if intent else None),
            entity_ok=check("expected_entity", intent.target_entity if intent else None),
            activity_ok=check("expected_activity", intent.activity if intent else None),
            answer_ok=answer_ok,
            latency_ms=res.elapsed_ms,
            error=res.clarification if res.status not in ("ok", "planned") else None,
            detail={"sql": res.sql, "sources": sources or None},
            usage=dict(res.usage or {}),
        ))
    return EvaluationReport(out)


# ---------------------------------------------------------------------------
# Threshold calibration
# ---------------------------------------------------------------------------


@dataclass
class ThresholdSuggestion:
    kind: str                 #: entity / activity / source
    samples: int              #: labeled decisions it's based on
    current: float
    suggested: float
    cost_current: float
    cost_suggested: float
    #: At the suggested threshold: right answers accepted / wrong ones accepted / right ones sent back.
    accepted_right: int = 0
    accepted_wrong: int = 0
    rejected_right: int = 0


@dataclass
class CalibrationReport:
    suggestions: List[ThresholdSuggestion]
    cost_wrong: float
    cost_ask: float
    cases: int
    notes: List[str] = field(default_factory=list)

    @property
    def thresholds(self) -> Dict[str, float]:
        """The suggested values, shaped like the config's ``semantic.thresholds``."""
        return {s.kind: s.suggested for s in self.suggestions}

    def summary(self) -> str:
        lines = [
            f"calibrated on {self.cases} labeled questions (a wrong answer costs {self.cost_wrong:g}, "
            f"asking back costs {self.cost_ask:g}):"
        ]
        for s in self.suggestions:
            change = "keep" if abs(s.suggested - s.current) < 1e-9 else f"{s.current:.2f} → {s.suggested:.2f}"
            lines.append(
                f"  {s.kind:<10} {change:<14} cost {s.cost_current:g} → {s.cost_suggested:g} · "
                f"{s.samples} decisions: {s.accepted_right} right accepted, {s.accepted_wrong} wrong accepted, "
                f"{s.rejected_right} right sent back"
            )
        lines += [f"  note: {n}" for n in self.notes]
        lines.append('  → "thresholds": ' + json.dumps(self.thresholds))
        return "\n".join(lines)


def calibrate_thresholds(
    search: SemanticSearch,
    cases: List[Dict[str, Any]],
    cost_wrong: float = 5.0,
    cost_ask: float = 1.0,
) -> CalibrationReport:
    """
    Suggests ``entity`` / ``activity`` / ``source`` thresholds from labeled
    questions (``expected_entity`` / ``expected_activity`` /
    ``expected_sources``, as in ``evaluation.json``) for the configured
    decision engine — thresholds tuned on one engine version don't carry
    over to another, so re-run after changing it.

    Every question is planned (not executed) with all thresholds at 0, so
    each decision's probability is recorded whether or not it would have
    passed; each kind's threshold is then the one minimizing total cost:
    accepting a wrong decision costs ``cost_wrong``, sending a right one
    back to the user costs ``cost_ask``. Few labeled questions → an
    overfit suggestion; aim for dozens per kind.
    """
    with _measuring(search):
        return _calibrate(search, cases, cost_wrong, cost_ask)


def _calibrate(search: SemanticSearch, cases: List[Dict[str, Any]], cost_wrong: float, cost_ask: float) -> CalibrationReport:
    thresholds = search.thresholds
    saved = thresholds.model_dump()
    samples: Dict[str, List[tuple]] = {"entity": [], "activity": [], "source": [], "answer_shape": []}
    try:
        for name in saved:
            setattr(thresholds, name, 0.0)
        for case in cases:
            try:
                res = search.search(case["question"], execute=False)
            except Exception:  # a broken case mustn't stop calibration
                continue
            for d in res.decisions:
                if d.kind == "entity" and "expected_entity" in case:
                    samples["entity"].append((d.probability, d.answer == case["expected_entity"]))
                elif d.kind == "activity" and "expected_activity" in case:
                    samples["activity"].append((d.probability, d.answer == case["expected_activity"]))
                elif d.kind == "source_relevance" and "expected_sources" in case:
                    samples["source"].append((d.probability, d.subject in case["expected_sources"]))
                elif (d.kind == "answer_shape" and d.decided_by == "engine"
                      and "expected_answer_shape" in case):
                    samples["answer_shape"].append((d.probability, d.answer == case["expected_answer_shape"]))
    finally:
        for name, value in saved.items():
            setattr(thresholds, name, value)

    suggestions, notes = [], []
    for kind, points in samples.items():
        if not points:
            label = {"source": "sources"}.get(kind, kind)
            notes.append(f"no labeled {kind} decisions — add expected_{label} to cases")
            continue
        current = saved[kind]
        probs = sorted({p for p, _ in points})
        midpoints = [(a + b) / 2 for a, b in zip(probs, probs[1:])]
        # ... and just above the highest seen: "never decide this kind automatically"
        candidates = sorted({round(t, 3) for t in (current, *probs, *midpoints)} | {round(probs[-1] + 0.001, 3)})

        def cost(t: float) -> float:
            return sum((cost_wrong if not right else 0.0) if p >= t else (cost_ask if right else 0.0) for p, right in points)

        best = min(candidates, key=lambda t: (cost(t), abs(t - current)))  # ties: stay close to the current one
        suggestions.append(ThresholdSuggestion(
            kind=kind, samples=len(points), current=current, suggested=best,
            cost_current=cost(current), cost_suggested=cost(best),
            accepted_right=sum(1 for p, r in points if p >= best and r),
            accepted_wrong=sum(1 for p, r in points if p >= best and not r),
            rejected_right=sum(1 for p, r in points if p < best and r),
        ))
        if len(points) < 20:
            notes.append(f"{kind}: only {len(points)} labeled decisions — treat the suggestion as a hint")
    return CalibrationReport(suggestions, cost_wrong, cost_ask, len(cases), notes)

"""
The 🧭 Auto mode: pick the mode (``rules`` / ``llm`` / ``llm_decides``) for a
question from how similar questions went before — and let the decision
engine choose.

1. **Past runs** (``FeedbackStore.runs()``): one per conversation, read
   by the mode that ran it; its outcome is the last round's. A run is a
   success unless it was rated *not answered* (0) or *partly* (½), or
   ended in an invalid plan (0) — **no negative feedback counts as
   positive**. How often it asked back is kept apart, as evidence.
2. **Similar runs**: cosine over content stems between the question's
   template and each run's (its question and its English reading,
   whichever is closer), at least ``min_similarity``; the ``max_runs``
   closest.
3. **Evidence per mode**: similarity-weighted runs and successes, a
   smoothed success rate ((successes + 1) / (runs + 2)), median time,
   average decision/LLM calls, how often it asked back, and a few of the
   similar questions with how they went.
4. **The decision** is one choice question to the decision engine (Jev —
   never the LLM, even when a mode it could pick is the LLM's), with that
   evidence as facts. The offline engine takes the best smoothed rate,
   the cheaper mode on a tie. **A tie goes to the cheaper mode**: every
   mode within ``tie_margin`` of the engine's top probability is as good
   a pick, so the cheapest of them is used (Paddle < Dive < Fly). Below
   ``thresholds.router`` — or with no engine answer — the ``fallback``
   mode is used: routing is an internal choice, so a doubt never becomes
   a question to the user.

A mode no similar question ran in has no evidence (not a bad record):
the engine is told so.
"""

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .decisions import DecisionState
from .intent import DecisionRecord
from .text import content_stems

#: What each mode is, for the engine (cheapest first — also the tie-break order).
MODES: Dict[str, str] = {
    "rules": "Paddle: wording rules read the question, the decision engine settles doubts. Fastest and cheapest; "
             "best for plainly worded questions.",
    "llm": "Dive: an LLM reads the question first, the decision engine decides. Better for free phrasing, other "
           "languages and indirect questions; costs one LLM call.",
    "llm_decides": "Fly: the LLM reads the question and makes every decision. For questions needing judgment "
                   "and context; the slowest and costliest.",
}
QUESTION = "Which mode should answer this question, given how similar questions went in each mode?"


@dataclass
class Route:
    reader: str
    record: DecisionRecord
    evidence: Dict[str, Any] = field(default_factory=dict)
    similar: List[Dict[str, Any]] = field(default_factory=list)


class ModeRouter:
    def __init__(self, store: Any = None, min_similarity: float = 0.5, max_runs: int = 30,
                 fallback: str = "rules", threshold: float = 0.5, tie_margin: float = 0.05):
        self.store = store
        #: Modes whose probability is within this of the top one are tied — the cheapest of them wins.
        self.tie_margin = tie_margin
        self.min_similarity = min_similarity
        self.max_runs = max_runs
        self.fallback = fallback
        self.threshold = threshold
        self._version: Optional[int] = None
        self._runs: List[Dict[str, Any]] = []

    def _load(self) -> None:
        if self.store is None or self._version == self.store.version:
            return
        self._runs = []
        for run in self.store.runs():
            run["_stems"] = [set(content_stems(t)) for t in (run.get("template"), run.get("question"),
                                                              run.get("english_question")) if t]
            self._runs.append(run)
        self._version = self.store.version

    def similar(self, template: str, question: str) -> List[Dict[str, Any]]:
        """Past runs like this question, closest first, with ``similarity``."""
        self._load()
        queries = [q for q in (set(content_stems(template)), set(content_stems(question))) if q]
        if not queries:
            return []
        scored = []
        for run in self._runs:
            best = max((len(q & s) / math.sqrt(len(q) * len(s)) for q in queries for s in run["_stems"] if s),
                       default=0.0)
            if best >= self.min_similarity:
                scored.append((round(best, 3), run))
        scored.sort(key=lambda x: -x[0])
        return [{**{k: v for k, v in run.items() if k != "_stems"}, "similarity": sim}
                for sim, run in scored[: self.max_runs]]

    @staticmethod
    def evidence(similar: List[Dict[str, Any]], modes: List[str]) -> Dict[str, Dict[str, Any]]:
        """Per mode: weighted runs and successes, smoothed success rate, speed, cost, a few examples."""
        out: Dict[str, Dict[str, Any]] = {}
        for mode in modes:
            runs = [r for r in similar if r["reader"] == mode]
            weight = sum(r["similarity"] for r in runs)
            wins = sum(r["similarity"] * r["success"] for r in runs)
            times = sorted(r["elapsed_ms"] for r in runs if r.get("elapsed_ms") is not None)
            out[mode] = {
                "similar_runs": len(runs),
                "weighted_runs": round(weight, 2),
                "weighted_successes": round(wins, 2),
                "success_rate": round((wins + 1) / (weight + 2), 3),  # smoothed: no runs → 0.5, no evidence
                "median_ms": times[len(times) // 2] if times else None,
                "avg_engine_calls": round(sum(r["engine_calls"] for r in runs) / len(runs), 2) if runs else None,
                "avg_llm_calls": round(sum(r["llm_calls"] for r in runs) / len(runs), 2) if runs else None,
                "asked_back_rate": round(sum(r["asked_back"] for r in runs) / len(runs), 2) if runs else None,
                "examples": [{"question": r.get("english_question") or r["question"],
                              "similarity": r["similarity"], "outcome": r["outcome"]} for r in runs[:3]],
            }
        return out

    def route(self, engine: Any, question: str, template: str, modes: List[str],
              state: Optional[DecisionState] = None) -> Route:
        """Which of ``modes`` should read ``question`` — see the module docstring."""
        modes = [m for m in MODES if m in modes]
        if len(modes) == 1:
            return Route(modes[0], DecisionRecord(kind="route", question=QUESTION, answer=modes[0], probability=1.0,
                                                  decided_by="deterministic", subject="the only mode available"))
        similar = self.similar(template, question)
        evidence = self.evidence(similar, modes)
        tried = {m: e for m, e in evidence.items() if e["similar_runs"]}
        best = self.cheapest_of_tied({m: e["success_rate"] for m, e in evidence.items()}, modes)[0] if tried else None
        base = state or DecisionState(query=question)
        ask_state = base.model_copy(update={
            "facts": {**base.facts, "history_by_mode": evidence,
                      "note": "success = not rated negatively; modes with no similar runs have no evidence"},
            "default": best or self.fallback,
        })
        fallback = self.fallback if self.fallback in modes else modes[0]
        try:
            result = engine.classify(ask_state, QUESTION, {m: MODES[m] for m in modes})
        except Exception as exc:  # a routing failure never costs the answer: the fallback mode reads it
            return Route(fallback, DecisionRecord(
                kind="route", question=QUESTION, answer=fallback, probability=0.0, decided_by="deterministic",
                subject=f"routing failed ({exc.__class__.__name__}) — the fallback mode"), evidence, similar)
        summary = "; ".join(f"{m}: {e['weighted_successes']:g}/{e['weighted_runs']:g} similar ok"
                            for m, e in evidence.items() if e["similar_runs"]) or "no similar questions yet"
        choice, probability = self.cheapest_of_tied(result.probabilities, modes)
        if choice is not None and choice != result.choice:
            summary += f" — {choice} and {result.choice} tied ({probability:.2f} vs {result.probability:.2f}): the cheaper"
        choice, probability = (choice, probability) if choice is not None else (result.choice, result.probability)
        record = DecisionRecord(kind="route", question=QUESTION, answer=choice, probability=probability,
                                threshold=self.threshold, alternatives=[a for a in result.ranked() if a[0] != choice][:2],
                                subject=summary)
        if not record.passed or choice not in modes:
            record.subject = f"{summary} — not sure ({choice} {probability:.2f}): the fallback mode"
            return Route(fallback, record, evidence, similar)
        return Route(choice, record, evidence, similar)

    def cheapest_of_tied(self, probabilities: Dict[str, float], modes: List[str]) -> Tuple[Optional[str], float]:
        """The cheapest mode within ``tie_margin`` of the top probability (``modes`` is cheapest first)."""
        known = {m: float(probabilities[m]) for m in modes if m in probabilities}
        if not known:
            return None, 0.0
        top = max(known.values())
        choice = next(m for m in modes if m in known and known[m] >= top - self.tie_margin - 1e-9)
        return choice, known[choice]

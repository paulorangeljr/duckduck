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
      "expected_results": ["alice", "bob"]  # optional: first result column, as a set
    }
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .engine import SearchResult, SemanticSearch


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
            "plan_validity": sum(c.status in ("ok", "planned") for c in self.cases) / n,
            "execution_success": sum(c.status == "ok" for c in self.cases) / n,
            "answer_accuracy": self._rate("answer_ok"),
            "mean_latency_ms": sum(c.latency_ms for c in self.cases) / n,
        }


def load_dataset(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def evaluate(search: SemanticSearch, cases: List[Dict[str, Any]], execute: bool = True) -> EvaluationReport:
    out = []
    for case in cases:
        try:
            res: SearchResult = search.search(case["question"], execute=execute)
        except Exception as exc:  # an evaluation run must survive a broken case
            out.append(CaseResult(case["question"], "error", None, None, None, None, 0.0, error=repr(exc)))
            continue
        plan, intent = res.query_plan, res.intent

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
            else (plan is not None and sorted(plan.sources) == sorted(case["expected_sources"])),
            entity_ok=check("expected_entity", intent.target_entity if intent else None),
            activity_ok=check("expected_activity", intent.activity if intent else None),
            answer_ok=answer_ok,
            latency_ms=res.elapsed_ms,
            error=res.clarification if res.status not in ("ok", "planned") else None,
            detail={"sql": res.sql, "sources": plan.sources if plan else None},
        ))
    return EvaluationReport(out)

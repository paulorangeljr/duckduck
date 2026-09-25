"""
Case memory: questions like this one that users confirmed (or corrected) before.

Similarity is over question *templates* — the question read in English with
its values replaced by their kinds ("what can I find for this ip
<ip_address>") — so "10.0.0.1" and "10.0.0.2" asked the same way are the
same case. Cosine over content stems, ignoring stopwords.

What the interpreter does with the similar cases (all of it evidence, never
a rule — the decision engine still decides, and a low-confidence decision is
still asked back):

- the tables they used become candidates even if retrieval missed them
  (each is still judged for relevance);
- the entity, activity, answer-shape and source-relevance questions carry
  them as a fact (``similar_confirmed_questions`` / ``used_in_similar_confirmed_questions``);
- the offline lexical engine, which can't read facts, takes a very similar
  case's entity/activity/answer shape as its no-evidence default.
"""

import math
import re
from typing import Any, Dict, Iterable, List, Optional

from .text import content_stems


def question_template(text: str, literals: Iterable[Any]) -> str:
    """``text`` with each extracted value replaced by ``<its kind>``, lowercased and squashed."""
    literals = list(literals)
    for lit in sorted(literals, key=lambda lit: -len(getattr(lit, "text", None) or lit.value)):
        kind = getattr(lit, "semantic_type", None) or lit.kind
        text = re.sub(re.escape(getattr(lit, "text", None) or lit.value), f"<{kind}>", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip().lower().rstrip("?!. ")


class CaseMemory:
    """Reads confirmed/corrected cases from a ``FeedbackStore`` (re-read whenever it changes)."""

    def __init__(self, store: Any, max_cases: int = 3, min_similarity: float = 0.6):
        self.store = store
        self.max_cases = max_cases
        self.min_similarity = min_similarity
        self._version: Optional[int] = None
        self._cases: List[Dict[str, Any]] = []
        self._stems: List[set] = []

    def _load(self) -> None:
        if self._version == self.store.version:
            return
        self._cases = self.store.cases()
        self._stems = [set(content_stems(c["template"])) for c in self._cases]
        self._version = self.store.version

    def similar(self, template: str) -> List[Dict[str, Any]]:
        """The most similar cases (``similarity`` ≥ ``min_similarity``), best first."""
        self._load()
        query = set(content_stems(template))
        if not query:
            return []
        scored = []
        for case, stems in zip(self._cases, self._stems):
            if not stems:
                continue
            similarity = len(query & stems) / math.sqrt(len(query) * len(stems))
            if similarity >= self.min_similarity:
                scored.append((round(similarity, 3), case))
        scored.sort(key=lambda x: -x[0])
        return [{**case, "similarity": sim} for sim, case in scored[: self.max_cases]]


def as_fact(case: Dict[str, Any]) -> Dict[str, Any]:
    """A case as the decision engine sees it — compact, no ids."""
    fact = {
        "question": case.get("english_question") or case.get("question"),
        "similarity": case.get("similarity"),
        "answer_kind": case.get("answer_shape"),
        "entity": case.get("entity"),
        "activity": case.get("activity"),
        "tables": case.get("sources"),
        "confirmed_by_user" if case.get("kind") == "confirmed" else "corrected_by_user": True,
    }
    return {k: v for k, v in fact.items() if v not in (None, [], "")}

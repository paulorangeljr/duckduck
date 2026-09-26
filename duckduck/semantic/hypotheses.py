"""
Hypotheses: when a search would stop to ask the user, the engine judges
every complete reading first.

A clarification already lists the readings it hesitates between — each
``ClarificationOption`` carries the pins that choose it (``answer_shape:
count``, ``entity: host``, ``values_field: owners.department``…). Instead
of asking straight away:

1. **Plan every reading in parallel** — the same search with each option's
   pins, ``execute=False`` (no data read). A reading that needs another
   choice is expanded once more (``depth``), up to ``max_hypotheses``.
2. **Describe each complete plan** in plain words (``describe``): what it
   returns, from which tables, filtered how, joined how — plus its SQL.
3. **One batch to the engine**: a choice — *which of these answers the
   question?* — and a yes/no per plan — *does it answer exactly what was
   asked?* (independent calls would each see one plan; the choice sees
   them side by side, the yes/no guards against all of them being wrong).
4. **Pick or ask**: the winner must clear ``threshold``, lead the runner-up
   by ``margin``, and get a yes. Then it runs, with a ``reading`` decision
   first in the trail (``decided_by="engine"``, the alternatives and their
   probabilities) and the decisions its pins made marked
   ``decided_by="hypothesis"``. Otherwise the user gets the original
   question, as before.

Low confidence still never executes: a reading only runs when the engine
is sure of it *as a whole plan*. The offline lexical engine can't judge
plans, so it keeps asking back (``HypothesisJudge.active``).
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .decisions import Ask, DecisionState, LexicalDecisionEngine, ask_all
from .intent import DecisionRecord
from .metering import submit_in_context

QUESTION = "Which of these readings of the question answers it?"
CHECK = "Does this plan return exactly what the question asks for?"
CRITERIA = {
    "true": "Running this plan answers the question as asked: the right things, from the right tables, with "
            "every filter the question implies and none it doesn't.",
    "false": "It answers a different question: the wrong kind of answer, the wrong things, a missing or extra "
             "filter, or tables that don't hold what was asked.",
}

_SHAPE_HEADS = {
    "list": "List", "count": "Count", "values": "The different values of", "count_values": "How many different",
    "count_by": "A count for each value of", "browse": "Every row and column of", "lookup": "Everything about",
    "locate": "Which tables hold",
}


@dataclass
class Hypothesis:
    key: str
    label: str
    pins: Dict[str, Any]
    result: Any
    description: str = ""
    probability: Optional[float] = None
    answers: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "pins": self.pins, "description": self.description,
                "sql": getattr(self.result, "sql", None), "probability": self.probability,
                "answers": self.answers}


@dataclass
class HypothesisJudge:
    """``max_hypotheses`` readings planned (``workers`` at a time), ``depth`` levels of choices expanded."""

    enabled: bool = True
    max_hypotheses: int = 6
    depth: int = 2
    workers: int = 6
    threshold: float = 0.6
    margin: float = 0.15
    check: float = 0.5

    def active(self, engine: Any) -> bool:
        return self.enabled and not isinstance(engine, LexicalDecisionEngine)

    # ------------------------------------------------------------------

    def readings(self, run: Any, pinned: Dict[str, Any], first: Any) -> List[Hypothesis]:
        """Every complete reading reachable from ``first``'s clarification — planned in parallel."""
        frontier: List[Tuple[str, Dict[str, Any], Any]] = [("", dict(pinned), first)]
        done: List[Hypothesis] = []
        planned = 0
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            for _ in range(max(1, self.depth)):
                jobs = []
                for label, pins, result in frontier:
                    for option in (result.followup.options if result.followup else []):
                        if not option.pins or planned >= self.max_hypotheses:
                            continue
                        merged = {**pins, **option.pins}
                        name = f"{label} · {option.label}" if label else option.label
                        jobs.append((name, merged, submit_in_context(pool, run, merged)))
                        planned += 1
                frontier = []
                for name, pins, future in jobs:
                    try:
                        result = future.result()
                    except Exception:  # a reading that fails to plan is not a reading
                        continue
                    if result.status == "planned":
                        done.append(Hypothesis(key=f"h{len(done) + 1}", label=name, pins=pins, result=result))
                    elif result.status == "needs_clarification":
                        frontier.append((name, pins, result))
                if not frontier:
                    break
        return done

    def judge(self, engine: Any, question: str, hypotheses: List[Hypothesis], catalog: Any,
              state: Optional[DecisionState] = None
              ) -> Tuple[Optional[Hypothesis], Optional[DecisionRecord], List[Hypothesis]]:
        """
        One batch: which reading, and does each answer the question. The
        winner (``None``: ask the user), the ``reading`` record, and every
        reading best first.
        """
        if not hypotheses:
            return None, None, []
        base = state or DecisionState(query=question)
        for h in hypotheses:
            h.description = describe(h.result, catalog)
        asks = [Ask(key=f"check:{h.key}", question=CHECK, subject=h.description, criteria=CRITERIA,
                    state=base.model_copy(update={"facts": {**base.facts, "sql": h.result.sql or ""},
                                                  "offline_prior": 0.5}))
                for h in hypotheses]
        if len(hypotheses) > 1:
            asks.insert(0, Ask(key="reading", question=QUESTION,
                               options={h.key: f"{h.label} — {h.description}" for h in hypotheses},
                               state=base.model_copy(update={"default": None})))
        answers = ask_all(engine, asks)
        for h in hypotheses:
            check = answers.get(f"check:{h.key}")
            h.answers = round(check.probability, 3) if check is not None else None
        choice = answers.get("reading")
        if choice is not None:
            for h in hypotheses:
                h.probability = round(choice.probabilities.get(h.key, 0.0), 3)
            ranked = sorted(hypotheses, key=lambda h: -(h.probability or 0.0))
        else:  # one reading: the yes/no is the whole judgment
            hypotheses[0].probability = hypotheses[0].answers
            ranked = hypotheses
        best = ranked[0]
        runner_up = ranked[1].probability if len(ranked) > 1 else 0.0
        record = DecisionRecord(
            kind="reading", question=QUESTION, answer=best.label, probability=best.probability or 0.0,
            threshold=self.threshold, alternatives=[(h.label, h.probability) for h in ranked[1:4]],
            subject=f"{len(hypotheses)} reading{'s' if len(hypotheses) != 1 else ''} planned in parallel; "
                    f"this one: {best.description}",
        )
        sure = ((best.probability or 0.0) >= self.threshold and (best.probability or 0.0) - (runner_up or 0.0)
                >= self.margin and (best.answers is None or best.answers >= self.check))
        return (best if sure else None), record, ranked


def describe(result: Any, catalog: Any) -> str:
    """A planned result in plain words — what the engine judges a reading by."""
    intent = result.intent
    shape = intent.answer_shape if intent is not None else "list"
    if shape == "catalog":
        return f"answer about this assistant itself — its {intent.catalog_topic or 'tables'} — without reading data"
    if shape in ("small_talk", "out_of_scope"):
        return "reply that the question isn't about the data"
    if result.query_plan is None:
        values = ", ".join(r.value for r in (intent.resources if intent else [])) or "the value"
        tables = ", ".join(s.source for s in result.sections) or "every table"
        return f"{_SHAPE_HEADS.get(shape, shape)} {values}, looked up in {tables}"
    plan = result.query_plan

    def field_words(ref: str) -> str:
        try:
            desc = (catalog.field(ref).description or "").strip().split(".")[0]
        except Exception:
            desc = ""
        return f"{ref} ({desc})" if desc else ref

    target = plan.group_by or plan.select
    parts = [f"{_SHAPE_HEADS.get(shape, shape)} {', '.join(field_words(r) for r in target[:4])}"]
    parts.append("from " + ", ".join(plan.sources))
    for f in plan.filters:
        parts.append(f"where {f.field} {f.operator} {f.value!r}")
    tr = plan.time_range
    if tr is not None:
        if tr.last_hours:
            parts.append(f"{tr.field} in the last {tr.last_hours:g} hours")
        else:
            parts.append(f"{tr.field} from {tr.start or '…'} to {tr.end or '…'}")
    for j in plan.joins:
        parts.append(f"joining {j.left} = {j.right}")
    return "; ".join(parts)

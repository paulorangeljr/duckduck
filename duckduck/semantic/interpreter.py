"""
Semantic Interpreter — natural language → ``SemanticIntent``.

Not a decision tree: every judgment below (entity, activity, each
source's relevance, each value's type) is asked *independently* against
the same state, and each one is recorded with its probability. The
planner combines them afterwards.
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .catalog import Catalog
from .decisions import DecisionEngine, DecisionState
from .extraction import Extraction, RuleBasedExtractor, ValueExtractor
from .intent import (
    ClarificationNeeded,
    DecisionRecord,
    ResourceFilter,
    ScoredSource,
    SemanticIntent,
    Thresholds,
)
from .retrieval import CatalogRetriever, LexicalRetriever
from .text import vocabulary

#: Shape-recognized literal kinds → the semantic type they denote.
_SHAPE_TYPES = {"ip_address": "ip_address", "domain": "domain", "email": "email"}


class SemanticInterpreter:
    def __init__(
        self,
        catalog: Catalog,
        engine: DecisionEngine,
        retriever: Optional[CatalogRetriever] = None,
        extractor: Optional[ValueExtractor] = None,
        thresholds: Optional[Thresholds] = None,
        top_k: int = 5,
    ):
        self.catalog = catalog
        self.engine = engine
        self.retriever = retriever or LexicalRetriever(catalog)
        self.extractor = extractor or RuleBasedExtractor(catalog)
        self.thresholds = thresholds or Thresholds()
        self.top_k = top_k
        self._entity_vocab = {
            name: vocabulary([name.replace("_", " "), *e.keywords])
            for name, e in catalog.entities.items()
        }
        self._semantic_types = {
            f.semantic_type for s in catalog.sources.values() for f in s.fields.values() if f.semantic_type
        }

    def interpret(self, question: str, now: datetime) -> Tuple[SemanticIntent, List[DecisionRecord]]:
        extraction = self.extractor.extract(question, now)
        decisions: List[DecisionRecord] = []
        intent = SemanticIntent(
            question=question,
            time_range=extraction.time_range,
            value_filters=extraction.enum_matches,
            literals=extraction.literals,
        )

        self._decide_entity(intent, extraction, decisions)
        self._decide_activity(intent, extraction, decisions)
        self._decide_resources(intent, extraction, decisions)
        self._decide_sources(intent, extraction, decisions)
        return intent, decisions

    # ------------------------------------------------------------------

    def _decide_entity(self, intent: SemanticIntent, ex: Extraction, decisions: List[DecisionRecord]) -> None:
        if not self.catalog.entities:
            return
        # The head noun is the strongest evidence: the first word the catalog
        # knows as an entity ("show me failed authentication *attempts*
        # from user bob"); fall back to every term.
        focus = next(
            (t for t in [*ex.focus_terms, *ex.terms] if any(t in v for v in self._entity_vocab.values())),
            None,
        )
        terms = [focus] if focus else ex.terms
        options = {
            name: f"{e.description} Keywords: {', '.join(e.keywords)}"
            for name, e in self.catalog.entities.items()
        }
        question = "What entity is the user asking for?"
        result = self.engine.classify(DecisionState(query=intent.question, terms=terms), question, options)
        record = DecisionRecord(
            kind="entity", question=question, answer=result.choice,
            probability=result.probability, threshold=self.thresholds.entity,
            alternatives=result.ranked()[1:4],
        )
        decisions.append(record)
        if not record.passed:
            raise ClarificationNeeded(
                f"Couldn't tell what you're asking for (best guess: '{result.choice}', "
                f"{result.probability:.2f}).",
                record, [label for label, _ in result.ranked()[:4]],
            )
        intent.target_entity, intent.target_confidence = result.choice, result.probability

    def _decide_activity(self, intent: SemanticIntent, ex: Extraction, decisions: List[DecisionRecord]) -> None:
        if not self.catalog.activities:
            return
        options = {
            name: f"{a.description} Keywords: {', '.join(a.keywords)}"
            for name, a in self.catalog.activities.items()
        }
        question = "What activity is being investigated?"
        result = self.engine.classify(DecisionState(query=intent.question, terms=ex.terms), question, options)
        record = DecisionRecord(
            kind="activity", question=question, answer=result.choice,
            probability=result.probability, threshold=self.thresholds.activity,
            alternatives=result.ranked()[1:4],
        )
        decisions.append(record)
        # Optional: plenty of questions ("show me production machines")
        # name no activity — sources are still chosen by relevance.
        if record.passed:
            intent.activity, intent.activity_confidence = result.choice, result.probability

    def _decide_resources(self, intent: SemanticIntent, ex: Extraction, decisions: List[DecisionRecord]) -> None:
        activity = self.catalog.activities.get(intent.activity) if intent.activity else None
        free_text = [lit for lit in ex.literals if lit.kind == "term" and not self._known_type(lit)]

        for lit in ex.literals:
            sem_type = self._known_type(lit)
            if sem_type:
                # Recognized by shape (an IP, a domain), typed by the extractor,
                # or named right before the value ("user alice").
                if lit.kind in _SHAPE_TYPES:
                    how, by = f"shape: {lit.kind}", "deterministic"
                elif lit.semantic_type == sem_type:
                    how, by = "typed by the extractor", "extractor"
                else:
                    how, by = f"preceded by '{lit.hint}'", "deterministic"
                decisions.append(DecisionRecord(
                    kind="resource_type", question=f"What kind of value is {lit.value!r}?",
                    answer=sem_type, probability=0.99, decided_by=by, subject=how,
                ))
                intent.resources.append(ResourceFilter(type=sem_type, value=lit.value, confidence=0.99, literal_kind=lit.kind))
                continue

            if lit.kind == "term" and len(free_text) > 1:
                raise ClarificationNeeded(
                    "More than one word could be the value to filter on: "
                    + ", ".join(repr(t.value) for t in free_text) + ". Quote the value you mean.",
                    options=[t.value for t in free_text],
                )

            if activity is not None and activity.resource:
                # The activity says what it's about ("accessed X" → X is a domain).
                decisions.append(DecisionRecord(
                    kind="resource_type", question=f"What kind of value is {lit.value!r}?",
                    answer=activity.resource, probability=intent.activity_confidence,
                    decided_by="deterministic", subject=f"resource of activity '{intent.activity}'",
                ))
                intent.resources.append(ResourceFilter(
                    type=activity.resource, value=lit.value,
                    confidence=intent.activity_confidence, literal_kind=lit.kind,
                ))
                continue

            sem_types = sorted({
                f.semantic_type for s in self.catalog.sources.values() for f in s.fields.values()
                if f.semantic_type and f.semantic_type != "event_time"
            })
            if not sem_types:
                continue
            question = f"What kind of value is {lit.value!r} in this question?"
            result = self.engine.classify(
                DecisionState(query=intent.question, terms=ex.terms, facts={"value": lit.value}),
                question, {t: t.replace("_", " ") for t in sem_types},
            )
            record = DecisionRecord(
                kind="resource_type", question=question, answer=result.choice,
                probability=result.probability, threshold=self.thresholds.resource_type,
                alternatives=result.ranked()[1:4],
            )
            decisions.append(record)
            if record.passed:
                intent.resources.append(ResourceFilter(
                    type=result.choice, value=lit.value,
                    confidence=result.probability, literal_kind=lit.kind,
                ))
            else:
                # Never drop it silently — that would answer a broader
                # question than the one asked.
                raise ClarificationNeeded(
                    f"Couldn't tell what {lit.value!r} refers to — name its type right "
                    f"before it (e.g. 'user {lit.value}', 'host {lit.value}').", record,
                    [label for label, _ in result.ranked()[:4]],
                )

    def _known_type(self, lit) -> Optional[str]:
        """A literal's semantic type when it's knowable without asking anyone."""
        if lit.kind in _SHAPE_TYPES:
            return self._shape_semantic_type(lit.kind)
        if lit.semantic_type in self._semantic_types:
            return lit.semantic_type
        return self._hinted_type(lit.hint)

    def _hinted_type(self, hint: Optional[str]) -> Optional[str]:
        """Entity named by the word right before a value, if fields carry that semantic type."""
        if not hint:
            return None
        for name, vocab in self._entity_vocab.items():
            if hint in vocab and name in self._semantic_types:
                return name
        return None

    def _shape_semantic_type(self, kind: str) -> str:
        sem_type = _SHAPE_TYPES[kind]
        if kind == "email" and not any(
            f.semantic_type == "email" for s in self.catalog.sources.values() for f in s.fields.values()
        ):
            return "user"  # no dedicated email fields: an address identifies a user
        return sem_type

    def _decide_sources(self, intent: SemanticIntent, ex: Extraction, decisions: List[DecisionRecord]) -> None:
        retrieved: Dict[str, float] = dict(self.retriever.search(intent.question, self.top_k))
        scored: List[ScoredSource] = []
        question = "Is this source relevant to answering the question?"
        state = DecisionState(query=intent.question, terms=ex.terms)
        subjects = {name: self.catalog.describe_source(name) for name in retrieved}
        decide_many = getattr(self.engine, "decide_many", None)
        if decide_many is not None:  # one round trip for every candidate
            results = decide_many(state, question, subjects)
        else:
            results = {name: self.engine.decide(state, question, subj) for name, subj in subjects.items()}
        for name, retrieval_score in retrieved.items():
            result = results[name]
            threshold = self.thresholds.critical if self.catalog.sources[name].critical else self.thresholds.source
            record = DecisionRecord(
                kind="source_relevance", question=question, subject=name,
                answer=result.answer, probability=result.probability, threshold=threshold,
            )
            decisions.append(record)
            if record.passed:
                scored.append(ScoredSource(source=name, confidence=result.probability, retrieval_score=retrieval_score))
        if not scored:
            best = max(
                (d for d in decisions if d.kind == "source_relevance"),
                key=lambda d: d.probability, default=None,
            )
            raise ClarificationNeeded(
                "No data source looks relevant enough to answer this question.",
                best, [d.subject for d in decisions if d.kind == "source_relevance"],
            )
        scored.sort(key=lambda s: (s.confidence, s.retrieval_score), reverse=True)
        intent.candidate_sources = scored

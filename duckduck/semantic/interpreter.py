"""
Semantic Interpreter — natural language → ``SemanticIntent``.

Not a decision tree: every judgment below (entity, activity, each
source's relevance, each value's type) is asked *independently* against
the same state, and each one is recorded with its probability. The
planner combines them afterwards.

They're independent, so they go out together: one batch (``ask_all`` —
a single Jev request) for all of them. A second, small batch only when
the chosen entity/activity is declared by sources lexical retrieval
didn't propose (the question used words the catalog doesn't).
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .catalog import Catalog
from .decisions import CRITERIA, Ask, DecisionEngine, DecisionState, ask_all
from .extraction import Extraction, RuleBasedExtractor, ValueExtractor
from .clarify import ClarificationTexts
from .intent import (
    ClarificationNeeded,
    DecisionRecord,
    ResourceFilter,
    ScoredSource,
    SemanticIntent,
    Thresholds,
)
from .retrieval import CatalogRetriever, LexicalRetriever
from .shapes import ACROSS_SHAPES, AnswerShapes
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
        texts: Optional[ClarificationTexts] = None,
        shapes: Optional[AnswerShapes] = None,
    ):
        self.catalog = catalog
        #: The wording of each answer shape (defaults + ``semantic.answer_shapes``).
        self.shapes = shapes or AnswerShapes()
        self.engine = engine
        self.retriever = retriever or LexicalRetriever(catalog)
        self.extractor = extractor or RuleBasedExtractor(catalog)
        self.thresholds = thresholds or Thresholds()
        self.top_k = top_k
        #: The questions asked back to the user.
        self.texts = texts or ClarificationTexts()
        self._entity_vocab = {
            name: vocabulary([name.replace("_", " "), *e.keywords])
            for name, e in catalog.entities.items()
        }
        self._semantic_types = {
            f.semantic_type for s in catalog.sources.values() for f in s.fields.values() if f.semantic_type
        }

    def interpret(self, question: str, now: datetime, pinned: Optional[Dict[str, Any]] = None,
                  evidence: Any = None) -> Tuple[SemanticIntent, List[DecisionRecord]]:
        """
        ``pinned``: decisions the user settled by answering clarifications
        (see ``Clarification``) — taken as given, never asked again.
        ``evidence``: an ``EvidenceProber`` — where the question's values
        were (not) found in the candidate sources goes to the engine as facts.
        """
        pins = dict(pinned or {})
        extraction = self.extractor.extract(question, now)
        english = getattr(extraction, "english_question", None)
        extraction.literals = self._without_shape_words([question, english], extraction.literals)
        decisions: List[DecisionRecord] = []
        intent = SemanticIntent(
            question=question,
            english_question=english,
            time_range=extraction.time_range,
            value_filters=extraction.enum_matches,
            literals=extraction.literals,
        )
        try:
            refused = {k[7:] for k, v in pins.items() if k.startswith("source:") and v is False}
            ranked = self.retriever.search(intent.working_question, self.top_k + len(refused))
            retrieved = dict([(n, sc) for n, sc in ranked if n not in refused][: self.top_k])
            for key, value in pins.items():  # a source the user picked is a candidate whatever retrieval said
                if key.startswith("source:") and value and key[7:] in self.catalog.sources:
                    retrieved.setdefault(key[7:], 0.0)
            live = self._live_evidence(extraction, pins, retrieved, evidence) if evidence is not None else {}
            asks = [a for a in (self._entity_ask(intent, extraction, pins), self._activity_ask(intent, extraction, pins),
                                self._shape_ask(intent, pins)) if a]
            asks += self._value_asks(intent, extraction, pins, live)
            asks += self._source_asks(intent, extraction, [n for n in retrieved if f"source:{n}" not in pins], live)
            answers = ask_all(self.engine, asks)

            self._apply_shape(intent, answers.get("answer_shape"), decisions, pins)
            self._apply_entity(intent, answers.get("entity"), decisions, pins)
            self._apply_activity(intent, answers.get("activity"), decisions, pins)
            self._apply_resources(intent, extraction, answers, decisions, pins)

            # sources that declare what was decided, but that lexical retrieval missed
            extra = self._declaring_sources(intent, exclude=retrieved)
            if extra:
                extra_live = self._live_evidence(extraction, pins, extra, evidence) if evidence is not None else {}
                answers.update(ask_all(self.engine, self._source_asks(intent, extraction, extra, extra_live)))
                retrieved.update(extra)
            self._apply_sources(intent, retrieved, answers, decisions, pins)
        except ClarificationNeeded as exc:
            exc.decisions = decisions  # everything decided so far, for the result/audit
            raise
        return intent, decisions

    # ------------------------------------------------------------------
    # the questions
    # ------------------------------------------------------------------

    def _entity_ask(self, intent: SemanticIntent, ex: Extraction, pins: Dict[str, Any]) -> Optional[Ask]:
        if not self.catalog.entities or "entity" in pins:
            return None
        ruled_out = {k[11:] for k, v in pins.items() if k.startswith("not_entity:") and v}
        if ruled_out and not set(self.catalog.entities) - ruled_out:
            raise ClarificationNeeded("None of the kinds of things in the data is what the question asks for.")
        # The head noun is the strongest evidence: the first word the catalog
        # knows as an entity ("show me failed authentication *attempts*
        # from user bob"); fall back to every term.
        focus = next(
            (t for t in [*ex.focus_terms, *ex.terms] if any(t in v for v in self._entity_vocab.values())),
            None,
        )
        options = {
            name: f"{e.description} Keywords: {', '.join(e.keywords)}"
            for name, e in self.catalog.entities.items() if name not in ruled_out
        }
        return Ask(key="entity", question="What entity is the user asking for?", options=options,
                   state=intent.decision_state(terms=[focus] if focus else ex.terms))

    def _activity_ask(self, intent: SemanticIntent, ex: Extraction, pins: Dict[str, Any]) -> Optional[Ask]:
        if not self.catalog.activities or "activity" in pins:
            return None
        options = {
            name: f"{a.description} Keywords: {', '.join(a.keywords)}"
            for name, a in self.catalog.activities.items()
        }
        return Ask(key="activity", question="What activity is being investigated?", options=options,
                   state=intent.decision_state(terms=ex.terms))

    def _free_text(self, ex: Extraction, pins: Dict[str, Any]) -> list:
        """Untyped free-text values — just the one the user named, when they were asked to pick."""
        free = [lit for lit in ex.literals if lit.kind == "term" and not self._known_type(lit)]
        if "value_term" in pins:
            free = [lit for lit in free if lit.value == pins["value_term"]]
        return free

    def _live_evidence(self, ex: Extraction, pins: Dict[str, Any], sources, evidence) -> Dict[str, Any]:
        """
        ``{source: [{value, found_in, not_found_in}, ...], "types:<value>": [semantic types found]}``
        for the question's values over the fields that could hold them.
        """
        free = {lit.value for lit in self._free_text(ex, pins)}
        dropped = {lit.value for lit in ex.literals if lit.kind == "term" and not self._known_type(lit)} - free
        out: Dict[str, Any] = {}
        for lit in ex.literals:
            if lit.value in dropped:
                continue
            typed = self._known_type(lit) or pins.get(f"value:{lit.value}")
            found_types = set()
            for name in sources:
                src = self.catalog.sources[name]
                refs = [f"{name}.{f}" for f, d in src.fields.items()
                        if (d.semantic_type == typed if typed else
                            d.type == "string" and d.semantic_type and d.semantic_type != "event_time")][:3]
                if not refs:
                    continue
                fact = evidence.summary(lit.value, refs)
                out.setdefault(name, []).append(fact)
                found_types |= {self.catalog.field(r).semantic_type for r in fact["found_in"]}
            if not typed:
                out[f"types:{lit.value}"] = sorted(found_types)
        return out

    def _value_asks(self, intent: SemanticIntent, ex: Extraction, pins: Dict[str, Any],
                    live: Optional[Dict[str, Any]] = None) -> List[Ask]:
        """"What kind of value is X?" for a free-text value nothing else types (answer used only if needed)."""
        free_text = self._free_text(ex, pins)
        if len(free_text) != 1 or f"value:{free_text[0].value}" in pins:
            return []  # none to type, ambiguous (asked back to the user), or already answered by them
        sem_types = self._value_types()
        if not sem_types:
            return []
        lit = free_text[0]
        facts: Dict[str, Any] = {"value": lit.value}
        if live and f"types:{lit.value}" in live:
            facts["live_check"] = {"value_found_in_fields_of_type": live[f"types:{lit.value}"]}
        return [Ask(
            key=f"value:{lit.value}", question=f"What kind of value is {lit.value!r} in this question?",
            options={t: t.replace("_", " ") for t in sem_types},
            state=intent.decision_state(terms=ex.terms, facts=facts),
        )]

    def _value_types(self) -> List[str]:
        return sorted({
            f.semantic_type for s in self.catalog.sources.values() for f in s.fields.values()
            if f.semantic_type and f.semantic_type != "event_time"
        })

    def _source_asks(self, intent: SemanticIntent, ex: Extraction, names,
                     live: Optional[Dict[str, Any]] = None) -> List[Ask]:
        def state(name: str) -> DecisionState:
            facts = {"live_check": live[name]} if live and live.get(name) else {}
            return intent.decision_state(terms=ex.terms, facts=facts)

        return [
            Ask(key=f"source:{name}", question="Is this source relevant to answering the question?",
                subject=self.catalog.describe_source(name), criteria=CRITERIA["source"], state=state(name))
            for name in names
        ]

    def _declaring_sources(self, intent: SemanticIntent, exclude) -> Dict[str, float]:
        """Sources declaring the decided entity/activity that aren't candidates yet (up to ``top_k``)."""
        wanted = [x for x in (intent.target_entity, intent.activity) if x]
        extra = [
            name for name, src in self.catalog.sources.items()
            if name not in exclude and any(w in src.entities or w in src.activities for w in wanted)
        ]
        return {name: 0.0 for name in extra[: self.top_k]}

    # ------------------------------------------------------------------
    # the answers, in the order a person would check them
    # ------------------------------------------------------------------

    def _apply_entity(self, intent: SemanticIntent, result, decisions: List[DecisionRecord], pins: Dict[str, Any]) -> None:
        question = "What entity is the user asking for?"
        if pins.get("entity") in self.catalog.entities:
            decisions.append(_by_user("entity", question, pins["entity"], self.thresholds.entity))
            intent.target_entity, intent.target_confidence = pins["entity"], 1.0
            return
        if result is None:
            return
        record = DecisionRecord(
            kind="entity", question=question, answer=result.choice,
            probability=result.probability, threshold=self.thresholds.entity,
            alternatives=result.ranked()[1:4],
        )
        decisions.append(record)
        if not record.passed and intent.answer_shape in ("values", "count_values", *ACROSS_SHAPES):
            record.subject = "not needed for this kind of answer"
            return  # "the different severities" / "everything about X" — the field or the value decides
        if not record.passed:
            ranked = [label for label, _ in result.ranked()[:4]]
            raise ClarificationNeeded(
                f"Couldn't tell what you're asking for (best guess: '{result.choice}', "
                f"{result.probability:.2f}).",
                record, ranked, self.texts.entity(intent.question, ranked, self.catalog,
                                                  counting=intent.answer_shape in ("count", "count_by")),
            )
        intent.target_entity, intent.target_confidence = result.choice, result.probability

    def _apply_activity(self, intent: SemanticIntent, result, decisions: List[DecisionRecord], pins: Dict[str, Any]) -> None:
        if pins.get("activity") in self.catalog.activities:
            decisions.append(_by_user("activity", "What activity is being investigated?", pins["activity"],
                                      self.thresholds.activity))
            intent.activity, intent.activity_confidence = pins["activity"], 1.0
            return
        if result is None:
            return
        record = DecisionRecord(
            kind="activity", question="What activity is being investigated?", answer=result.choice,
            probability=result.probability, threshold=self.thresholds.activity,
            alternatives=result.ranked()[1:4],
        )
        decisions.append(record)
        # Optional: plenty of questions ("show me production machines")
        # name no activity — sources are still chosen by relevance.
        if record.passed:
            intent.activity, intent.activity_confidence = result.choice, result.probability

    _SHAPE_QUESTION = "What kind of answer does the question ask for?"

    def _without_shape_words(self, questions: List[Optional[str]], literals: list) -> list:
        """The answer-shape wording ("which tables", "tudo sobre") is never a value to filter on."""
        shape_tokens = set().union(*(self.shapes.matched_tokens(q) for q in questions if q))
        if not shape_tokens:
            return literals
        out = []
        for lit in literals:
            if lit.kind == "term":
                words = lit.value.split()
                while words and words[0].lower() in shape_tokens:
                    words.pop(0)
                while words and words[-1].lower() in shape_tokens:
                    words.pop()
                if not words:
                    continue
                if len(words) != len(lit.value.split()):
                    lit = lit.model_copy(update={"value": " ".join(words), "text": " ".join(words)})
            out.append(lit)
        return out

    def _shape_ask(self, intent: SemanticIntent, pins: Dict[str, Any]) -> Optional[Ask]:
        if pins.get("answer_shape") in self.shapes.descriptions:
            return None
        candidates, words = self.shapes.candidates(intent.working_question)
        if len(candidates) == 1:
            return None
        return Ask(key="answer_shape", question=self._SHAPE_QUESTION,
                   options={c: self.shapes.descriptions[c] for c in candidates},
                   state=intent.decision_state(facts={"wording": words}))

    def _apply_shape(self, intent: SemanticIntent, result: Any, decisions: List[DecisionRecord],
                     pins: Dict[str, Any]) -> None:
        """
        Which kind of answer — and so which SQL: a list, a count, the
        different values of a field, how many there are, a count per group.
        Clear wording settles it without a model; ambiguous wording goes to
        the engine (in the same batch); a doubt is asked back.
        """
        if pins.get("answer_shape") in self.shapes.descriptions:
            intent.answer_shape = pins["answer_shape"]
            decisions.append(_by_user("answer_shape", self._SHAPE_QUESTION, intent.answer_shape, None))
            return
        candidates, words = self.shapes.candidates(intent.working_question)
        if len(candidates) == 1:
            intent.answer_shape = candidates[0]
            if candidates[0] != "list":
                decisions.append(DecisionRecord(kind="answer_shape", question=self._SHAPE_QUESTION, answer=candidates[0],
                                                probability=0.99, decided_by="deterministic", subject=words))
            return
        record = DecisionRecord(
            kind="answer_shape", question=self._SHAPE_QUESTION, subject=words, answer=result.choice,
            probability=result.probability, threshold=self.thresholds.answer_shape, alternatives=result.ranked()[1:4],
        )
        decisions.append(record)
        if not record.passed:
            ranked = [c for c, _ in result.ranked()]
            raise ClarificationNeeded(
                f"Not sure what kind of answer the question asks for (best guess {result.choice}, "
                f"{result.probability:.2f} < {self.thresholds.answer_shape:.2f}).", record, ranked,
                self.texts.answer_shape(intent.question, ranked),
            )
        intent.answer_shape = result.choice

    def _apply_resources(self, intent: SemanticIntent, ex: Extraction, answers, decisions: List[DecisionRecord],
                         pins: Dict[str, Any]) -> None:
        activity = self.catalog.activities.get(intent.activity) if intent.activity else None
        free_text = self._free_text(ex, pins)
        dropped = {lit.value for lit in ex.literals if lit.kind == "term" and not self._known_type(lit)} \
            - {lit.value for lit in free_text}

        for lit in ex.literals:
            if lit.value in dropped:
                continue  # the user said another word was the value
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
                    followup=self.texts.value_term(intent.question, [t.value for t in free_text]),
                )

            pinned_type = pins.get(f"value:{lit.value}")
            if lit.kind == "term" and pinned_type:
                decisions.append(_by_user("resource_type", f"What kind of value is {lit.value!r}?", pinned_type,
                                          self.thresholds.resource_type))
                intent.resources.append(ResourceFilter(type=pinned_type, value=lit.value, confidence=1.0,
                                                       literal_kind=lit.kind))
                continue

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

            result = answers.get(f"value:{lit.value}")
            if result is None:
                continue
            record = DecisionRecord(
                kind="resource_type", question=result.question, answer=result.choice,
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
                ranked = [label for label, _ in result.ranked()[:4]]
                raise ClarificationNeeded(
                    f"Couldn't tell what {lit.value!r} refers to — name its type right "
                    f"before it (e.g. 'user {lit.value}', 'host {lit.value}').", record, ranked,
                    self.texts.value_type(intent.question, lit.value, ranked),
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

    def _apply_sources(self, intent: SemanticIntent, retrieved: Dict[str, float], answers,
                       decisions: List[DecisionRecord], pins: Dict[str, Any]) -> None:
        scored: List[ScoredSource] = []
        question = "Is this source relevant to answering the question?"
        for name, retrieval_score in retrieved.items():
            if f"source:{name}" in pins:
                chosen = bool(pins[f"source:{name}"])
                decisions.append(_by_user("source_relevance", question, chosen, self.thresholds.source, subject=name,
                                          probability=1.0 if chosen else 0.0))
                if chosen:
                    scored.append(ScoredSource(source=name, confidence=1.0, retrieval_score=retrieval_score))
                continue
            result = answers[f"source:{name}"]
            threshold = self.thresholds.critical if self.catalog.sources[name].critical else self.thresholds.source
            record = DecisionRecord(
                kind="source_relevance", question=question, subject=name,
                answer=result.answer, probability=result.probability, threshold=threshold,
            )
            decisions.append(record)
            if record.passed:
                scored.append(ScoredSource(source=name, confidence=result.probability, retrieval_score=retrieval_score))
        if not scored and intent.answer_shape in ACROSS_SHAPES:
            intent.candidate_sources = []  # answered from every table holding the value, not from these
            return
        if not scored:
            best = max(
                (d for d in decisions if d.kind == "source_relevance"),
                key=lambda d: d.probability, default=None,
            )
            asked = sorted(
                (d for d in decisions if d.kind == "source_relevance" and d.decided_by != "user"),
                key=lambda d: d.probability, reverse=True,
            )
            raise ClarificationNeeded(
                "No data source looks relevant enough to answer this question.",
                best, [d.subject for d in decisions if d.kind == "source_relevance"],
                self.texts.source(intent.question, [d.subject for d in asked], self.catalog) if asked else None,
            )
        scored.sort(key=lambda s: (s.confidence, s.retrieval_score), reverse=True)
        intent.candidate_sources = scored


def _by_user(kind: str, question: str, answer: Any, threshold: Optional[float], subject: str = "",
             probability: float = 1.0) -> DecisionRecord:
    """A decision the user settled by answering a clarification."""
    return DecisionRecord(kind=kind, question=question, subject=subject, answer=answer, probability=probability,
                          threshold=threshold, decided_by="user")


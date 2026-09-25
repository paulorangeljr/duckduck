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
from .decisions import CRITERIA, Ask, DecisionEngine, DecisionState, ask_all, engine_in_use
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
from .memory import as_fact, question_template
from .scope import in_scope
from .shapes import ACROSS_SHAPES, AnswerShapes, browse_target, catalog_topic
from .clarify import first_sentence
from .text import content_stems, stem, tokenize, vocabulary

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
        memory: Any = None,
    ):
        self.catalog = catalog
        #: ``CaseMemory`` — similar confirmed questions, as evidence (``None``: off).
        self.memory = memory
        #: The wording of each answer shape (defaults + ``semantic.answer_shapes``).
        self.shapes = shapes or AnswerShapes()
        self._engine = engine
        self.retriever = retriever or LexicalRetriever(catalog)
        self.extractor = extractor or RuleBasedExtractor(catalog)
        self._preview_rules = RuleBasedExtractor(catalog)  # preview() with the rules reader: no LLM while typing
        #: The ``llm`` reader: an ``LLMExtractor`` asked for its reading of the question (``None``: unavailable).
        self.llm_reader: Any = extractor if hasattr(extractor, "_reading") else None
        #: ``rules`` or ``llm`` — see ``interpret``.
        self.reader = "rules"
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

    @property
    def engine(self) -> DecisionEngine:
        """The decision engine: the default one, or the search's own (``decisions.using_engine``)."""
        return engine_in_use(self._engine)

    @engine.setter
    def engine(self, engine: DecisionEngine) -> None:
        self._engine = engine

    def interpret(self, question: str, now: datetime, pinned: Optional[Dict[str, Any]] = None,
                  evidence: Any = None, reader: Optional[str] = None) -> Tuple[SemanticIntent, List[DecisionRecord]]:
        """
        ``pinned``: decisions the user settled by answering clarifications
        (see ``Clarification``) — taken as given, never asked again.
        ``evidence``: an ``EvidenceProber`` — where the question's values
        were (not) found in the candidate sources goes to the engine as facts.
        """
        pins = dict(pinned or {})
        reader = self._check_reader(reader)
        extraction = (self.llm_reader.extract(question, now, reading=True) if reader in self.LLM_READERS
                      else self.extractor.extract(question, now))
        english = getattr(extraction, "english_question", None)
        extraction.literals = self._without_shape_words([question, english], extraction.literals)
        decisions: List[DecisionRecord] = []
        intent = SemanticIntent(
            question=question,
            english_question=english,
            time_range=extraction.time_range,
            value_filters=extraction.enum_matches,
            literals=extraction.literals,
            reader=reader,
            reading=getattr(extraction, "reading", None),
        )
        if reader in self.LLM_READERS:
            decisions.append(self._reading_record(intent))
        if self.memory is not None:
            intent.similar_cases = self.memory.similar(question_template(intent.working_question, intent.literals))
        if self._about_the_catalog(intent, decisions, pins):
            return intent, decisions  # nothing to decide about the data — no entity, activity or tables to ask about
        if self._small_talk(intent, decisions, pins):
            return intent, decisions  # "hi", "thanks" — nothing to look up
        if self._browse(intent, decisions, pins):
            return intent, decisions  # "show me table owners" — the question already says which table
        try:
            refused = {k[7:] for k, v in pins.items() if k.startswith("source:") and v is False}
            refused |= {n for n in self.catalog.sources if not in_scope(n)}  # the user narrowed the tables
            ranked = self.retriever.search(intent.working_question, self.top_k + len(refused))
            retrieved = dict([(n, sc) for n, sc in ranked if n not in refused][: self.top_k])
            for key, value in pins.items():  # a source the user picked is a candidate whatever retrieval said
                if key.startswith("source:") and value and key[7:] in self.catalog.sources and in_scope(key[7:]):
                    retrieved.setdefault(key[7:], 0.0)
            for name in self._reading_sources(intent):  # ... and one the LLM's reading names (still judged)
                if name not in refused:
                    retrieved.setdefault(name, 0.0)
            for case in intent.similar_cases:  # ... and so is one a similar confirmed question used (still judged)
                for name in case.get("sources") or []:
                    if name in self.catalog.sources and name not in refused:
                        retrieved.setdefault(name, 0.0)
            live = self._live_evidence(extraction, pins, retrieved, evidence) if evidence is not None else {}
            asks = [a for a in (self._entity_ask(intent, extraction, pins), self._activity_ask(intent, extraction, pins),
                                self._shape_ask(intent, pins)) if a]
            asks += self._value_asks(intent, extraction, pins, live)
            asks += self._source_asks(intent, extraction, [n for n in retrieved if f"source:{n}" not in pins], live)
            scope_ask = self._in_scope_ask(intent, extraction, max((sc for _, sc in ranked), default=0.0), pins)
            if scope_ask is not None:
                asks.append(scope_ask)
            self._reading_defaults(intent, asks)
            answers = ask_all(self.engine, asks)
            if self._out_of_scope(intent, answers.get("in_scope"), decisions, pins):
                return intent, decisions  # not about the data: a direct reply, no questions back

            self._apply_shape(intent, answers.get("answer_shape"), decisions, pins)
            if intent.answer_shape in ("catalog", "browse"):  # chosen over the rules' reading: nothing else to decide
                return intent, decisions
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
            exc.intent = intent  # ... and what was understood (the feedback store's template needs its values)
            raise
        return intent, decisions

    # ------------------------------------------------------------------
    # the questions
    # ------------------------------------------------------------------

    def _entity_ask(self, intent: SemanticIntent, ex: Extraction, pins: Dict[str, Any]) -> Optional[Ask]:
        if not self.catalog.entities or "entity" in pins:
            return None
        if self._values_of_named_field(intent, pins):
            return None  # the field decides the answer, not the entity
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
        remembered = _remembered(intent, "entity", options)
        records = self._records_of_activity(ex, focus, options)
        if records is not None:  # "show me the logins": the head noun is an activity — its records are asked for
            remembered["facts"] = {**remembered.get("facts", {}), "head_noun_is_an_activity": records[1]}
            remembered.setdefault("default", records[0])
        return Ask(key="entity", question="What entity is the user asking for?", options=options,
                   state=intent.decision_state(terms=[focus] if focus else ex.terms, **remembered))

    def _records_of_activity(self, ex: Extraction, focus: Optional[str],
                             options: Dict[str, str]) -> Optional[Tuple[str, str]]:
        """
        ``(row-level entity, the word)`` when the question's head noun names an
        activity and no entity ("the logins", "as conexões"): what it lists are
        that activity's records. ``None`` otherwise.
        """
        if focus is not None:
            return None
        rows = [n for n, e in self.catalog.entities.items() if e.row_level and n in options]
        if len(rows) != 1:
            return None
        if getattr(self, "_activity_vocab", None) is None:
            self._activity_vocab = vocabulary([w for n, a in self.catalog.activities.items()
                                               for w in [n.replace("_", " "), *a.keywords]])
        head = next((t for t in [*ex.focus_terms, *ex.terms] if t in self._activity_vocab), None)
        return (rows[0], head) if head else None

    def _activity_ask(self, intent: SemanticIntent, ex: Extraction, pins: Dict[str, Any]) -> Optional[Ask]:
        if not self.catalog.activities or "activity" in pins:
            return None
        options = {
            name: f"{a.description} Keywords: {', '.join(a.keywords)}"
            for name, a in self.catalog.activities.items()
        }
        return Ask(key="activity", question="What activity is being investigated?", options=options,
                   state=intent.decision_state(terms=ex.terms, **_remembered(intent, "activity", options)))

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
            if intent.similar_cases:
                facts["used_in_similar_confirmed_questions"] = any(
                    name in (c.get("sources") or []) for c in intent.similar_cases)
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
            if name not in exclude and in_scope(name) and any(w in src.entities or w in src.activities for w in wanted)
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

    def preview(self, question: str, now: datetime, reader: Optional[str] = None) -> Dict[str, Any]:
        """
        While the question is typed: the entity, the answer kind and the
        relevance of the candidate tables, from one engine batch — never
        raising, nothing recorded. ``rules`` reader: rule-based extraction,
        no LLM. ``llm`` reader: the LLM reads it (cached, so the search that
        follows reuses the same reading), and the engine decides as in
        ``interpret``.
        """
        reader = self._check_reader(reader)
        ex = self.llm_reader.extract(question, now, reading=True) if reader in self.LLM_READERS else \
            self._preview_rules.extract(question, now)
        english = getattr(ex, "english_question", None)
        intent = SemanticIntent(question=question, english_question=english, time_range=ex.time_range,
                                value_filters=ex.enum_matches, reader=reader, reading=getattr(ex, "reading", None),
                                literals=self._without_shape_words([question, english], ex.literals))
        wq = intent.working_question
        base = {"question": question, "reader": reader, "english_question": english,
                "reading": intent.reading.as_fact() if intent.reading else None, "field": None, "field_options": []}
        if self.memory is not None:
            intent.similar_cases = self.memory.similar(question_template(wq, intent.literals))
        if self.shapes.small_talk_kind(wq):
            return {**base, "answer_shape": {"choice": "small_talk", "probability": 1.0, "sure": True},
                    "entity": None, "sources": []}
        candidates, _ = self._candidates(intent)
        if candidates == ["catalog"]:
            return {**base, "answer_shape": {"choice": "catalog", "probability": 1.0, "sure": True},
                    "catalog_topic": catalog_topic(wq), "entity": None, "sources": []}
        browse = next((b for b in (browse_target(t, self._table_names()) for t in (question, english) if t) if b), None)
        if browse:  # "show me table owners": that table, nothing to ask
            return {**base, "answer_shape": {"choice": "browse", "probability": 1.0, "sure": True},
                    "entity": None, "sources": [{"source": browse[0], "probability": 1.0, "relevant": True}]}
        ranked = self.retriever.search(wq, self.top_k)
        names = [n for n, _ in ranked][: self.top_k]
        for case in intent.similar_cases:
            names += [n for n in case.get("sources") or [] if n in self.catalog.sources and n not in names]
        names += [n for n in self._reading_sources(intent) if n not in names]
        asks = [a for a in (self._entity_ask(intent, ex, {}), self._shape_ask(intent, {})) if a]
        asks += self._source_asks(intent, ex, names)
        self._reading_defaults(intent, asks)
        answers = ask_all(self.engine, asks)
        entity = answers.get("entity")
        shape = answers.get("answer_shape")
        sources = []
        for name in names:
            result = answers.get(f"source:{name}")
            if result is None:
                continue
            threshold = self.thresholds.critical if self.catalog.sources[name].critical else self.thresholds.source
            sources.append({"source": name, "probability": round(result.probability, 3),
                            "relevant": result.probability >= threshold})
        field = self._values_of_named_field(intent, {})
        if field:  # what else it could be about: the fields of the relevant tables, the named one first
            tables = [field.split(".")[0]] + [s["source"] for s in sources if s["relevant"]]
            fields = [f"{t}.{f}" for t in dict.fromkeys(tables) for f, d in self.catalog.sources[t].fields.items()
                      if f != self.catalog.sources[t].resolved_time_field]
            base["field"] = field
            base["field_options"] = [{"field": ref, "description": (self.catalog.field(ref).description or "").strip()}
                                     for ref in [field] + [r for r in fields if r != field]][:25]
        return {
            **base,
            "answer_shape": ({"choice": candidates[0], "probability": 1.0, "sure": True} if len(candidates) == 1 else
                             {"choice": shape.choice, "probability": round(shape.probability, 3),
                              "sure": shape.probability >= self.thresholds.answer_shape}),
            "entity": None if entity is None else {
                "choice": entity.choice, "probability": round(entity.probability, 3),
                "sure": entity.probability >= self.thresholds.entity,
                "ranked": [[e, round(p, 3)] for e, p in entity.ranked()],
                "descriptions": {e: d.description.strip() for e, d in self.catalog.entities.items()},
            },
            "sources": sources,
        }

    _IN_SCOPE_QUESTION = "Is the question about the data these tables hold?"

    def _small_talk(self, intent: SemanticIntent, decisions: List[DecisionRecord], pins: Dict[str, Any]) -> bool:
        if pins.get("in_scope") is True:
            return False
        kind = self.shapes.small_talk_kind(intent.working_question)
        if kind is None:
            return False
        intent.answer_shape, intent.small_talk = "small_talk", kind
        decisions.append(DecisionRecord(kind="answer_shape", question=self._SHAPE_QUESTION, answer="small_talk",
                                        probability=0.99, decided_by="deterministic", subject=kind))
        return True

    def _table_names(self) -> Dict[str, str]:
        """Every way to write a table in scope → its source: the source name, its bound table, ``_`` as spaces."""
        names: Dict[str, str] = {}
        for name, src in self.catalog.sources.items():
            if not in_scope(name) or name.startswith("_"):
                continue
            for n in (name, src.table or ""):
                if n and re.fullmatch(r"[\w.]+", n):
                    names.setdefault(n.lower(), name)
                    names.setdefault(n.lower().replace("_", " "), name)
        return names

    def _browse(self, intent: SemanticIntent, decisions: List[DecisionRecord], pins: Dict[str, Any]) -> bool:
        """
        "Show me table owners", "the alerts table", "mostre a tabela owners",
        "preview owners": the question names a table — its rows, every column,
        decided before anything else (no entity/activity/table question).
        Pin ``answer_shape`` to anything else to read it the usual way.
        """
        if pins.get("answer_shape") not in (None, "browse"):
            return False
        names = self._table_names()
        found = None
        for text in dict.fromkeys(t for t in (intent.question, intent.english_question) if t):
            found = browse_target(text, names)
            if found:
                break
        if found is None:
            return False
        intent.answer_shape, intent.browse_source, intent.row_limit = "browse", found[0], found[1]
        decisions.append(DecisionRecord(kind="answer_shape", question=self._SHAPE_QUESTION, answer="browse",
                                        probability=0.99, decided_by="deterministic",
                                        subject=f"the question names the table '{found[0]}'"))
        return True

    def _scope_summary(self) -> str:
        """What the catalog is about, for the in-scope question — tables, entities, activities."""
        if getattr(self, "_summary", None) is None:
            tables = "; ".join(f"{n} — {first_sentence(s.description) or n}"
                               for n, s in list(self.catalog.sources.items())[:40])
            entities = ", ".join(n.replace("_", " ") for n, e in self.catalog.entities.items() if not e.row_level)
            activities = ", ".join(n.replace("_", " ") for n in self.catalog.activities)
            self._summary = (f"Tables: {tables}." + (f" Things they identify: {entities}." if entities else "")
                             + (f" Activities: {activities}." if activities else ""))
        return self._summary

    def _in_scope_ask(self, intent: SemanticIntent, ex: Extraction, best_retrieval: float,
                      pins: Dict[str, Any]) -> Optional[Ask]:
        """
        "Is this about the data at all?" — one yes/no in the same batch. The
        offline engine reads a deterministic rule instead (``offline_prior``):
        no catalog word, value shape, known value, time range or retrieval
        hit means no.
        """
        if pins.get("in_scope") is True:
            return None
        nothing = (not ex.terms and not ex.enum_matches and ex.time_range is None and best_retrieval <= 0
                   and not any(lit.kind != "term" for lit in ex.literals)
                   and "catalog" not in self._candidates(intent)[0])  # "which systems are connected?" is about me
        return Ask(key="in_scope", question=self._IN_SCOPE_QUESTION, subject=self._scope_summary(),
                   criteria=CRITERIA["in_scope"],
                   state=intent.decision_state(terms=ex.terms, offline_prior=0.05 if nothing else 0.9))

    def _out_of_scope(self, intent: SemanticIntent, result: Any, decisions: List[DecisionRecord],
                      pins: Dict[str, Any]) -> bool:
        if pins.get("in_scope") is True:
            decisions.append(_by_user("in_scope", self._IN_SCOPE_QUESTION, True, None))
            return False
        if result is None:
            return False
        record = DecisionRecord(kind="in_scope", question=self._IN_SCOPE_QUESTION, answer=result.answer,
                                probability=result.probability, threshold=self.thresholds.out_of_scope)
        decisions.append(record)
        if result.probability >= self.thresholds.out_of_scope:
            return False  # about the data, or not sure: the normal path
        intent.answer_shape = "out_of_scope"
        return True

    def _about_the_catalog(self, intent: SemanticIntent, decisions: List[DecisionRecord], pins: Dict[str, Any]) -> bool:
        """"What kind of information do you have?" — answered from the catalog, before any other decision."""
        if pins.get("answer_shape") not in (None, "catalog"):
            return False
        candidates, words = self._candidates(intent)
        if pins.get("answer_shape") != "catalog" and candidates != ["catalog"]:
            return False
        intent.answer_shape = "catalog"
        intent.catalog_topic = catalog_topic(intent.working_question)
        words = f"{words}, topic: {intent.catalog_topic}" if words else f"topic: {intent.catalog_topic}"
        decisions.append(
            _by_user("answer_shape", self._SHAPE_QUESTION, "catalog", None) if pins.get("answer_shape") == "catalog"
            else DecisionRecord(kind="answer_shape", question=self._SHAPE_QUESTION, answer="catalog", probability=0.99,
                                decided_by="deterministic", subject=words))
        return True

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
        candidates, words = self._candidates(intent)
        if len(candidates) == 1:
            return None
        memory = _remembered(intent, "answer_shape", candidates)
        facts = {**({"wording": words} if words else {}), **memory.get("facts", {})}
        default = memory.get("default") or ("list" if candidates == ["list", "lookup"] else None)
        return Ask(key="answer_shape", question=self._SHAPE_QUESTION,
                   options={c: self.shapes.descriptions[c] for c in candidates},
                   state=intent.decision_state(facts=facts, default=default))

    def _candidates(self, intent: SemanticIntent) -> Tuple[List[str], str]:
        """
        The wording's candidates — plus, when nothing in the wording says
        what kind of answer and the question carries a value ("bring me
        information for 10.0.0.196"), everything about that value
        (``lookup``) as an alternative to a list: the engine reads which.
        """
        typed_value = any(lit.kind != "term" or lit.semantic_type for lit in intent.literals)
        # "which systems are connected to 10.0.0.5" is about the data: only a value-less question may be about me
        candidates, words = self.shapes.candidates(intent.working_question, about_me=not typed_value)
        if candidates == ["list"]:
            field = self._named_field(intent.working_question)
            if field is not None:  # "what are the severities of the events?" → severity's values
                return ["values"], f"'{field}' names a field, not a thing to list"
        if candidates == ["count"]:
            head = self.shapes.counted_head(intent.working_question)
            ref = self._field_at_head(head) if head else None
            if ref is not None:  # "how many departments do we have?" → how many different departments
                return ["count_values"], f"'{ref.split('.', 1)[1]}' names a field, not a thing to count"
        if candidates == ["list"] and any(lit.kind != "term" or lit.semantic_type for lit in intent.literals):
            candidates = ["list", "lookup"]
        return self._with_reading(intent, candidates, words)

    def _with_reading(self, intent: SemanticIntent, candidates: List[str], words: str) -> Tuple[List[str], str]:
        """
        The ``llm`` reader: the LLM's answer kind joins the rules' — when they
        agree nothing changes; when they don't, both go to the engine (it sees
        the reading as a fact) and a doubt is asked back. The rules' catalog
        reading stays final (it answers before anything is asked).
        """
        r = intent.reading
        if r is None or not r.answer or r.answer in ("small_talk", "out_of_scope") or candidates == ["catalog"]:
            return candidates, words
        if r.answer == "browse" and not (r.about_kind == "table" and r.about):
            return candidates, words  # a table to show, but which? the rules' reading stands
        if r.answer in candidates:
            return candidates, words
        note = f"the LLM read it as {r.answer}"
        return candidates + [r.answer], f"{words}; {note}" if words else note

    def _named_field(self, question: str) -> Optional[str]:
        """
        The field a "what are the <X> …" question asks for: <X> names a field
        of a table in scope, and none of its words is a thing the catalog
        lists (an entity or its keywords — "which are the users…" stays a list).
        """
        head = self.shapes.named_head(question)
        ref = self._field_named_in(head) if head else None
        if ref is None:  # "which departments do they have?": the field must be the head noun
            head = self.shapes.asked_head(question)
            ref = self._field_at_head(head) if head else None
        return ref.split(".", 1)[1] if ref else None

    def _field_named_in(self, text: str) -> Optional[str]:
        """``source.field`` of the in-scope field ``text`` names (all its words, longest name first) that
        isn't one of the things the catalog lists (an entity or its keywords), or ``None``."""
        said = set(content_stems(text))
        if not said:
            return None
        if getattr(self, "_entity_stems", None) is None:
            self._entity_stems = {st for name, ent in self.catalog.entities.items()
                                  for w in [name.replace("_", " "), *ent.keywords] for st in content_stems(w)}
        named = []
        for name, src in self.catalog.sources.items():
            if not in_scope(name):
                continue
            for field in src.fields:
                tokens = {stem(t) for t in tokenize(field.replace("_", " "))}
                if tokens and tokens <= said and not tokens & self._entity_stems:
                    named.append((len(tokens), f"{name}.{field}"))
        return max(named, key=lambda n: n[0])[1] if named else None

    # ------------------------------------------------------------------
    # the llm reader
    # ------------------------------------------------------------------

    READERS = ("rules", "llm", "llm_decides")
    #: The readers that start with the LLM's reading of the question.
    LLM_READERS = ("llm", "llm_decides")

    def _check_reader(self, reader: Optional[str]) -> str:
        reader = reader or self.reader
        if reader not in self.READERS:
            raise ValueError(f"unknown reader {reader!r}; use one of {', '.join(self.READERS)}")
        if reader in self.LLM_READERS and self.llm_reader is None:
            raise ValueError(f"the {reader} reader needs an LLM: set semantic.default_llm (or extractor.llm) to an "
                             "ai_providers entry, or use the rules reader")
        return reader

    _READING_QUESTION = "How did the LLM read the question?"

    def _reading_record(self, intent: SemanticIntent) -> DecisionRecord:
        r = intent.reading
        if r is None:
            return DecisionRecord(kind="llm_reading", question=self._READING_QUESTION, answer=None, probability=0.0,
                                  decided_by="llm", subject="no reading (the LLM failed or gave nothing usable)")
        about = f"{r.about_kind} {r.about}" if r.about else ""
        subject = "; ".join(x for x in (about, f"grouped by {r.group_by}" if r.group_by else "") if x)
        return DecisionRecord(kind="llm_reading", question=self._READING_QUESTION, answer=r.answer,
                              probability=1.0, decided_by="llm", subject=subject)

    def _reading_sources(self, intent: SemanticIntent) -> List[str]:
        r = intent.reading
        if r is None:
            return []
        refs = [r.about if r.about_kind in ("field", "table") else None, r.group_by]
        return [x.split(".")[0] for x in refs if x and x.split(".")[0] in self.catalog.sources
                and in_scope(x.split(".")[0])]

    def _reading_defaults(self, intent: SemanticIntent, asks: List[Ask]) -> None:
        """The offline lexical engine's no-evidence answer follows the reading (a real engine reads the facts)."""
        r = intent.reading
        if r is None:
            return
        for ask in asks:
            if ask.state.default is not None or ask.options is None:
                continue
            if ask.key == "answer_shape" and r.answer in ask.options:
                ask.state.default = r.answer
            elif ask.key == "entity" and r.about_kind == "entity" and r.about in ask.options:
                ask.state.default = r.about

    def _field_at_head(self, phrase: str) -> Optional[str]:
        """
        Like ``_field_named_in``, but the field must be what the phrase starts
        with ("departments I have" → department) — "how many hosts have a
        rule…" counts hosts, not rules, though it mentions ``rule``.
        """
        ref = self._field_named_in(phrase)
        if ref is None:
            return None
        first = content_stems(phrase)
        tokens = {stem(t) for t in tokenize(ref.split(".", 1)[1].replace("_", " "))}
        return ref if set(first[:len(tokens)]) == tokens else None

    def _values_of_named_field(self, intent: SemanticIntent, pins: Dict[str, Any]) -> Optional[str]:
        """
        "Show me the departments I have", "the different severities": the
        answer is a field's values and the question names that field — which
        thing it is "about" (the entity) doesn't matter, so it isn't asked.
        """
        shape = pins.get("answer_shape")
        if shape is None:
            candidates, _ = self._candidates(intent)
            shape = candidates[0] if len(candidates) == 1 else None
        if shape not in ("values", "count_values"):
            return None
        named = self._field_named_in(intent.working_question)
        r = intent.reading
        if named is None and r is not None and r.about_kind == "field" and r.about and in_scope(r.about.split(".")[0]):
            if self.catalog.field(r.about).semantic_type not in self.catalog.entities:
                return r.about  # the LLM read which field — not an entity's
        return named

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
        candidates, words = self._candidates(intent)
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
        if record.passed and result.choice == "catalog":
            intent.catalog_topic = catalog_topic(intent.working_question)
        if record.passed and result.choice == "browse" and intent.reading is not None:
            intent.browse_source = intent.reading.about
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


def _remembered(intent: SemanticIntent, key: str, options) -> Dict[str, Any]:
    """
    Similar confirmed questions as ``facts`` for the engine, and — for the
    offline lexical engine, which can't read facts — a very similar case's
    ``key`` as its no-evidence ``default``.
    """
    if not intent.similar_cases:
        return {}
    out: Dict[str, Any] = {"facts": {"similar_confirmed_questions": [as_fact(c) for c in intent.similar_cases]}}
    best = intent.similar_cases[0]
    if best.get("similarity", 0) >= 0.8 and best.get(key) in options:
        out["default"] = best[key]
    return out


def _by_user(kind: str, question: str, answer: Any, threshold: Optional[float], subject: str = "",
             probability: float = 1.0) -> DecisionRecord:
    """A decision the user settled by answering a clarification."""
    return DecisionRecord(kind=kind, question=question, subject=subject, answer=answer, probability=probability,
                          threshold=threshold, decided_by="user")


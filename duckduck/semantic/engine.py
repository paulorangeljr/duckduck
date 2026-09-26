"""
``SemanticSearch`` — the end-to-end pipeline:

    question → retrieval → decisions → intent → graph → plan → validate
             → optimize/execute (DuckAPI) → results

``search()`` never raises for an expected outcome: a low-confidence
decision comes back as ``status="needs_clarification"`` (never executed
on a guess), an invalid plan as ``status="invalid_plan"``.
"""

import inspect
import logging
from collections import OrderedDict
import os
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import pandas as pd

from .catalog import Catalog
from .clarify import ClarificationTexts
from .compiler import display_sql
from .decisions import DecisionEngine, DecisionState, JEVAdapter, LexicalDecisionEngine, using_engine
from .metering import metered
from .router import ModeRouter
from .executor import PlanExecutor, SourceFetch
from .extraction import ValueExtractor
from .graph import RelationshipGraph
from .intent import Clarification, ClarificationNeeded, ClarificationOption, DecisionRecord, ScoredSource, SemanticIntent, Thresholds
from .interpreter import SemanticInterpreter
from .plan import LogicalQueryPlan
from .planner import QueryPlanner, SourcePlan, TrivialAnswer
from .retrieval import CatalogRetriever
from . import scope
from .shapes import ACROSS_SHAPES, AnswerShapes, load_answer_shapes
from .validator import PlanValidationError, QueryValidator

logger = logging.getLogger("duckduck.semantic")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Section:
    """One table's part of a ``lookup`` / ``locate`` answer."""

    source: str
    description: str = ""
    #: The fields the value was looked up in (``matched_on``: those where it was found).
    checked: List[str] = field(default_factory=list)
    matched_on: List[str] = field(default_factory=list)
    rows: int = 0
    sql: str = ""
    #: The rows found (``lookup``); ``None`` for ``locate``, which only counts.
    results: Optional[pd.DataFrame] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source, "description": self.description, "checked": self.checked,
            "matched_on": self.matched_on, "rows": self.rows, "sql": self.sql,
            "results": _records(self.results) if self.results is not None else None,
        }


def _rows_found(sections: List[Section]) -> pd.DataFrame:
    """Every table's rows under one set of columns (a union by column name), ``source`` first."""
    frames = [s.results.assign(source=s.source) for s in sections if s.results is not None and not s.results.empty]
    if not frames:
        return pd.DataFrame(columns=["source"])
    rows = pd.concat(frames, ignore_index=True, sort=False)
    return rows[["source", *[c for c in rows.columns if c != "source"]]]


def _records(df: Optional[pd.DataFrame]) -> List[Dict[str, Any]]:
    return df.astype(object).where(df.notna(), None).to_dict(orient="records") if df is not None else []


@dataclass
class SearchResult:
    question: str
    #: ``ok`` / ``planned`` (plan only, not executed) /
    #: ``needs_clarification`` / ``invalid_plan``.
    status: str
    intent: Optional[SemanticIntent] = None
    query_plan: Optional[LogicalQueryPlan] = None
    #: Human-readable SQL, scanning each source as ``table(args)``.
    sql: Optional[str] = None
    results: Optional[pd.DataFrame] = None
    decisions: List[DecisionRecord] = field(default_factory=list)
    fetches: List[SourceFetch] = field(default_factory=list)
    elapsed_ms: float = 0.0
    clarification: Optional[str] = None
    options: List[str] = field(default_factory=list)
    #: When ``needs_clarification``: the question to ask the user back, with
    #: options (each pinning decisions) — see ``SemanticSearch.conversation``.
    followup: Optional[Clarification] = None
    #: The decisions the user had settled when this ran (``search(pinned=)``).
    pinned: Dict[str, Any] = field(default_factory=dict)
    #: Live checks run for the decision engine (``live_evidence``): field, value, found.
    evidence: List[Any] = field(default_factory=list)
    #: ``lookup`` / ``locate``: one entry per table checked.
    sections: List[Section] = field(default_factory=list)
    #: The id this search was recorded under (``SemanticSearch(feedback=...)``) — what feedback refers to.
    search_id: Optional[str] = None
    #: The conversation this round belongs to (``SemanticSearch.conversation``).
    conversation_id: Optional[str] = None
    #: The tables the user limited this question to (``search(only_sources=...)``), or ``None``.
    only_sources: Optional[List[str]] = None
    #: A direct reply instead of data — small talk ("hi", "thanks") or a question that isn't about
    #: the data (``intent.answer_shape`` says which) — and example questions to try instead.
    #: How it was read and decided: ``rules`` / ``llm`` / ``llm_decides`` (see ``SemanticInterpreter``).
    reader: Optional[str] = None
    #: What was asked for: the reader itself, or ``auto`` (the router picked ``reader`` — see ``route``).
    requested_reader: Optional[str] = None
    #: ``auto``: the mode it picked, the evidence per mode, the similar past questions.
    #: The readings planned and judged when the question was ambiguous (``hypotheses``): each one's
    #: label, pins, plain-words plan, SQL, and the engine's probability — best first.
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    route: Optional[Dict[str, Any]] = None
    #: What it cost: decision-engine and LLM calls, tokens, seconds, reported money (``metering.Usage``).
    usage: Dict[str, Any] = field(default_factory=dict)
    reply: Optional[str] = None
    suggestions: List[str] = field(default_factory=list)
    #: ``lookup`` / ``locate``: one row per table checked (source, found, rows, matched_on,
    #: description). For ``locate`` that *is* the answer, so ``results`` holds it too; for
    #: ``lookup``, ``results`` holds the rows found, every table's under one set of columns.
    summary: Optional[pd.DataFrame] = None

    @property
    def clarification_question(self) -> Optional[str]:
        """The question and numbered options to show the user (``None`` unless ``needs_clarification``)."""
        return self.followup.render() if self.followup else None

    @property
    def sources(self) -> List[ScoredSource]:
        return self.intent.candidate_sources if self.intent else []

    def report(self) -> str:
        """
        Human-readable summary — exactly what ``python -m duckduck.semantic
        ask`` prints: status, every decision with its probability, then the
        clarification or the SQL and the results.
        """
        lines = [f"[{self.status}] ({self.elapsed_ms:.0f} ms)"]
        for d in self.decisions:
            lines.append(f"  {d.kind:<24} {str(d.subject)[:40]:<40} {str(d.answer):<20} {d.probability:.2f}")
        if self.reply:
            lines += ["", self.reply] + [f"  - {s}" for s in self.suggestions]
            return "\n".join(lines)
        if self.clarification:
            lines += ["", self.clarification]
            if self.followup and self.followup.options:
                lines += ["", self.followup.render()]
            return "\n".join(lines)
        if self.sections:
            if self.summary is not None:
                lines += ["", self.summary.to_string(index=False)]
            for sec in self.sections:
                if sec.results is not None and not sec.results.empty:
                    lines += ["", f"── {sec.source} ({sec.rows} rows, matched on {', '.join(sec.matched_on)})",
                              sec.results.to_string(index=False)]
            return "\n".join(lines)
        if self.sql:
            lines += ["", self.sql]
        if self.results is not None:
            lines += ["", self.results.to_string(index=False)]
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly shape (``POST /search`` response)."""
        return {
            "question": self.question,
            "status": self.status,
            "search_id": self.search_id,
            "conversation_id": self.conversation_id,
            "only_sources": self.only_sources,
            "reader": self.reader,
            "requested_reader": self.requested_reader,
            "route": self.route,
            "hypotheses": self.hypotheses,
            "usage": self.usage,
            "reply": self.reply,
            "suggestions": self.suggestions,
            "intent": self.intent.model_dump(mode="json") if self.intent else None,
            "sources": [s.model_dump() for s in self.sources],
            "query_plan": self.query_plan.model_dump(mode="json") if self.query_plan else None,
            "sql": self.sql,
            "execution": {
                "elapsed_ms": round(self.elapsed_ms, 1),
                "fetches": [vars(f) for f in self.fetches],
            },
            "results": _records(self.results),
            "summary": _records(self.summary) if self.summary is not None else None,
            "sections": [s.to_dict() for s in self.sections],
            "decisions": [d.model_dump(mode="json") for d in self.decisions],
            "clarification": self.clarification,
            "options": self.options,
            "followup": self.followup.model_dump() if self.followup else None,
            "pinned": self.pinned,
            "evidence": [vars(p) for p in self.evidence],
        }

    def to_json(self, results_only: bool = False, **dumps_kwargs: Any) -> str:
        """
        ``to_dict()`` as a JSON string — dates and other non-JSON values
        become ISO/strings. ``results_only=True`` → just the answer rows
        (``[]`` when the search didn't get as far as running).
        """
        import json

        payload = self.to_dict()["results"] if results_only else self.to_dict()
        return json.dumps(payload, default=_json_default, **dumps_kwargs)


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, (list, dict)):  # numpy scalars → int / float / bool
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return str(value)


class SemanticSearch:
    """
    Parameters
    ----------
    catalog : Catalog or str
        The semantic catalog, or a path to its YAML/JSON file.
    duck : DuckAPI, optional
        The virtualization layer the catalog's ``table:`` sources are
        registered in (e.g. after ``duck.auto_register()``). Without it,
        only ``plan()`` works.
    engine : DecisionEngine, optional
        Defaults to ``LexicalDecisionEngine``; pass
        ``JEVAdapter(your_jev_client)`` for the real thing.
    strict : bool
        By default a catalog source whose ``table`` isn't registered in
        ``duck`` (a connector that failed ``auto_register(on_error="warn")``,
        say) is dropped with a ``RuntimeWarning`` and the rest keeps
        working. ``strict=True`` raises instead.
    """

    def __init__(
        self,
        catalog: Union[Catalog, str],
        duck: Any = None,
        engine: Optional[DecisionEngine] = None,
        retriever: Optional[CatalogRetriever] = None,
        extractor: Optional[ValueExtractor] = None,
        thresholds: Optional[Thresholds] = None,
        allowed_sources: Optional[Iterable[str]] = None,
        default_limit: int = 1000,
        clock: Callable[[], datetime] = _utcnow,
        strict: bool = False,
        clarification_texts: Optional[Dict[str, str]] = None,
        live_evidence: Union[bool, Dict[str, Any], None] = None,
        answer_shapes: Union[Dict[str, Any], str, None] = None,
        feedback: Any = None,
        memory: Union[bool, Dict[str, Any], Any] = None,
        reader: str = "rules",
        llm_reader: Any = None,
        llm_engine: Optional[DecisionEngine] = None,
        router: Any = None,
        router_options: Optional[Dict[str, Any]] = None,
        hypotheses: Union[bool, Dict[str, Any], Any] = True,
        stream: bool = True,
    ):
        if isinstance(catalog, str):
            catalog = Catalog.load(catalog)
        self.duck = duck
        self.catalog = self._available_subset(catalog, duck, strict) if duck is not None else catalog
        self.engine = engine or LexicalDecisionEngine()
        self.thresholds = thresholds or Thresholds()
        self.clock = clock
        self.graph = RelationshipGraph(self.catalog)
        #: The questions asked back to the user (``clarify.DEFAULT_TEXTS`` + overrides).
        self.texts = ClarificationTexts(clarification_texts)
        #: Probe the question's values in candidate sources before deciding (see ``evidence``):
        #: ``True`` or ``{"max_probes": 8, "timeout": 5.0}``; off by default.
        self.live_evidence = ({} if live_evidence is True else dict(live_evidence)) if live_evidence else None
        #: How each kind of answer is worded (``shapes.DEFAULT_WORDING`` + ``answer_shapes``).
        self.shapes = AnswerShapes(load_answer_shapes(answer_shapes))
        self.interpreter = SemanticInterpreter(
            self.catalog, self.engine, retriever=retriever, extractor=extractor, thresholds=self.thresholds,
            texts=self.texts, shapes=self.shapes,
        )
        #: How questions are read: ``rules`` (the wording, the engine settles ambiguity) or ``llm``
        #: (an LLM reads the question first; the engine decides between its reading and the
        #: rules'). ``llm_reader``: the ``LLMExtractor`` that reads (default: ``extractor``, if an LLM one).
        if llm_reader is not None:
            self.interpreter.llm_reader = llm_reader
        #: The ``llm_decides`` reader's decision engine: the LLM answers every decision Jev would
        #: (``LLMDecisionBackend``). Default: one over the llm reader's own LLM.
        self._llm_engine = llm_engine
        #: The Auto mode: picks a reader per question from how similar ones went (``router.ModeRouter``).
        self.router = router if router is not None else ModeRouter(
            feedback, threshold=self.thresholds.router, **(router_options or {}))
        self.reader = reader
        #: When a question would be asked back: plan every reading in parallel and let the engine judge
        #: the plans (``hypotheses.HypothesisJudge``; ``False``: ask back at once, as before).
        from .hypotheses import HypothesisJudge

        self.hypotheses = (hypotheses if isinstance(hypotheses, HypothesisJudge)
                           else HypothesisJudge(**hypotheses) if isinstance(hypotheses, dict)
                           else HypothesisJudge(enabled=bool(hypotheses)))
        self.planner = QueryPlanner(
            self.catalog, self.engine, graph=self.graph, thresholds=self.thresholds,
            allowed_sources=allowed_sources, default_limit=default_limit, texts=self.texts, shapes=self.shapes,
        )
        self.validator = QueryValidator(self.catalog, allowed_sources=allowed_sources)
        self._previews: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()  # preview() cache, per question text
        #: Auto's pick per question text — made once, while typing or at the search, and used by both, so
        #: the "Auto picked" chip is the mode that answers. Kept ``route_ttl`` seconds: then the history
        #: (which may have grown) is read again.
        self._routes: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self.route_ttl = 300.0
        self._table_labels: Optional[Dict[str, str]] = None
        #: ``FeedbackStore`` — every search is recorded (its decision trail, never its rows), and
        #: ``feedback()`` stores what the user said about it. ``None``: nothing recorded.
        self.feedback_store = feedback
        from .feedback import catalog_version

        self.catalog_version = catalog_version(self.catalog)
        backend = getattr(self.engine, "backend", None)
        self.engine_label = " ".join(x for x in (type(backend or self.engine).__name__,
                                                 getattr(backend, "model", None)) if x)
        if memory and feedback is not None:
            from .memory import CaseMemory

            memory = memory if isinstance(memory, CaseMemory) else CaseMemory(
                feedback, **(memory if isinstance(memory, dict) else {}))
            self.interpreter.memory = memory
        #: ``stream``: a source that can't be capped at the source is read page by page when its table
        #: has a streaming function, keeping only the rows its filters let through (see ``executor``).
        self.executor = PlanExecutor(self.catalog, duck, stream=stream) if duck is not None else None
        #: Answers registered as tables of their own (``take_over``), by name.
        self.taken_over: Dict[str, Any] = {}
        #: Why the ``llm`` reader couldn't be built (``from_config``), when it couldn't.
        self.llm_reader_error: Optional[str] = None

    def take_over(self, result: "SearchResult", name: Optional[str] = None, full: bool = True,
                  user: Optional[str] = None) -> Any:
        """
        "Take over from here": register ``result``'s rows as a table in the
        DuckAPI this search reads from, to query with ``duck.sql`` (see
        ``takeover``). ``full``: an answer that stopped at its row cap runs
        again without it. Returns a ``TakenOver`` (name, rows, columns, how).

        Taking the rows to work on is a yes: with a feedback store, an
        answer nobody rated yet is recorded as *answered*
        (``TakenOver.feedback``) — a rating the user gave stays.
        """
        from .takeover import TAKEOVER_REASON, take_over

        taken = take_over(self, result, name=name, full=full)
        store, search_id = self.feedback_store, getattr(result, "search_id", None)
        if store is not None and search_id:
            try:
                if store.verdict_of(search_id) is None:
                    taken.feedback = {"id": store.record_feedback(search_id, "answered", reason=TAKEOVER_REASON,
                                                                  user=user), "verdict": "answered"}
            except Exception as exc:  # a rating never costs the take-over
                logger.warning("feedback: couldn't record the take-over as answered (%s)", exc)
        return taken

    @classmethod
    def from_config(
        cls,
        duck: Any,
        config_path: Optional[str] = None,
        section: str = "semantic",
        **overrides: Any,
    ) -> "SemanticSearch":
        """
        Builds everything from the ``"semantic"`` section of
        ``duckduck.json`` (see ``duckduck.semantic.config``): catalog path,
        decision engine (lexical or Jev + its key), LLM extractor and its
        prompt, thresholds, authorization. ``overrides`` go straight to
        the constructor (e.g. ``clock=``). With
        ``catalog_generation.auto_refresh``, missing/expired sources are
        drafted into ``catalog_path`` first.
        """
        from .config import SemanticConfig

        cfg = SemanticConfig.load(duck, config_path, section)
        if cfg.catalog_generation.auto_refresh:
            _auto_refresh(cfg, duck)
        kwargs = dict(
            engine=cfg.build_engine(duck),
            thresholds=cfg.thresholds,
            clarification_texts=cfg.clarification_texts,
            answer_shapes=cfg.answer_shape_overrides(),
            memory={"max_cases": cfg.feedback.max_cases, "min_similarity": cfg.feedback.min_similarity}
            if cfg.feedback.memory else None,
            live_evidence=cfg.live_evidence.model_dump(exclude={"enabled"}) if cfg.live_evidence.enabled else None,
            allowed_sources=cfg.allowed_sources,
            default_limit=cfg.default_limit,
            strict=cfg.strict,
        )
        if "feedback" not in overrides:  # a caller's own store (or None) wins — never open the file twice
            kwargs["feedback"] = cfg.build_feedback()
        kwargs.update(overrides)
        reader = kwargs.pop("reader", cfg.reader)
        kwargs.setdefault("router_options", cfg.router.model_dump())
        kwargs.setdefault("hypotheses", cfg.hypotheses.model_dump())
        kwargs.setdefault("stream", cfg.stream)
        search = cls(cfg.path(cfg.catalog_path), duck, **kwargs)
        # built against the *available* catalog subset
        extractor = cfg.build_extractor(search.catalog, duck)
        if extractor is not None and "extractor" not in overrides:
            search.interpreter.extractor = extractor
        if "llm_reader" not in overrides:
            try:
                search.interpreter.llm_reader = cfg.build_llm_reader(search.catalog, duck, search.interpreter.extractor)
            except Exception as exc:
                if reader == "llm":
                    raise
                search.llm_reader_error = f"{exc.__class__.__name__}: {exc}"  # the llm reader just isn't offered
                logger.warning("semantic: the llm reader is unavailable (%s)", search.llm_reader_error)
        search.reader = reader
        return search

    # ------------------------------------------------------------------

    def conversation(self, question: str, execute: bool = True, max_rounds: int = 5,
                     user: Optional[str] = None, only_sources: Optional[Iterable[str]] = None,
                     pinned: Optional[Dict[str, Any]] = None, reader: Optional[str] = None) -> "Conversation":
        """
        Asks ``question`` and keeps the thread: while the result needs
        clarification, ``conversation.answer(reply)`` pins the chosen option
        and asks again — each round settles one decision, so it ends.
        """
        return Conversation(self, question, execute=execute, max_rounds=max_rounds, user=user,
                            only_sources=only_sources, pinned=pinned, reader=reader)

    def plan(self, question: str) -> SearchResult:
        """Interprets and plans ``question`` without executing anything."""
        return self.search(question, execute=False)

    @property
    def readers(self) -> List[str]:
        """The readers this search can use: ``rules``, plus ``llm``, ``llm_decides`` and ``auto`` with an LLM."""
        return ["rules"] + (["llm", "llm_decides", "auto"] if self.interpreter.llm_reader is not None else [])

    @property
    def llm_engine(self) -> Optional[DecisionEngine]:
        """The decision engine of the ``llm_decides`` reader (``None`` without an LLM)."""
        if self._llm_engine is None and self.interpreter.llm_reader is not None:
            from .llm_decisions import LLMDecisionBackend

            self._llm_engine = JEVAdapter(LLMDecisionBackend(self.interpreter.llm_reader.llm), timeout=60.0)
        return self._llm_engine

    def engine_for(self, reader: Optional[str]) -> DecisionEngine:
        """Who decides for ``reader``: the LLM for ``llm_decides``, else the configured engine (Jev / offline)."""
        return self.llm_engine if (reader or self.reader) == "llm_decides" else self.engine

    def engine_label_for(self, reader: Optional[str]) -> str:
        engine = self.engine_for(reader)
        backend = getattr(engine, "backend", None)
        llm = getattr(backend, "llm", None)
        model = getattr(backend, "model", None) or getattr(llm, "model", None) or getattr(llm, "model_id", None)
        return " ".join(str(x) for x in (type(backend or engine).__name__, model) if x)

    @property
    def reader(self) -> str:
        """The default reader: ``rules`` / ``llm`` / ``llm_decides`` / ``auto``."""
        return self._reader

    @reader.setter
    def reader(self, reader: str) -> None:
        if reader != "auto":
            self.interpreter.reader = self.interpreter._check_reader(reader)
        self._reader = reader

    def check_reader(self, reader: Optional[str]) -> str:
        """``reader`` (default: the search's), refused when unknown or unavailable — ``auto`` included."""
        reader = reader or self.reader
        if reader == "auto":
            return reader
        return self.interpreter._check_reader(reader)

    def route(self, question: str) -> Any:
        """
        The Auto mode's choice for ``question`` (a ``router.Route``) — asked
        of the configured engine once, then reused for ``route_ttl`` seconds:
        the preview's pick is the search's, never a second, different one.
        """
        from .memory import question_template

        key = " ".join(question.lower().split())
        hit = self._routes.get(key)
        if hit is not None and time.monotonic() - hit[0] < self.route_ttl:
            self._routes.move_to_end(key)
            return hit[1]
        literals = self.interpreter._preview_rules.extract(question, self.clock()).literals
        state = DecisionState(query=question)
        route = self.router.route(self.engine, question, question_template(question, literals),
                                  [r for r in self.readers if r != "auto"], state)
        self._routes[key] = (time.monotonic(), route)
        while len(self._routes) > 256:
            self._routes.popitem(last=False)
        return route

    def _route_is_fresh(self, question: str) -> bool:
        hit = self._routes.get(" ".join(question.lower().split()))
        return hit is not None and time.monotonic() - hit[0] < self.route_ttl

    def search(self, question: str, execute: bool = True, pinned: Optional[Dict[str, Any]] = None,
               conversation_id: Optional[str] = None, user: Optional[str] = None,
               only_sources: Optional[Iterable[str]] = None, reader: Optional[str] = None,
               requested_reader: Optional[str] = None) -> SearchResult:
        """
        Answers ``question``. ``pinned``: decisions settled by the user's
        answers to earlier clarifications (``ClarificationOption.pins``) —
        ``conversation()`` keeps track of them for you. ``only_sources``: the
        tables the user chose for this question (``preview()`` offers them) —
        nothing else is used, joins included. With a feedback store, the
        result is recorded (``result.search_id``).
        """
        only = self._check_scope(only_sources)
        requested = self.check_reader(reader)
        route = None
        with scope.only_sources(only), metered() as usage:
            if requested == "auto":  # which mode? from how similar questions went — the engine decides
                route = self.route(question)
            reader = route.reader if route is not None else requested
            with using_engine(self.engine_for(reader)):
                result = self._search(question, execute, pinned, reader)
                if result.status == "needs_clarification" and self.hypotheses.active(self.interpreter.engine):
                    result = self._explore(question, execute, pinned, reader, result)
        if route is not None:
            result.decisions.insert(0, route.record)
            result.route = {"reader": route.reader, "evidence": route.evidence,
                            "similar": [{"question": r.get("english_question") or r["question"],
                                         "reader": r["reader"], "outcome": r["outcome"],
                                         "similarity": r["similarity"]} for r in route.similar[:5]]}
        result.reader = reader
        result.requested_reader = requested_reader or requested
        result.usage = usage.to_dict()
        result.conversation_id = conversation_id
        if only is not None:
            result.only_sources = sorted(only)
            result.decisions.insert(0, DecisionRecord(
                kind="scope", question="Which tables may this question use?", subject=", ".join(sorted(only)),
                answer=sorted(only), probability=1.0, decided_by="user"))
        if self.feedback_store is not None:
            try:
                result.search_id = self.feedback_store.record_search(
                    result, conversation_id=conversation_id, user=user,
                    catalog_version=self.catalog_version, engine=self.engine_label_for(reader))
            except Exception as exc:  # recording must never cost the user their answer
                logger.warning("feedback: couldn't record the search (%s)", exc)
        return result

    def _explore(self, question: str, execute: bool, pinned: Optional[Dict[str, Any]], reader: Optional[str],
                 first: SearchResult) -> SearchResult:
        """
        The search stopped to ask: plan each reading the question offers (its
        options' pins) in parallel, let the engine judge the plans in one
        batch, and run the winner — or ask, as ``first`` does, when no
        reading is sure enough (see ``hypotheses``).
        """
        base = dict(pinned or {})
        readings = self.hypotheses.readings(lambda pins: self._search(question, False, pins, reader), base, first)
        if not readings:
            return first
        state = first.intent.decision_state() if first.intent is not None else DecisionState(query=question)
        winner, record, ranked = self.hypotheses.judge(self.interpreter.engine, question, readings, self.catalog,
                                                       state)
        seen = [h.to_dict() for h in ranked]
        if winner is None:
            first.hypotheses = seen
            if record is not None:
                first.decisions.append(record)  # why it still asks: no reading was sure enough
            return first
        final = self._search(question, execute, {**base, **winner.pins}, reader)
        before = {(d.kind, str(d.answer)) for d in first.decisions if d.decided_by == "user"}
        for d in final.decisions:  # what the winner's pins settled was the engine's judgment, not the user's
            if d.decided_by == "user" and (d.kind, str(d.answer)) not in before:
                d.decided_by = "hypothesis"
        final.decisions.insert(0, record)
        final.hypotheses = seen
        final.pinned = base
        return final

    def _check_scope(self, only_sources: Optional[Iterable[str]]) -> Optional[frozenset]:
        if only_sources is None:
            return None
        only = frozenset(only_sources)
        unknown = sorted(only - set(self.catalog.sources))
        if unknown:
            raise ValueError(f"only_sources: unknown table(s) {unknown}; known: {sorted(self.catalog.sources)}")
        if not only:
            raise ValueError("only_sources: choose at least one table")
        return only

    def preview(self, question: str, reader: Optional[str] = None) -> Dict[str, Any]:
        """
        What the question seems to be about, while it's being typed — one
        decision-engine batch (entity, answer kind, relevance of the
        candidate tables), nothing executed or recorded. ``reader``: ``rules``
        (rule-based extraction, no LLM) or ``llm`` (the LLM reads it first —
        cached, so the search that follows reuses the reading). ``systems``:
        every table the user may choose, grouped by the system it lives in
        (the ``auto_register`` service), those judged relevant marked — what
        the web app's "Systems" chip lists.
        """
        requested = self.check_reader(reader)
        key = requested + ":" + " ".join(question.lower().split())
        cached = self._previews.get(key)
        if cached is not None and requested == "auto" and not self._route_is_fresh(question):
            cached = None  # Auto's pick expired: route again, so the chip shows what the search will use
        if cached is not None:
            self._previews.move_to_end(key)
            return cached
        with metered() as usage:
            route = self.route(question) if requested == "auto" else None
            reader = route.reader if route is not None else requested
            with using_engine(self.engine_for(reader)):
                seen = self.interpreter.preview(question, self.clock(), reader=reader)
        seen["usage"] = usage.to_dict()
        if route is not None:  # the chip says which mode Auto picked, and why
            seen["route"] = {"reader": reader, "probability": round(route.record.probability, 3),
                             "why": route.record.subject, "evidence": route.evidence}
        if self.feedback_store is not None and usage.spent:  # the cost of typing, per mode
            try:
                self.feedback_store.record_preview(reader, usage.to_dict())
            except Exception as exc:
                logger.warning("feedback: couldn't record the preview's usage (%s)", exc)
        seen["joins"] = self._preview_joins(seen)
        seen["systems"] = self._systems({s["source"]: s for s in seen["sources"]})
        self._previews[key] = seen
        while len(self._previews) > 256:
            self._previews.popitem(last=False)
        return seen

    def _preview_joins(self, seen: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        The joins the answer would need: from each relevant table to the
        nearest one holding the entity the question asks for (the planner's
        own rule, ``_find_entity_field``). The tables on the way become
        relevant (``joined``), so the suggestion ticks them too.
        """
        entity = seen.get("entity") or {}
        shape = (seen.get("answer_shape") or {}).get("choice")
        ent = self.catalog.entities.get(entity.get("choice") or "")
        if not entity.get("sure") or ent is None or ent.row_level or shape in ("catalog", "small_talk", "lookup", "locate"):
            return []
        judged = {s["source"]: s for s in seen["sources"]}
        joins: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for name in [s["source"] for s in seen["sources"] if s["relevant"]]:
            try:
                found = self.planner._find_entity_field(name, entity["choice"], None, set(), set(), None)
            except Exception:  # a preview never fails
                continue
            if found is None:
                continue
            for edge in found[1].edges:
                joins.setdefault((edge.left, edge.right), {
                    "left": edge.left, "right": edge.right, "type": edge.type,
                    "confidence": round(edge.confidence, 3), "for": entity["choice"]})
                if edge.right_source not in judged:
                    judged[edge.right_source] = {"source": edge.right_source, "probability": None, "relevant": True}
                    seen["sources"].append(judged[edge.right_source])
                target = judged[edge.right_source]
                if not target["relevant"]:
                    target["relevant"] = True
                target["joined"] = True
        return list(joins.values())

    def _systems(self, judged: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Every table the user may use, grouped by system, relevant ones first."""
        labels: Dict[str, str] = {}
        if self.duck is not None and hasattr(self.duck, "list_tables"):
            if self._table_labels is None:
                try:
                    listed = self.duck.list_tables()
                    self._table_labels = dict(zip(listed["name"], listed["source"]))
                except Exception:
                    self._table_labels = {}
            labels = self._table_labels
        service_of = getattr(self.duck, "service_of", {}) if self.duck is not None else {}
        groups: Dict[str, Dict[str, Any]] = {}
        for name, src in self.catalog.sources.items():
            if not (self.planner.allowed is None or name in self.planner.allowed):
                continue
            table = (src.table or "").lower()
            system = service_of.get(table) or ("DuckDB" if src.relation else "other")
            group = groups.setdefault(system, {"system": system, "label": labels.get(table) or "", "sources": []})
            j = judged.get(name, {})
            group["sources"].append({
                "source": name, "description": src.description.strip().split(". ")[0].rstrip("."),
                "probability": j.get("probability"), "relevant": bool(j.get("relevant")),
                "joined": bool(j.get("joined")), "icon": self.source_icon(name),
            })
        out = list(groups.values())
        for g in out:
            icons = [s["icon"] for s in g["sources"]]
            g["icon"] = max(set(icons), key=icons.count) if icons else "api"
            g["sources"].sort(key=lambda s: (not s["relevant"], -(s["probability"] or 0), s["source"]))
            g["relevant"] = any(s["relevant"] for s in g["sources"])
        out.sort(key=lambda g: (not g["relevant"], g["system"]))
        return out

    def source_icon(self, name: str) -> str:
        """The kind of source behind a catalog source — sharepoint, database, servicenow, files, … (``admin.SOURCE_KINDS``)."""
        from .admin import source_kind

        src = self.catalog.sources.get(name)
        if src is None or src.relation:
            return "duckdb"
        fn = getattr(self.duck, "functions", {}).get((src.table or "").lower()) if self.duck is not None else None
        return source_kind(fn) if fn is not None else "api"

    def feedback(self, result: Union["SearchResult", str], verdict: str, categories: Iterable[str] = (),
                 reason: str = "", expected: Optional[Dict[str, Any]] = None, user: Optional[str] = None,
                 feedback_id: Optional[str] = None) -> str:
        """
        What the user said about a search (a ``SearchResult`` or its
        ``search_id``): ``verdict`` ``answered`` / ``partial`` /
        ``not_answered``, ``categories`` (``feedback.CATEGORIES``), ``reason``,
        and optionally ``expected`` — what was right (``sources``,
        ``answer_shape``, ``entity``, ``activity``, ``synonym``).
        ``feedback_id``: replace that earlier feedback instead of adding one.
        """
        if self.feedback_store is None:
            raise RuntimeError("no feedback store: build SemanticSearch(feedback=FeedbackStore(path)) "
                               "or set semantic.feedback.enabled")
        search_id = result if isinstance(result, str) else result.search_id
        if not search_id:
            raise ValueError("this result wasn't recorded (no search_id)")
        return self.feedback_store.record_feedback(search_id, verdict, categories, reason, expected, user,
                                                   feedback_id=feedback_id)

    def _search(self, question: str, execute: bool, pinned: Optional[Dict[str, Any]],
                reader: Optional[str] = None) -> SearchResult:
        started = time.perf_counter()
        now = self.clock()
        result = SearchResult(question=question, status="planned", pinned=dict(pinned or {}))
        prober = None
        if self.live_evidence is not None and self.duck is not None:
            from .evidence import EvidenceProber

            prober = EvidenceProber(self.catalog, self.duck, **self.live_evidence)
            result.evidence = prober.probes  # filled in as the probes run
        try:
            intent, decisions = self.interpreter.interpret(question, now, pinned, evidence=prober, reader=reader)
            result.intent = intent
            result.decisions.extend(decisions)
            if intent.answer_shape in ("small_talk", "out_of_scope"):  # a direct reply, nothing to look up
                self._reply(result, intent)
                result.elapsed_ms = (time.perf_counter() - started) * 1000
                return result
            if intent.answer_shape == "catalog":  # "what kind of information do you have?"
                result.results = self._catalog_answer(intent)
                result.status = "ok" if execute else "planned"
                result.elapsed_ms = (time.perf_counter() - started) * 1000
                return result
            if intent.answer_shape == "lookup" and not intent.resources:
                intent.answer_shape = "list"  # "tell me about the alerts": no value to look up — list them
            if intent.answer_shape == "browse":  # "show me table owners": that table, every column
                plan, plan_decisions = self.planner.plan_browse(intent), []
            elif intent.answer_shape not in ACROSS_SHAPES:
                try:
                    plan, plan_decisions = self.planner.plan(intent, pinned, evidence=prober)
                except TrivialAnswer as trivial:
                    result.decisions.append(DecisionRecord(
                        kind="answer_shape", question="What kind of answer does the question ask for?",
                        subject=f"a list would only repeat {trivial.value!r}", answer="lookup", probability=0.99,
                        decided_by="deterministic",
                    ))
                    intent.answer_shape = "lookup"
            if intent.answer_shape in ACROSS_SHAPES:
                self._across(result, intent, now, execute)
                result.elapsed_ms = (time.perf_counter() - started) * 1000
                return result
            result.decisions.extend(plan_decisions)
            result.query_plan = self.validator.validate(plan)
            result.sql = display_sql(plan, self.catalog, now)
        except ClarificationNeeded as exc:
            # everything decided before it stopped, then the decision that stopped it
            for d in [*getattr(exc, "decisions", []), *([exc.decision] if exc.decision is not None else [])]:
                if d not in result.decisions:
                    result.decisions.append(d)
            result.status = "needs_clarification"
            if result.intent is None:
                result.intent = getattr(exc, "intent", None)  # what was understood before the doubt
            result.clarification = exc.reason
            result.options = exc.options
            result.followup = exc.followup or Clarification(kind="unresolvable", question=exc.reason)
            result.elapsed_ms = (time.perf_counter() - started) * 1000
            return result
        except PlanValidationError as exc:
            result.status = "invalid_plan"
            result.clarification = str(exc)
            result.elapsed_ms = (time.perf_counter() - started) * 1000
            return result

        if execute:
            if self.executor is None:
                raise RuntimeError("SemanticSearch was built without a DuckAPI instance — use plan() or pass duck=.")
            result.results, _, result.fetches = self.executor.execute(result.query_plan, now)
            result.status = "ok"
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result

    # ------------------------------------------------------------------

    def _across(self, result: SearchResult, intent: SemanticIntent, now: datetime, execute: bool) -> None:
        """
        ``lookup`` ("everything about 10.0.0.196") and ``locate`` ("which
        tables have 10.0.0.196"): one validated plan per (table, field)
        holding the value, run separately; ``results`` is the summary, one
        row per table, and ``sections`` holds each table's part.
        """
        if intent.answer_shape == "locate" and not intent.resources:
            result.results = self._catalog_listing(intent)
            result.status = "ok" if execute else "planned"
            return
        plans = self.planner.plan_across(intent)
        for sp in plans:
            self.validator.validate(sp.plan)
        by_source: Dict[str, List[SourcePlan]] = {}
        for sp in plans:
            by_source.setdefault(sp.source, []).append(sp)
        several = len({sp.value for sp in plans}) > 1

        def label(sp: SourcePlan) -> str:
            return f"{sp.field} = {sp.value}" if several else sp.field

        for source, parts in by_source.items():
            sec = Section(source=source, description=self.catalog.sources[source].description.strip(),
                          checked=[label(sp) for sp in parts],
                          sql="\n\n".join(display_sql(sp.plan, self.catalog, now) for sp in parts))
            result.sections.append(sec)
            if not execute:
                continue
            if self.executor is None:
                raise RuntimeError("SemanticSearch was built without a DuckAPI instance — use plan() or pass duck=.")
            frames = []
            for sp in parts:
                df, _, fetches = self.executor.execute(sp.plan, now)
                result.fetches.extend(fetches)
                found = int(df["count"].iloc[0]) if intent.answer_shape == "locate" else len(df)
                if found:
                    sec.matched_on.append(label(sp))
                    sec.rows += found
                    frames.append(df)
            if intent.answer_shape == "lookup":
                sec.results = (pd.concat(frames, ignore_index=True).drop_duplicates().reset_index(drop=True)
                               if frames else pd.DataFrame(columns=list(self.catalog.sources[source].fields)))
                sec.rows = len(sec.results)
        result.sql = "\n\n".join(f"-- {s.source}\n{s.sql}" for s in result.sections) or None
        if execute:
            result.sections.sort(key=lambda s: -s.rows)  # where it was found first
            result.summary = pd.DataFrame(
                [{"source": s.source, "found": s.rows > 0, "rows": s.rows, "matched_on": ", ".join(s.matched_on),
                  "description": s.description} for s in result.sections],
                columns=["source", "found", "rows", "matched_on", "description"],
            )
            result.results = result.summary if intent.answer_shape == "locate" else _rows_found(result.sections)
            result.status = "ok"

    def _reply(self, result: SearchResult, intent: SemanticIntent) -> None:
        """Small talk, or a question that isn't about the data: a reply, what can be asked, examples."""
        texts = self.texts
        result.suggestions = self.example_questions()
        if intent.answer_shape == "small_talk":
            result.reply = texts.t(f"reply.{intent.small_talk or 'greeting'}")
            if intent.small_talk != "greeting":
                result.suggestions = []
        else:
            topics = self.topics()
            result.reply = " ".join(t for t in (
                texts.t("reply.out_of_scope"),
                texts.t("reply.topics", topics=topics) if topics else "",
            ) if t)
        if result.suggestions:
            result.reply += " " + texts.t("reply.examples")
        result.status = "ok"

    def topics(self, limit: int = 8) -> str:
        """What can be asked about, in words: the entities and activities of the tables in scope."""
        allowed = [n for n in self.catalog.sources if self.planner._is_allowed(n)]
        names: List[str] = []
        for n in allowed:
            src = self.catalog.sources[n]
            for e in src.entities:
                ent = self.catalog.entities.get(e)
                if ent is not None and not ent.row_level:
                    names.append(e)
            names += list(src.activities)
        words = [n.replace("_", " ") for n in dict.fromkeys(names)][:limit]
        if not words:
            words = allowed[:limit]
        return ", ".join(words[:-1]) + (" and " if len(words) > 1 else "") + words[-1] if words else ""

    def example_questions(self, limit: int = 5) -> List[str]:
        """A question each table answers (its catalog ``examples``), one per table first."""
        per_table = [self.catalog.sources[n].examples for n in self.catalog.sources
                     if self.planner._is_allowed(n) and self.catalog.sources[n].examples]
        out: List[str] = []
        for rank in range(max((len(e) for e in per_table), default=0)):
            for examples in per_table:
                if rank < len(examples) and examples[rank] not in out:
                    out.append(examples[rank])
                if len(out) >= limit:
                    return out
        return out

    def _systems_answer(self, allowed: List[str]) -> pd.DataFrame:
        """
        "Which systems are connected?": one row per system (the ``auto_register``
        service) with what it is, how many tables it registered and how many of
        those the catalog describes (what can be asked about). With
        ``allowed_sources``, only the systems behind allowed tables.
        """
        from .takeover import SERVICE as TAKEN_OVER

        listed: Dict[str, Dict[str, Any]] = {}
        if self.duck is not None and hasattr(self.duck, "list_tables"):
            try:
                listed = {r["name"]: r for r in self.duck.list_tables().to_dict(orient="records")}
            except Exception:
                listed = {}
        service_of = getattr(self.duck, "service_of", {}) if self.duck is not None else {}
        described = {(self.catalog.sources[n].table or "").lower(): n for n in allowed}
        restricted = self.planner.allowed is not None
        systems: Dict[str, Dict[str, Any]] = {}
        for table, row in listed.items():
            system = service_of.get(table) or "registered in code"
            if system == TAKEN_OVER or (restricted and table not in described):
                continue
            entry = systems.setdefault(system, {"system": system, "kind": row.get("source") or "", "tables": 0,
                                                "described": 0, "examples": []})
            entry["tables"] += 1
            if table in described:
                entry["described"] += 1
                if len(entry["examples"]) < 5:
                    entry["examples"].append(described[table])
        for name in allowed:  # native DuckDB relations: in the catalog, not registered anywhere
            if self.catalog.sources[name].relation:
                entry = systems.setdefault("DuckDB", {"system": "DuckDB", "kind": "DuckDB relation", "tables": 0,
                                                      "described": 0, "examples": []})
                entry["tables"] += 1
                entry["described"] += 1
                entry["examples"].append(name)
        rows = [{**e, "examples": ", ".join(e["examples"])} for e in sorted(systems.values(), key=lambda e: e["system"])]
        return pd.DataFrame(rows, columns=["system", "kind", "tables", "described", "examples"])

    def _catalog_answer(self, intent: SemanticIntent) -> pd.DataFrame:
        """A question about the catalog itself, by topic — never reads data."""
        topic = intent.catalog_topic or "tables"
        allowed = [n for n in self.catalog.sources if self.planner._is_allowed(n)]
        cat = self.catalog

        def holders(kind: str, name: str) -> List[str]:
            if kind == "entity":
                refs = [f"{s}.{f}" for s in allowed for f, d in cat.sources[s].fields.items() if d.semantic_type == name]
                refs += [r for r in cat.entity_fields(name) if r.split(".")[0] in allowed and r not in refs]
                srcs = {r.split(".")[0] for r in refs} | {s for s in allowed if name in cat.sources[s].entities}
                return sorted(srcs)
            return sorted(s for s in allowed if name in cat.sources[s].activities)

        if topic == "entities":
            rows = [{"entity": n, "description": e.description.strip(), "keywords": ", ".join(e.keywords),
                     "tables": ", ".join(holders("entity", n)),
                     "fields": ", ".join(f"{s}.{f}" for s in allowed for f, d in cat.sources[s].fields.items()
                                         if d.semantic_type == n),
                     "records_themselves": bool(e.row_level)}
                    for n, e in cat.entities.items()]
            return pd.DataFrame(rows, columns=["entity", "description", "keywords", "tables", "fields",
                                               "records_themselves"])
        if topic == "activities":
            rows = [{"activity": n, "description": a.description.strip(), "keywords": ", ".join(a.keywords),
                     "about": (a.resource or "").replace("_", " "), "tables": ", ".join(holders("activity", n))}
                    for n, a in cat.activities.items()]
            return pd.DataFrame(rows, columns=["activity", "description", "keywords", "about", "tables"])
        if topic == "fields":
            named = self._tables_named_in(intent.working_question, allowed) or allowed
            rows = [{"source": s, "field": f, "type": d.type, "meaning": (d.semantic_type or "").replace("_", " "),
                     "description": d.description.strip(),
                     "known_values": ", ".join(list(d.values)[:10]) + (" …" if len(d.values) > 10 else "")}
                    for s in named for f, d in cat.sources[s].fields.items()]
            return pd.DataFrame(rows, columns=["source", "field", "type", "meaning", "description", "known_values"])
        if topic == "systems":
            return self._systems_answer(allowed)
        if topic == "relationships":
            rows = [{"from": r.from_, "to": r.to, "type": r.type, "confidence": r.confidence}
                    for r in cat.relationships
                    if r.from_.split(".")[0] in allowed and (r.to.split(".")[0] in allowed or "." not in r.to)]
            return pd.DataFrame(rows, columns=["from", "to", "type", "confidence"])
        return self._catalog_overview()

    @staticmethod
    def _tables_named_in(question: str, names: List[str]) -> List[str]:
        """The tables a question names ("the columns of proxy logs" → proxy_logs)."""
        import re

        found = []
        for name in names:
            variants = {name, name.replace("_", " ")}
            if any(re.search(rf"(?<![\w]){re.escape(v)}(?![\w])", question, re.I) for v in variants):
                found.append(name)
        return found

    def _catalog_overview(self) -> pd.DataFrame:
        """What there is to ask about: each table, what it holds, and a question it answers — no data read."""
        rows = []
        for name, src in self.catalog.sources.items():
            if not self.planner._is_allowed(name):
                continue
            about = [e for e in src.entities if not getattr(self.catalog.entities.get(e), "row_level", False)]
            rows.append({
                "source": name, "description": src.description.strip(),
                "about": ", ".join(e.replace("_", " ") for e in about + [a.replace("_", " ") for a in src.activities]),
                "example_question": src.examples[0] if src.examples else "",
            })
        return pd.DataFrame(rows, columns=["source", "description", "about", "example_question"])

    def _catalog_listing(self, intent: SemanticIntent) -> pd.DataFrame:
        """"Which tables have IPs?" / "which tables exist?" — answered from the catalog, no data read."""
        entity = self.catalog.entities.get(intent.target_entity) if intent.target_entity else None
        rows = []
        for name, src in self.catalog.sources.items():
            if not self.planner._is_allowed(name):
                continue
            if entity is not None and not entity.row_level:
                fields = [f for f, d in src.fields.items() if d.semantic_type == intent.target_entity]
                fields += [r.split(".")[1] for r in self.catalog.entity_fields(intent.target_entity)
                           if r.split(".")[0] == name and r.split(".")[1] not in fields]
                if not fields:
                    continue
            else:
                fields = []
            rows.append({"source": name, "fields": ", ".join(fields), "description": src.description.strip()})
        return pd.DataFrame(rows, columns=["source", "fields", "description"])

    @staticmethod
    def _available_subset(catalog: Catalog, duck: Any, strict: bool) -> Catalog:
        """Drops (or, with ``strict``, rejects) sources whose DuckAPI table isn't usable."""
        problems: Dict[str, str] = {}
        for name, src in catalog.sources.items():
            if not src.table:
                continue
            fn = duck.functions.get(src.table.lower())
            if fn is None:
                problems[name] = f"table '{src.table}' is not registered in DuckAPI"
                continue
            params = inspect.signature(fn).parameters
            unknown = [a for a in src.args if a not in params]
            if unknown:
                problems[name] = f"'{src.table}' doesn't accept args {unknown}"
        if not problems:
            return catalog
        if strict:
            raise ValueError("unavailable catalog sources:\n  - " + "\n  - ".join(
                f"{n}: {p}" for n, p in problems.items()
            ))
        for name, problem in problems.items():
            with warnings.catch_warnings():
                warnings.simplefilter("always", RuntimeWarning)
                warnings.warn(f"SemanticSearch: source '{name}' unavailable ({problem}) — skipping.", RuntimeWarning, stacklevel=3)
        keep = {n: s for n, s in catalog.sources.items() if n not in problems}
        rels = [
            r for r in catalog.relationships
            if r.from_.split(".")[0] in keep and ("." not in r.to or r.to.split(".")[0] in keep)
        ]
        # A subset of an already-validated catalog: skip re-validation (an
        # activity's resource type may legitimately have lost its only source).
        return Catalog.model_construct(
            sources=keep, entities=catalog.entities, activities=catalog.activities, relationships=rels,
        )


def _auto_refresh(cfg: Any, duck: Any) -> None:
    """Drafts what's missing/expired before searching; a failure keeps the catalog as it is."""
    from .commands import refresh_catalog

    try:
        result = refresh_catalog(cfg, duck)
    except Exception as exc:
        if not os.path.isfile(cfg.path(cfg.catalog_path)):
            raise
        with warnings.catch_warnings():
            warnings.simplefilter("always", RuntimeWarning)
            warnings.warn(f"catalog auto_refresh failed, using the catalog as it is: {exc}", RuntimeWarning, stacklevel=3)
        return
    if result.changed:
        logger.info("catalog auto_refresh: %s", result.summary().replace("\n", "; "))


class Conversation:
    """
    A question plus the user's answers to its clarifications. Each answer
    pins a decision (``decided_by="user"``) instead of being pasted into the
    question, so the engine never re-judges what the user settled, and
    every round removes one doubt.

    A reply can be an option's number (``"2"``), its value or label, or
    free text — which the decision engine maps to an option (a choice
    question, no LLM); a reply it can't map clearly leaves the question
    open (``understood`` is ``None`` in ``history``).
    """

    def __init__(self, search: "SemanticSearch", question: str, execute: bool = True, max_rounds: int = 5,
                 user: Optional[str] = None, only_sources: Optional[Iterable[str]] = None,
                 pinned: Optional[Dict[str, Any]] = None, reader: Optional[str] = None):
        import uuid

        self.search = search
        #: How the question is read, every round (``rules`` / ``llm``; default: the search's).
        #: ``auto``: the router picks the mode on the first round, and every later round keeps it.
        self.requested_reader = search.check_reader(reader)
        self.reader = self.requested_reader
        self.question = question
        #: Ties every round's recorded search together (``SearchResult.conversation_id``).
        self.id = uuid.uuid4().hex[:16]
        self.user = user
        #: The tables the user chose before asking — kept for every round.
        self.only_sources = sorted(only_sources) if only_sources is not None else None
        self.execute = execute
        self.max_rounds = max_rounds
        self.pinned: Dict[str, Any] = dict(pinned or {})  # e.g. the entity chosen before asking
        #: One entry per reply: what was asked, the reply, the option it picked (or None).
        self.history: List[Dict[str, Any]] = []
        self.result: SearchResult = self._ask()

    def take_over(self, name: Optional[str] = None, full: bool = True) -> Any:
        """The latest answer as a table of its own — ``SemanticSearch.take_over``."""
        return self.search.take_over(self.result, name=name, full=full, user=self.user)

    @property
    def done(self) -> bool:
        """Answered, or nothing left to ask (no options: rephrase the question or fix the catalog)."""
        fu = self.result.followup
        return self.result.status != "needs_clarification" or fu is None or not fu.options

    @property
    def rounds(self) -> int:
        return sum(1 for h in self.history if h["understood"] is not None)

    def answer(self, reply: str) -> SearchResult:
        if self.done:
            raise ValueError("nothing to answer: the conversation has no open clarification")
        followup = self.result.followup
        option = followup.option(reply) or self._interpret(reply, followup)
        self.history.append({
            "asked": followup.question, "reply": reply,
            "understood": option.value if option else None,
            "pinned": dict(option.pins) if option else {},
        })
        if option is None:
            return self.result  # same question stays open
        self.pinned.update(option.pins)
        if self.rounds > self.max_rounds:
            self.result = SearchResult(
                question=self.question, status="needs_clarification", pinned=dict(self.pinned),
                clarification=f"Still unsure after {self.max_rounds} answers — try rephrasing the question.",
                followup=Clarification(kind="unresolvable", question="Try rephrasing the question."),
            )
            return self.result
        self.result = self._ask()
        return self.result

    def _ask(self) -> SearchResult:
        result = self.search.search(self.question, execute=self.execute, pinned=self.pinned,
                                    conversation_id=self.id, user=self.user, only_sources=self.only_sources,
                                    reader=self.reader, requested_reader=self.requested_reader)
        self.reader = result.reader  # auto: the mode it picked, for every later round
        return result

    def _interpret(self, reply: str, followup: Clarification) -> Optional[ClarificationOption]:
        """Free text → an option, by the decision engine (a choice question), when it's sure enough."""
        from .decisions import DecisionState
        from .text import content_stems

        options = {o.value: o.label for o in followup.options}
        result = self.search.engine_for(self.reader).classify(
            DecisionState(query=reply, terms=content_stems(reply), facts={"we_asked": followup.question}),
            "Which of the options does the user's reply choose?", options,
        )
        if result.probability < self.search.thresholds.reply:
            return None
        return next(o for o in followup.options if o.value == result.choice)

    @property
    def transcript(self) -> str:
        """The question, then each clarification and reply — the thread as text."""
        lines = [self.question]
        for h in self.history:
            lines.append(f"  Q: {h['asked']}")
            lines.append(f"  A: {h['reply']}" + ("" if h["understood"] else "  (not understood)"))
        return "\n".join(lines)

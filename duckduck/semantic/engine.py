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
import os
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

import pandas as pd

from .catalog import Catalog
from .clarify import ClarificationTexts
from .compiler import display_sql
from .decisions import DecisionEngine, LexicalDecisionEngine
from .executor import PlanExecutor, SourceFetch
from .extraction import ValueExtractor
from .graph import RelationshipGraph
from .intent import Clarification, ClarificationNeeded, ClarificationOption, DecisionRecord, ScoredSource, SemanticIntent, Thresholds
from .interpreter import SemanticInterpreter
from .plan import LogicalQueryPlan
from .planner import QueryPlanner
from .retrieval import CatalogRetriever
from .validator import PlanValidationError, QueryValidator

logger = logging.getLogger("duckduck.semantic")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        if self.clarification:
            lines += ["", self.clarification]
            if self.followup and self.followup.options:
                lines += ["", self.followup.render()]
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
            "intent": self.intent.model_dump(mode="json") if self.intent else None,
            "sources": [s.model_dump() for s in self.sources],
            "query_plan": self.query_plan.model_dump(mode="json") if self.query_plan else None,
            "sql": self.sql,
            "execution": {
                "elapsed_ms": round(self.elapsed_ms, 1),
                "fetches": [vars(f) for f in self.fetches],
            },
            "results": (
                self.results.astype(object).where(self.results.notna(), None).to_dict(orient="records")
                if self.results is not None else []
            ),
            "decisions": [d.model_dump(mode="json") for d in self.decisions],
            "clarification": self.clarification,
            "options": self.options,
            "followup": self.followup.model_dump() if self.followup else None,
            "pinned": self.pinned,
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
        self.interpreter = SemanticInterpreter(
            self.catalog, self.engine, retriever=retriever, extractor=extractor, thresholds=self.thresholds,
            texts=self.texts,
        )
        self.planner = QueryPlanner(
            self.catalog, self.engine, graph=self.graph, thresholds=self.thresholds,
            allowed_sources=allowed_sources, default_limit=default_limit, texts=self.texts,
        )
        self.validator = QueryValidator(self.catalog, allowed_sources=allowed_sources)
        self.executor = PlanExecutor(self.catalog, duck) if duck is not None else None

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
            allowed_sources=cfg.allowed_sources,
            default_limit=cfg.default_limit,
            strict=cfg.strict,
        )
        kwargs.update(overrides)
        search = cls(cfg.path(cfg.catalog_path), duck, **kwargs)
        # built against the *available* catalog subset
        extractor = cfg.build_extractor(search.catalog, duck)
        if extractor is not None and "extractor" not in overrides:
            search.interpreter.extractor = extractor
        return search

    # ------------------------------------------------------------------

    def conversation(self, question: str, execute: bool = True, max_rounds: int = 5) -> "Conversation":
        """
        Asks ``question`` and keeps the thread: while the result needs
        clarification, ``conversation.answer(reply)`` pins the chosen option
        and asks again — each round settles one decision, so it ends.
        """
        return Conversation(self, question, execute=execute, max_rounds=max_rounds)

    def plan(self, question: str) -> SearchResult:
        """Interprets and plans ``question`` without executing anything."""
        return self.search(question, execute=False)

    def search(self, question: str, execute: bool = True, pinned: Optional[Dict[str, Any]] = None) -> SearchResult:
        """
        Answers ``question``. ``pinned``: decisions settled by the user's
        answers to earlier clarifications (``ClarificationOption.pins``) —
        ``conversation()`` keeps track of them for you.
        """
        started = time.perf_counter()
        now = self.clock()
        result = SearchResult(question=question, status="planned", pinned=dict(pinned or {}))
        try:
            intent, decisions = self.interpreter.interpret(question, now, pinned)
            result.intent = intent
            result.decisions.extend(decisions)
            plan, plan_decisions = self.planner.plan(intent, pinned)
            result.decisions.extend(plan_decisions)
            result.query_plan = self.validator.validate(plan)
            result.sql = display_sql(plan, self.catalog, now)
        except ClarificationNeeded as exc:
            # everything decided before it stopped, then the decision that stopped it
            for d in [*getattr(exc, "decisions", []), *([exc.decision] if exc.decision is not None else [])]:
                if d not in result.decisions:
                    result.decisions.append(d)
            result.status = "needs_clarification"
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

    def __init__(self, search: "SemanticSearch", question: str, execute: bool = True, max_rounds: int = 5):
        self.search = search
        self.question = question
        self.execute = execute
        self.max_rounds = max_rounds
        self.pinned: Dict[str, Any] = {}
        #: One entry per reply: what was asked, the reply, the option it picked (or None).
        self.history: List[Dict[str, Any]] = []
        self.result: SearchResult = search.search(question, execute=execute, pinned=self.pinned)

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
        self.result = self.search.search(self.question, execute=self.execute, pinned=self.pinned)
        return self.result

    def _interpret(self, reply: str, followup: Clarification) -> Optional[ClarificationOption]:
        """Free text → an option, by the decision engine (a choice question), when it's sure enough."""
        from .decisions import DecisionState
        from .text import content_stems

        options = {o.value: o.label for o in followup.options}
        result = self.search.engine.classify(
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

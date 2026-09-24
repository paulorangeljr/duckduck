"""
``SemanticSearch`` — the end-to-end pipeline:

    question → retrieval → decisions → intent → graph → plan → validate
             → optimize/execute (DuckAPI) → results

``search()`` never raises for an expected outcome: a low-confidence
decision comes back as ``status="needs_clarification"`` (never executed
on a guess), an invalid plan as ``status="invalid_plan"``.
"""

import inspect
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

import pandas as pd

from .catalog import Catalog
from .compiler import display_sql
from .decisions import DecisionEngine, LexicalDecisionEngine
from .executor import PlanExecutor, SourceFetch
from .extraction import ValueExtractor
from .graph import RelationshipGraph
from .intent import ClarificationNeeded, DecisionRecord, ScoredSource, SemanticIntent, Thresholds
from .interpreter import SemanticInterpreter
from .plan import LogicalQueryPlan
from .planner import QueryPlanner
from .retrieval import CatalogRetriever
from .validator import PlanValidationError, QueryValidator


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
        }


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
    ):
        if isinstance(catalog, str):
            catalog = Catalog.load(catalog)
        self.duck = duck
        self.catalog = self._available_subset(catalog, duck, strict) if duck is not None else catalog
        self.engine = engine or LexicalDecisionEngine()
        self.thresholds = thresholds or Thresholds()
        self.clock = clock
        self.graph = RelationshipGraph(self.catalog)
        self.interpreter = SemanticInterpreter(
            self.catalog, self.engine, retriever=retriever, extractor=extractor, thresholds=self.thresholds,
        )
        self.planner = QueryPlanner(
            self.catalog, self.engine, graph=self.graph, thresholds=self.thresholds,
            allowed_sources=allowed_sources, default_limit=default_limit,
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
        the constructor (e.g. ``clock=``).
        """
        from .config import SemanticConfig

        cfg = SemanticConfig.load(duck, config_path, section)
        kwargs = dict(
            engine=cfg.build_engine(duck),
            thresholds=cfg.thresholds,
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

    def plan(self, question: str) -> SearchResult:
        """Interprets and plans ``question`` without executing anything."""
        return self.search(question, execute=False)

    def search(self, question: str, execute: bool = True) -> SearchResult:
        started = time.perf_counter()
        now = self.clock()
        result = SearchResult(question=question, status="planned")
        try:
            intent, decisions = self.interpreter.interpret(question, now)
            result.intent = intent
            result.decisions.extend(decisions)
            plan, plan_decisions = self.planner.plan(intent)
            result.decisions.extend(plan_decisions)
            result.query_plan = self.validator.validate(plan)
            result.sql = display_sql(plan, self.catalog, now)
        except ClarificationNeeded as exc:
            if exc.decision is not None and exc.decision not in result.decisions:
                result.decisions.append(exc.decision)
            result.status = "needs_clarification"
            result.clarification = exc.reason
            result.options = exc.options
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

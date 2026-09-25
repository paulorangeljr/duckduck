"""
Query Planner — ``SemanticIntent`` + catalog + relationship graph →
``LogicalQueryPlan``.

Deterministic: picks the *primary* source (the one where the activity
happens and the question's filters apply), then for everything the
question needs that the primary source doesn't have — the requested
entity, an enumerated filter living elsewhere — finds the best join path
through the relationship graph. Every field and join it commits to is
confirmed with the decision engine (field/relationship relevance), with
the catalog's own confidence as the prior, and must clear its threshold.
Those confirmations don't depend on each other, so the whole plan's go
out as one batch once it's laid out (``ask_all``), and are checked in
the order they were needed.
"""

from typing import Dict, Iterable, List, Optional, Set, Tuple

from .catalog import ActivityDef, Catalog
from .decisions import CRITERIA, Ask, DecisionEngine, DecisionState, ask_all
from .graph import Path, RelationshipGraph
from .intent import ClarificationNeeded, DecisionRecord, SemanticIntent, Thresholds
from .plan import Filter, Join, LogicalQueryPlan, TimeRangeFilter

#: Prior for "a field whose semantic_type matches exactly is the right one".
_SEMANTIC_MATCH_PRIOR = 0.95
#: Prior for "a field whose enumerated values contain the word used is the right one".
_ENUM_MATCH_PRIOR = 0.90


class QueryPlanner:
    def __init__(
        self,
        catalog: Catalog,
        engine: DecisionEngine,
        graph: Optional[RelationshipGraph] = None,
        thresholds: Optional[Thresholds] = None,
        allowed_sources: Optional[Iterable[str]] = None,
        default_limit: int = 1000,
        max_hops: int = 3,
    ):
        self.catalog = catalog
        self.engine = engine
        self.graph = graph or RelationshipGraph(catalog)
        self.thresholds = thresholds or Thresholds()
        self.allowed: Optional[Set[str]] = set(allowed_sources) if allowed_sources is not None else None
        self.default_limit = default_limit
        self.max_hops = max_hops

    def plan(self, intent: SemanticIntent) -> Tuple[LogicalQueryPlan, List[DecisionRecord]]:
        decisions: List[DecisionRecord] = []
        checks: List[tuple] = []  # (Ask, record kind, threshold, message if it fails, options)
        activity = self.catalog.activities.get(intent.activity) if intent.activity else None
        primary = self._choose_primary(intent, activity)
        paths: List[Path] = []
        filters: List[Filter] = []
        resource_fields: Set[str] = set()

        # 1. values from the question typed by resource (github → domain)
        for res in intent.resources:
            fname = self._pick_field(primary, res.type, activity.resource_role if activity else None)
            ref = f"{primary}.{fname}"
            checks.append(self._field_check(intent, ref, f"filtering on {res.value!r} ({res.type})", _SEMANTIC_MATCH_PRIOR, len(checks)))
            fdef = self.catalog.field(ref)
            operator = "eq" if res.literal_kind in ("ip_address", "email") else (fdef.match or "eq")
            filters.append(Filter(field=ref, operator=operator, value=res.value))
            resource_fields.add(fname)

        # 2. enumerated values ("denied" → firewall_logs.action = 'DENY'),
        #    one filter per phrase, preferring the primary source
        by_term: Dict[str, List] = {}
        for match in intent.value_filters:
            by_term.setdefault(match.term, []).append(match)
        for term, matches in by_term.items():
            match, path = self._locate_enum(primary, term, matches)
            if path.edges:
                paths.append(path)
            checks.append(self._field_check(
                intent, match.field, f"the value {term!r}", _ENUM_MATCH_PRIOR, len(checks),
                # the catalog lists the stored value — ask about that exact reading
                question=f"Does {term!r} in the question mean {match.field} = {match.value!r}?",
                failure=f"Not confident that {term!r} means {match.field} = {match.value!r}.",
            ))
            filters.append(Filter(field=match.field, operator="eq", value=match.value))

        # 3. time range, always on the primary source's event time
        time_range = None
        if intent.time_range:
            tr = intent.time_range
            time_range = TimeRangeFilter(
                field=f"{primary}.{self.catalog.sources[primary].resolved_time_field}",
                last_hours=tr.last_hours, start=tr.start, end=tr.end,
            )

        # 4. what to return
        entity = self.catalog.entities.get(intent.target_entity) if intent.target_entity else None
        if entity is None or entity.row_level:
            select = [f"{primary}.{f}" for f in self.catalog.sources[primary].fields]
            distinct = False
        else:
            ref, path = self._locate_entity(primary, intent.target_entity, activity, resource_fields)
            if path.edges:
                paths.append(path)
            prior = min(self.catalog.entity_fields(intent.target_entity).get(ref, _SEMANTIC_MATCH_PRIOR), _SEMANTIC_MATCH_PRIOR)
            checks.append(self._field_check(intent, ref, f"identifying the requested {intent.target_entity}", prior, len(checks)))
            select = [ref]
            distinct = True

        sources, joins = self._merge_paths(intent, primary, paths, checks)
        self._run_checks(checks, decisions)
        plan = LogicalQueryPlan(
            select=select, sources=sources, joins=joins, filters=filters,
            time_range=time_range, distinct=distinct, limit=self.default_limit,
        )
        return plan, decisions

    # ------------------------------------------------------------------
    # Primary source
    # ------------------------------------------------------------------

    def _choose_primary(self, intent: SemanticIntent, activity: Optional[ActivityDef]) -> str:
        eligible = []
        for cand in intent.candidate_sources:
            if not self._is_allowed(cand.source):
                continue
            src = self.catalog.sources[cand.source]
            if intent.time_range and not src.resolved_time_field:
                continue
            if any(not self.catalog.fields_by_semantic_type(cand.source, r.type) for r in intent.resources):
                continue
            supports = bool(intent.activity) and intent.activity in src.activities
            eligible.append(((supports, cand.confidence, cand.retrieval_score), cand.source))
        if not eligible:
            needs = []
            if intent.time_range:
                needs.append("a time field")
            needs += [f"a '{r.type}' field (for {r.value!r})" for r in intent.resources]
            raise ClarificationNeeded(
                "None of the relevant sources ("
                + ", ".join(c.source for c in intent.candidate_sources)
                + ") has " + (" and ".join(needs) or "what this question needs") + ".",
                options=[c.source for c in intent.candidate_sources],
            )
        eligible.sort(key=lambda e: e[0], reverse=True)
        return eligible[0][1]

    # ------------------------------------------------------------------
    # Field location
    # ------------------------------------------------------------------

    def _pick_field(self, source: str, semantic_type: str, prefer_role: Optional[str], exclude: Iterable[str] = ()) -> Optional[str]:
        candidates = [f for f in self.catalog.fields_by_semantic_type(source, semantic_type) if f not in set(exclude)]
        if not candidates:
            return None
        if prefer_role:
            for f in candidates:
                if self.catalog.sources[source].fields[f].role == prefer_role:
                    return f
        return candidates[0]

    def _locate_enum(self, primary: str, term: str, matches: list):
        for m in matches:
            if m.field.split(".")[0] == primary:
                return m, Path(primary)
        best = None
        for m in matches:
            target = m.field.split(".")[0]
            path = self.graph.find_path(primary, lambda s, t=target: s == t, self.max_hops, self._is_allowed)
            if path and (best is None or path.confidence > best[1].confidence):
                best = (m, path)
        if best is None:
            raise ClarificationNeeded(
                f"{term!r} is a value of {', '.join(m.field for m in matches)}, which can't be "
                f"connected to '{primary}' through any known relationship.",
                options=[m.field for m in matches],
            )
        return best

    def _locate_entity(self, primary: str, entity: str, activity: Optional[ActivityDef], resource_fields: Set[str]):
        actor_role = activity.actor_role if activity else None
        represents = self.catalog.entity_fields(entity)

        def field_in(source: str, exclude: Iterable[str] = ()) -> Optional[str]:
            fname = self._pick_field(source, entity, actor_role, exclude)
            if fname is None:
                # explicit "represents" relationships (e.g. owner → user)
                fname = next(
                    (ref.split(".")[1] for ref in represents
                     if ref.split(".")[0] == source and ref.split(".")[1] not in set(exclude)),
                    None,
                )
            return fname

        direct = field_in(primary, exclude=resource_fields)
        if direct:
            return f"{primary}.{direct}", Path(primary)
        path = self.graph.find_path(
            primary, lambda s: s != primary and field_in(s) is not None, self.max_hops, self._is_allowed,
        )
        if path is None:
            raise ClarificationNeeded(
                f"Couldn't find any '{entity}' field reachable from '{primary}'.",
                options=sorted(represents),
            )
        return f"{path.end}.{field_in(path.end)}", path

    # ------------------------------------------------------------------
    # Confirmation decisions + join assembly
    # ------------------------------------------------------------------

    def _field_check(self, intent: SemanticIntent, ref: str, purpose: str, prior: float, n: int,
                     question: Optional[str] = None, failure: Optional[str] = None) -> tuple:
        ask = Ask(
            key=f"field:{n}", question=question or f"Is {ref} relevant for {purpose}?",
            subject=self.catalog.describe_field(ref), criteria=CRITERIA["field"],
            state=DecisionState(query=intent.question, prior=prior, facts={"field": ref}),
        )
        return ask, "field_relevance", ref, self.thresholds.field, \
            failure or f"Not confident that {ref} is the right field for {purpose}.", [ref]

    def _merge_paths(self, intent: SemanticIntent, primary: str, paths: List[Path], checks: List[tuple]):
        sources, joins = [primary], []
        for path in paths:
            for edge in path.edges:
                if edge.right_source in sources:
                    continue  # already joined in (one alias per source)
                ask = Ask(
                    key=f"join:{len(checks)}",
                    question=f"Is {edge.right_source} relevant for resolving {edge.left} via {edge.right}?",
                    subject=f"{edge.left} {edge.type} {edge.right}", criteria=CRITERIA["relationship"],
                    state=DecisionState(query=intent.question, prior=edge.confidence, facts={"relationship": edge.type}),
                )
                checks.append((ask, "relationship_relevance", f"{edge.left} = {edge.right}", self.thresholds.relationship,
                               f"Not confident enough in joining {edge.left} = {edge.right}", None))
                sources.append(edge.right_source)
                joins.append(Join(left=edge.left, right=edge.right))
        return sources, joins

    def _run_checks(self, checks: List[tuple], decisions: List[DecisionRecord]) -> None:
        """Every confirmation in one batch; the first to miss its threshold (in plan order) stops the plan."""
        answers = ask_all(self.engine, [c[0] for c in checks])
        for ask, kind, subject, threshold, message, options in checks:
            result = answers[ask.key]
            record = DecisionRecord(
                kind=kind, question=ask.question, subject=subject,
                answer=result.answer, probability=result.probability, threshold=threshold,
            )
            decisions.append(record)
            if not record.passed:
                if kind == "relationship_relevance":
                    message = f"{message} ({result.probability:.2f})."
                exc = ClarificationNeeded(message, record, options)
                exc.decisions = list(decisions)  # the confirmations that did pass, for the audit
                raise exc

    def _is_allowed(self, source: str) -> bool:
        return self.allowed is None or source in self.allowed

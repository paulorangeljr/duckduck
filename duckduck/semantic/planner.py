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

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .catalog import ActivityDef, Catalog
from .decisions import CRITERIA, Ask, DecisionEngine, DecisionState, ask_all
from .graph import Path, RelationshipGraph
from .clarify import ClarificationTexts
from .intent import FIELD_SHAPES, Clarification, ClarificationNeeded, DecisionRecord, SemanticIntent, Thresholds
from .shapes import AnswerShapes
from .plan import Filter, Join, LogicalQueryPlan, TimeRangeFilter
from .text import content_stems, stem, tokenize

class TrivialAnswer(Exception):
    """The list asked for would only repeat the value the question gave ("which ips … ip = X")."""

    def __init__(self, value: str, field: str):
        super().__init__(f"the answer would only repeat {value!r} ({field})")
        self.value, self.field = value, field


@dataclass
class SourcePlan:
    """One table's part of a ``lookup`` / ``locate`` answer: one value matched on one of its fields."""

    source: str
    field: str
    value: str
    plan: LogicalQueryPlan


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
        texts: Optional[ClarificationTexts] = None,
        shapes: Optional[AnswerShapes] = None,
    ):
        self.catalog = catalog
        self.shapes = shapes or AnswerShapes()
        self.engine = engine
        self.graph = graph or RelationshipGraph(catalog)
        self.thresholds = thresholds or Thresholds()
        self.allowed: Optional[Set[str]] = set(allowed_sources) if allowed_sources is not None else None
        self.default_limit = default_limit
        self.max_hops = max_hops
        #: The questions asked back to the user.
        self.texts = texts or ClarificationTexts()

    def plan(self, intent: SemanticIntent, pinned: Optional[Dict[str, Any]] = None,
             evidence: Any = None) -> Tuple[LogicalQueryPlan, List[DecisionRecord]]:
        """
        ``pinned``: what the user answered (``field:<ref>``, ``join:<a>=<b>``,
        ``term:<words>``). A "no" isn't the end: the refused field / join is
        left out and the plan looks for another way (another field or
        source, another join path); when there is none, the follow-up
        offers what is still possible.
        """
        pins = dict(pinned or {})
        refused = {k[6:] for k, v in pins.items() if k.startswith("field:") and v is False}
        blocked = {frozenset(k[5:].split("=", 1)) for k, v in pins.items() if k.startswith("join:") and v is False}
        decisions: List[DecisionRecord] = []
        checks: List[tuple] = []  # (Ask, record kind, threshold, message if it fails, options)
        activity = self.catalog.activities.get(intent.activity) if intent.activity else None
        primary = self._choose_primary(intent, activity, refused)
        paths: List[Path] = []
        filters: List[Filter] = []
        resource_fields: Set[str] = set()

        # 1. values from the question typed by resource (github → domain)
        for res in intent.resources:
            usable = self._usable_fields(primary, res.type, refused)
            fname = self._pick_field(primary, res.type, activity.resource_role if activity else None,
                                     exclude=set(self.catalog.fields_by_semantic_type(primary, res.type)) - set(usable))
            ref = f"{primary}.{fname}"
            only_one = len(usable) == 1
            check = self._field_check(
                intent, ref, f"filtering on {res.value!r} ({res.type})", _SEMANTIC_MATCH_PRIOR, len(checks),
                settled=only_one, followup=self.texts.field_filter(intent.question, ref, res.value, self.catalog),
            )
            if evidence is not None and not check[-1]:  # asked: tell the engine whether the value is there
                check[0].state.facts["live_check"] = {"value": res.value, "found": evidence.found(ref, res.value)}
            checks.append(check)
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
            answer = pins.get(f"term:{term}")
            if answer == "ignore":  # the user said to answer without it
                decisions.append(DecisionRecord(kind="value_filter", question=f"Filter on {term!r}?", subject=term,
                                                answer="ignored", probability=1.0, decided_by="user"))
                continue
            if answer is False:
                exc = ClarificationNeeded(f"You'll rephrase the question without {term!r}.")
                exc.decisions = list(decisions)
                raise exc
            matches = [m for m in matches if m.field not in refused]
            if not matches:  # every reading of the term was refused
                raise ClarificationNeeded(
                    f"No other meaning of {term!r} found in the catalog.", None, [],
                    self.texts.ignore_term(intent.question, term),
                )
            match, path = self._locate_enum(primary, term, matches, blocked, intent)
            if path.edges:
                paths.append(path)
            checks.append(self._field_check(
                intent, match.field, f"the value {term!r}", _ENUM_MATCH_PRIOR, len(checks),
                # the catalog lists the stored value — ask about that exact reading
                question=f"Does {term!r} in the question mean {match.field} = {match.value!r}?",
                failure=f"Not confident that {term!r} means {match.field} = {match.value!r}.",
                followup=self.texts.field_value(intent.question, match.field, term, match.value, self.catalog),
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
        shape = intent.answer_shape
        shape_ref = self._shape_field(intent, shape, primary, refused, pins, decisions) if shape in FIELD_SHAPES else None
        if shape in FIELD_SHAPES and shape_ref is None:  # "the different users": the entity answer is already distinct
            shape = "count" if shape == "count_values" else "list"
        group_by: List[str] = []
        if shape in ("values", "count_values"):  # "the different severities": the distinct values of that field
            select, distinct = [shape_ref], True
        elif entity is None or entity.row_level:
            select = [f"{primary}.{f}" for f in self.catalog.sources[primary].fields]
            distinct = False
        else:
            ref, path = self._locate_entity(primary, intent.target_entity, activity, resource_fields,
                                            refused, blocked, intent)
            if path.edges:
                paths.append(path)
            prior = min(self.catalog.entity_fields(intent.target_entity).get(ref, _SEMANTIC_MATCH_PRIOR), _SEMANTIC_MATCH_PRIOR)
            src = ref.split(".")[0]
            choices = set(self.catalog.fields_by_semantic_type(src, intent.target_entity)) | {
                r.split(".")[1] for r in self.catalog.entity_fields(intent.target_entity) if r.split(".")[0] == src
            }
            choices -= {r.split(".")[1] for r in refused if r.split(".")[0] == src}
            if src == primary:
                choices -= resource_fields
            label = intent.target_entity + (f" — {entity.description.strip()}" if entity.description.strip() else "")
            checks.append(self._field_check(
                intent, ref, f"identifying the requested {intent.target_entity}", prior, len(checks),
                settled=len(choices) == 1,
                question=f"Does {ref} hold the {intent.target_entity} the question asks for ({label})?",
                followup=self.texts.field_return(intent.question, ref, intent.target_entity, self.catalog),
            ))
            select = [ref]
            distinct = True
            if shape == "list" and pins.get("answer_shape") != "list":  # "which ips … 10.0.0.196": only the value
                same = _equal_to(ref, paths)
                for res, flt in zip(intent.resources, filters):
                    if flt.operator == "eq" and flt.field in same:
                        raise TrivialAnswer(res.value, ref)
        if shape == "count_by":  # per group: how many distinct entities, or how many records
            group_by = [shape_ref]
            if not distinct:
                select = [shape_ref]

        sources, joins = self._merge_paths(intent, primary, paths, checks)
        self._run_checks(checks, decisions, pins)
        plan = LogicalQueryPlan(
            select=select, sources=sources, joins=joins, filters=filters,
            time_range=time_range, distinct=distinct, limit=self.default_limit,
            aggregate="count" if shape in ("count", "count_values", "count_by") else None,
            group_by=group_by,
        )
        return plan, decisions

    def plan_across(self, intent: SemanticIntent) -> List[SourcePlan]:
        """
        ``lookup`` / ``locate``: one plan per (value, table, field) — every
        allowed source with a field of the value's type, not just the
        retrieved ones; each value of the question looked up on its own.
        ``lookup`` selects every column, ``locate`` counts. The question's
        enumerated values and time range apply where the table has them; a
        table without a time field is skipped when the question has a time
        range.
        """
        out: List[SourcePlan] = []
        for res in intent.resources:
            for name, src in self.catalog.sources.items():
                if not self._is_allowed(name) or (intent.time_range and not src.resolved_time_field):
                    continue
                for fname in self.catalog.fields_by_semantic_type(name, res.type):
                    filters = [self._value_filter(name, fname, res)]
                    filters += [Filter(field=m.field, operator="eq", value=m.value)
                                for m in intent.value_filters if m.field.split(".")[0] == name]
                    time_range = None
                    if intent.time_range:
                        tr = intent.time_range
                        time_range = TimeRangeFilter(field=f"{name}.{src.resolved_time_field}", last_hours=tr.last_hours,
                                                     start=tr.start, end=tr.end)
                    counting = intent.answer_shape == "locate"
                    plan = LogicalQueryPlan(
                        select=[f"{name}.{fname}"] if counting else [f"{name}.{f}" for f in src.fields],
                        sources=[name], filters=filters, time_range=time_range, limit=self.default_limit,
                        aggregate="count" if counting else None,
                    )
                    out.append(SourcePlan(source=name, field=fname, value=res.value, plan=plan))
        return out

    def _value_filter(self, source: str, fname: str, res) -> Filter:
        fdef = self.catalog.sources[source].fields[fname]
        operator = "eq" if res.literal_kind in ("ip_address", "email") else (fdef.match or "eq")
        return Filter(field=f"{source}.{fname}", operator=operator, value=res.value)

    _FIELD_QUESTIONS = {
        "values": "Which field's different values does the question ask for?",
        "count_values": "Which field's different values does the question count?",
        "count_by": "Which field does the question want a count for each value of?",
    }

    def _shape_field(self, intent: SemanticIntent, shape: str, primary: str, refused: Set[str],
                     pins: Dict[str, Any], decisions: List[DecisionRecord]) -> Optional[str]:
        """
        The field a ``values`` / ``count_values`` / ``count_by`` answer is
        about: pinned by the user; else the one the question names
        ("severities" → ``severity``; for a count per group, the word after
        "per"/"by" wins); else — unless a real entity already makes the
        answer distinct ("the different users") — the engine picks among
        the primary source's fields, and a doubt is asked back.
        """
        src = self.catalog.sources[primary]
        question = self._FIELD_QUESTIONS[shape]
        pinned = pins.get("values_field")
        if isinstance(pinned, str) and pinned.split(".")[0] == primary and pinned.split(".", 1)[-1] in src.fields:
            decisions.append(DecisionRecord(kind="values_field", question=question, subject=primary, answer=pinned,
                                            probability=1.0, decided_by="user"))
            return pinned
        fields = [f for f in src.fields if f"{primary}.{f}" not in refused]
        words = set(content_stems(intent.question))
        named = []
        for f in fields:
            tokens = {stem(t) for t in tokenize(f.replace("_", " "))}
            if tokens and tokens <= words:
                named.append((tokens, f))
        if shape == "count_by":  # "how many ips per rule": the group is the word after "per"
            after = _words_after(intent.question, self.shapes)
            grouped = [(t, f) for t, f in named if t & after]
            named = grouped or named
        entity = self.catalog.entities.get(intent.target_entity) if intent.target_entity else None
        if entity is not None and not entity.row_level and shape != "count_by":
            named = [(t, f) for t, f in named if src.fields[f].semantic_type != intent.target_entity] or named
        if named:
            best = max(len(t) for t, _ in named)
            candidates = [f for t, f in named if len(t) == best]
            if len(candidates) == 1:
                ref = f"{primary}.{candidates[0]}"
                decisions.append(DecisionRecord(kind="values_field", question=question, answer=ref, probability=0.99,
                                                subject=f"the question names '{candidates[0]}'",
                                                decided_by="deterministic"))
                return ref
        else:
            if entity is not None and not entity.row_level and shape != "count_by":
                return None  # "the different users": listing the entity is already distinct
            candidates = [f for f in fields if f != src.resolved_time_field]
        if not candidates:
            return None
        options = {f"{primary}.{f}": self.catalog.describe_field(f"{primary}.{f}") for f in candidates}
        result = self.engine.classify(
            DecisionState(query=intent.question, terms=sorted(words), facts={"source": primary, "answer": shape}),
            question, options,
        )
        record = DecisionRecord(kind="values_field", question=question, subject=primary, answer=result.choice,
                                probability=result.probability, threshold=self.thresholds.field,
                                alternatives=result.ranked()[1:4])
        decisions.append(record)
        if not record.passed:
            top = [ref for ref, _ in result.ranked()[:4]]
            exc = ClarificationNeeded(
                f"Not sure which field the question means (best guess {result.choice}, "
                f"{result.probability:.2f} < {self.thresholds.field:.2f}).", record, top,
                self.texts.values_field(intent.question, top, self.catalog,
                                        "count_by_field" if shape == "count_by" else "values_field"),
            )
            exc.decisions = list(decisions)
            raise exc
        return result.choice

    # ------------------------------------------------------------------
    # Primary source
    # ------------------------------------------------------------------

    def _usable_fields(self, source: str, semantic_type: str, refused: Set[str]) -> List[str]:
        return [f for f in self.catalog.fields_by_semantic_type(source, semantic_type) if f"{source}.{f}" not in refused]

    def _choose_primary(self, intent: SemanticIntent, activity: Optional[ActivityDef], refused: Set[str] = frozenset()) -> str:
        eligible = []
        for cand in intent.candidate_sources:
            if not self._is_allowed(cand.source):
                continue
            src = self.catalog.sources[cand.source]
            if intent.time_range and not src.resolved_time_field:
                continue
            if any(not self._usable_fields(cand.source, r.type, refused) for r in intent.resources):
                continue
            supports = bool(intent.activity) and intent.activity in src.activities
            eligible.append(((supports, cand.confidence, cand.retrieval_score), cand.source))
        if not eligible:
            self._retry_value_type(intent, refused)
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

    def _retry_value_type(self, intent: SemanticIntent, refused: Set[str]) -> None:
        """A value whose field the user refused, with no other field of its type: ask what it is instead."""
        for res in intent.resources:
            refused_here = [r for r in refused if self.catalog.field(r).semantic_type == res.type]
            if not refused_here or any(self._usable_fields(c.source, res.type, refused) for c in intent.candidate_sources):
                continue
            counts: Dict[str, int] = {}
            for cand in intent.candidate_sources:
                for f in self.catalog.sources[cand.source].fields.values():
                    if f.semantic_type and f.semantic_type not in (res.type, "event_time"):
                        counts[f.semantic_type] = counts.get(f.semantic_type, 0) + 1
            others = sorted(counts, key=lambda t: (-counts[t], t))[:6]
            if others:
                raise ClarificationNeeded(
                    f"{res.value!r} isn't in {refused_here[0]}, and no other data has a '{res.type}' field.",
                    None, others,
                    self.texts.value_type(intent.question, res.value, others, rejected_field=refused_here[0],
                                          catalog=self.catalog),
                )

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

    def _locate_enum(self, primary: str, term: str, matches: list, blocked=frozenset(), intent=None):
        for m in matches:
            if m.field.split(".")[0] == primary:
                return m, Path(primary)
        best = None
        for m in matches:
            target = m.field.split(".")[0]
            path = self.graph.find_path(primary, lambda s, t=target: s == t, self.max_hops, self._is_allowed, blocked)
            if path and (best is None or path.confidence > best[1].confidence):
                best = (m, path)
        if best is None:
            raise ClarificationNeeded(
                f"{term!r} is a value of {', '.join(m.field for m in matches)}, which can't be "
                f"connected to '{primary}' through any known relationship.",
                options=[m.field for m in matches],
                followup=self.texts.ignore_term(intent.question, term) if intent is not None else None,
            )
        return best

    def _locate_entity(self, primary: str, entity: str, activity: Optional[ActivityDef], resource_fields: Set[str],
                       refused: Set[str] = frozenset(), blocked=frozenset(), intent=None):
        found = self._find_entity_field(primary, entity, activity, resource_fields, refused, blocked)
        if found is not None:
            return found
        reachable = [
            e for e in self.catalog.entities
            if e != entity and not self.catalog.entities[e].row_level
            and self._find_entity_field(primary, e, activity, resource_fields, refused, blocked) is not None
        ]
        raise ClarificationNeeded(
            f"Couldn't find any '{entity}' field reachable from '{primary}'.",
            options=sorted(self.catalog.entity_fields(entity)),
            followup=self.texts.entity_reachable(intent.question, entity, primary, reachable, self.catalog)
            if reachable and intent is not None else None,
        )

    def _find_entity_field(self, primary: str, entity: str, activity: Optional[ActivityDef], resource_fields: Set[str],
                           refused: Set[str], blocked) -> Optional[Tuple[str, Path]]:
        actor_role = activity.actor_role if activity else None
        represents = self.catalog.entity_fields(entity)

        def field_in(source: str, exclude: Iterable[str] = ()) -> Optional[str]:
            exclude = set(exclude) | {r.split(".")[1] for r in refused if r.split(".")[0] == source}
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
            primary, lambda s: s != primary and field_in(s) is not None, self.max_hops, self._is_allowed, blocked,
        )
        if path is None:
            return None
        return f"{path.end}.{field_in(path.end)}", path

    # ------------------------------------------------------------------
    # Confirmation decisions + join assembly
    # ------------------------------------------------------------------

    def _field_check(self, intent: SemanticIntent, ref: str, purpose: str, prior: float, n: int,
                     question: Optional[str] = None, failure: Optional[str] = None, settled: bool = False,
                     followup: Optional[Clarification] = None) -> tuple:
        """
        A field confirmation for the batch. ``settled``: the catalog leaves no
        choice (the only field of that type in the source) — recorded as a
        deterministic decision at ``prior`` instead of being asked: the
        confirmation exists to catch a wrong pick among candidates, and the
        entity/value it serves was already decided.
        """
        ask = Ask(
            key=f"field:{n}", question=question or f"Is {ref} relevant for {purpose}?",
            subject=self.catalog.describe_field(ref), criteria=CRITERIA["field"],
            state=DecisionState(query=intent.question, prior=prior, facts={"field": ref}),
        )
        settled = settled and prior >= self.thresholds.field  # a weak catalog link still gets asked
        followup = followup or self.texts.field_filter(intent.question, ref, purpose, self.catalog)
        return ask, "field_relevance", ref, self.thresholds.field, \
            failure or f"Not confident that {ref} is the right field for {purpose}.", followup, settled

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
                followup = self.texts.join(intent.question, edge.left, edge.right, self.catalog)
                checks.append((ask, "relationship_relevance", f"{edge.left} = {edge.right}", self.thresholds.relationship,
                               f"Not confident enough in joining {edge.left} = {edge.right}", followup, False))
                sources.append(edge.right_source)
                joins.append(Join(left=edge.left, right=edge.right))
        return sources, joins

    def _run_checks(self, checks: List[tuple], decisions: List[DecisionRecord], pins: Dict[str, Any]) -> None:
        """Every confirmation in one batch; the first to miss its threshold (in plan order) stops the plan."""
        pin_of = {id(c): next(iter(c[5].options[0].pins)) for c in checks}  # "field:<ref>" / "join:<a>=<b>"
        answers = ask_all(self.engine, [c[0] for c in checks if not c[-1] and pin_of[id(c)] not in pins])
        for check in checks:
            ask, kind, subject, threshold, message, followup, settled = check
            key = pin_of[id(check)]
            if key in pins:
                yes = bool(pins[key])
                record = DecisionRecord(kind=kind, question=ask.question, subject=subject, answer=yes,
                                        probability=1.0 if yes else 0.0, threshold=threshold, decided_by="user")
                decisions.append(record)
                if not yes:
                    exc = ClarificationNeeded(
                        f"You said not to use {subject} — this question can't be answered without it; "
                        f"try rephrasing it.", record,
                    )
                    exc.decisions = list(decisions)
                    raise exc
                continue
            if settled:
                decisions.append(DecisionRecord(
                    kind=kind, question=ask.question, subject=subject, answer=True,
                    probability=ask.state.prior, threshold=threshold, decided_by="deterministic",
                ))
                continue
            result = answers[ask.key]
            record = DecisionRecord(
                kind=kind, question=ask.question, subject=subject,
                answer=result.answer, probability=result.probability, threshold=threshold,
            )
            decisions.append(record)
            if not record.passed:
                if kind == "relationship_relevance":
                    message = f"{message} ({result.probability:.2f})."
                exc = ClarificationNeeded(message, record, [subject], followup)
                exc.decisions = list(decisions)  # the confirmations that did pass, for the audit
                raise exc

    def _is_allowed(self, source: str) -> bool:
        return self.allowed is None or source in self.allowed


def _words_after(question: str, shapes: AnswerShapes) -> Set[str]:
    """Stems of the word right after "per" / "by" / "for each" / "por" ... — the group in a count per group."""
    out = set()
    for _, end in shapes.group_words(question):
        nxt = re.match(r"\s+(?:the |a |an |o |os |as )?([\w-]+)", question[end:], re.I)
        if nxt:
            out |= {stem(t) for t in tokenize(nxt.group(1))}
    return out


def _equal_to(ref: str, paths: List[Path]) -> Set[str]:
    """``ref`` and every field an equality join of the plan ties it to (same value on every row)."""
    same, grew = {ref}, True
    edges = [(e.left, e.right) for p in paths for e in p.edges]
    while grew:
        grew = False
        for a, b in edges:
            if (a in same) != (b in same):
                same |= {a, b}
                grew = True
    return same

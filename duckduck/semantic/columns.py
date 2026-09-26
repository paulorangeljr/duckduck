"""
More columns for an answer: a step back after the question.

A question selects what it asks for — "which critical CVEs published in the
last year are in KEV?" lists the CVE ids — but whoever reads the answer
often wants to see more of the same rows: the severity, when it was
published, the score. ``column_options`` lists the other fields of the
tables the answer read (the ones it filtered on, and its time field,
suggested first), and ``with_columns`` runs **the same plan** again with
those fields added to its ``select``: no decision is taken again (no engine
or LLM call), the filters, joins and time range stay as they were, and the
SQL still comes only from the compiler over a validated plan.

Only for answers that are rows (``list``, ``values``): a count has nothing
to add a column to, and ``browse``/``lookup`` already show every column.
Adding a column to a ``DISTINCT`` answer gives one row per distinct
combination — the same things, with what the user asked to see next to
them.
"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from .. import progress
from .intent import DecisionRecord


def can_add_columns(result: Any) -> bool:
    plan = getattr(result, "query_plan", None)
    shape = result.intent.answer_shape if getattr(result, "intent", None) is not None else "list"
    return (plan is not None and plan.aggregate is None and shape not in ("browse", "lookup", "locate")
            and result.status == "ok")


def _base_select(result: Any) -> List[str]:
    added = set(getattr(result, "added_columns", []) or [])
    return [ref for ref in result.query_plan.select if ref not in added]


def column_options(catalog: Any, result: Any) -> List[Dict[str, Any]]:
    """
    Every field of the answer's tables, one entry each: ``ref``, ``source``,
    ``field``, ``description``, ``type``, ``selected`` (already shown),
    ``asked`` (part of what the question asked for — can't be removed),
    ``kept`` (in the rows already read: adding it reads nothing again),
    ``suggested`` with ``why`` (filtered on, the time field). Suggested
    first, then in the catalog's order. Empty when columns can't be added.
    """
    if not can_add_columns(result):
        return []
    plan = result.query_plan
    base, shown = set(_base_select(result)), set(plan.select)
    why: Dict[str, str] = {}
    for f in plan.filters:
        why.setdefault(f.field, "filtered on")
    if plan.time_range is not None:
        why.setdefault(plan.time_range.field, "time filter")
    for source in plan.sources:
        tf = catalog.sources[source].time_field
        if tf:
            why.setdefault(f"{source}.{tf}", "when it happened")
    data = getattr(result, "data", None)

    def kept(source: str, name: str) -> bool:
        if data is None or data.released:
            return False
        cols = data.columns.get(source, set())
        return cols is None or catalog.sources[source].physical_column(name) in cols

    options: List[Dict[str, Any]] = []
    for source in plan.sources:
        for name, fdef in catalog.sources[source].fields.items():
            ref = f"{source}.{name}"
            options.append({
                "ref": ref, "source": source, "field": name, "description": fdef.description or "",
                "type": fdef.type, "selected": ref in shown, "asked": ref in base,
                "suggested": ref in why and ref not in base, "why": why.get(ref, ""),
                "kept": kept(source, name),
            })
    options.sort(key=lambda o: (not o["asked"], not o["suggested"]))
    return options


def with_columns(search: Any, result: Any, columns: Iterable[str], now: Optional[datetime] = None) -> Any:
    """
    ``result`` again, its plan's ``select`` = what the question asked for +
    ``columns`` (the complete set of extra fields — pass ``[]`` to go back).
    Raises ``ValueError`` for an answer that can't take columns or a field
    that isn't one of its tables'.
    """
    import copy
    import time

    if not can_add_columns(result):
        raise ValueError("this answer has no rows to add columns to (a count, or it already shows every column)")
    plan = result.query_plan
    base = _base_select(result)
    extra: List[str] = []
    for ref in columns:
        ref = str(ref)
        source, _, name = ref.partition(".")
        if source not in plan.sources or name not in search.catalog.sources[source].fields:
            raise ValueError(f"{ref!r} isn't a field of the tables this answer read ({', '.join(plan.sources)})")
        if ref not in base and ref not in extra:
            extra.append(ref)
    started = time.perf_counter()
    new_plan = search.validator.validate(plan.model_copy(update={"select": base + extra}))
    widened = copy.copy(result)
    widened.added_columns = extra
    progress.step("columns", "Adding " + (", ".join(extra) or "no columns — back to the original ones"))
    cap = result.plan_limit or new_plan.limit  # the plan's own row cap; the sample stays the sample
    search._answer_rows(widened, new_plan.model_copy(update={"limit": cap}), now or search.clock(), result.sample,
                        data=result.data, again=True)
    widened.decisions = [d for d in result.decisions if d.kind != "columns"]
    if extra:
        widened.decisions.append(DecisionRecord(
            kind="columns", question="Which other columns should the answer show?", subject=", ".join(base),
            answer=extra, probability=1.0, decided_by="user"))
    widened.elapsed_ms = (time.perf_counter() - started) * 1000
    return widened

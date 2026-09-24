"""
Query Validator — semantic checks on a ``LogicalQueryPlan`` before it's
compiled, on top of the structural checks Pydantic already enforces.

Checks: sources exist and are authorized; every field exists and belongs
to its source; operators fit the field type; values fit the field type;
joins follow a known joinable relationship and connect every source; the
time field is a datetime. A plan from anywhere — the planner, an API
caller, a fallback LLM — goes through the same gate.

Injection: identifiers can only be catalog names (checked here, and
pattern-constrained by the models); values are always emitted as quoted
literals by the compiler, never spliced.
"""

from typing import Iterable, List, Optional, Set

from .catalog import JOINABLE_RELATIONSHIPS, Catalog
from .plan import LogicalQueryPlan

_TEXT_OPERATORS = {"contains", "starts_with", "ends_with"}
_ORDER_OPERATORS = {"gt", "gte", "lt", "lte"}
_ORDERED_TYPES = {"integer", "float", "datetime", "date"}
_VALUE_TYPES = {
    "string": (str,),
    "integer": (int,),
    "float": (int, float),
    "boolean": (bool,),
    "datetime": (str,),
    "date": (str,),
}


class PlanValidationError(ValueError):
    def __init__(self, issues: List[str]):
        super().__init__("invalid query plan:\n  - " + "\n  - ".join(issues))
        self.issues = issues


class QueryValidator:
    def __init__(self, catalog: Catalog, allowed_sources: Optional[Iterable[str]] = None):
        self.catalog = catalog
        self.allowed: Optional[Set[str]] = set(allowed_sources) if allowed_sources is not None else None
        self._joinable = set()
        for rel in catalog.relationships:
            if rel.type in JOINABLE_RELATIONSHIPS:
                self._joinable.add((rel.from_, rel.to))
                self._joinable.add((rel.to, rel.from_))

    def issues(self, plan: LogicalQueryPlan) -> List[str]:
        issues: List[str] = []
        known_sources = [s for s in plan.sources if s in self.catalog.sources]
        for source in plan.sources:
            if source not in self.catalog.sources:
                issues.append(f"unknown source '{source}'")
            elif self.allowed is not None and source not in self.allowed:
                issues.append(f"source '{source}' is not authorized")

        def check_field(ref: str, role: str) -> bool:
            if ref.split(".")[0] not in known_sources:
                return False  # already reported as an unknown source
            if not self.catalog.has_field(ref):
                issues.append(f"{role}: unknown field '{ref}'")
                return False
            return True

        for ref in plan.select:
            check_field(ref, "select")

        for flt in plan.filters:
            if not check_field(flt.field, "filter"):
                continue
            ftype = self.catalog.field(flt.field).type
            if flt.operator in _TEXT_OPERATORS and ftype != "string":
                issues.append(f"filter: '{flt.operator}' needs a string field, '{flt.field}' is {ftype}")
            if flt.operator in _ORDER_OPERATORS and ftype not in _ORDERED_TYPES:
                issues.append(f"filter: '{flt.operator}' needs an ordered field, '{flt.field}' is {ftype}")
            values = flt.value if isinstance(flt.value, list) else [flt.value]
            allowed_types = _VALUE_TYPES[ftype]
            for v in values:
                # bool is an int subclass — don't let True pass as an integer
                bad_bool = isinstance(v, bool) and ftype != "boolean"
                if bad_bool or not isinstance(v, allowed_types):
                    issues.append(f"filter: value {v!r} doesn't fit {ftype} field '{flt.field}'")

        if plan.time_range and check_field(plan.time_range.field, "time_range"):
            ftype = self.catalog.field(plan.time_range.field).type
            if ftype not in ("datetime", "date"):
                issues.append(f"time_range: '{plan.time_range.field}' is {ftype}, not datetime/date")

        connected = {plan.sources[0]}
        for join in plan.joins:
            ok = check_field(join.left, "join") & check_field(join.right, "join")
            if ok and (join.left, join.right) not in self._joinable:
                issues.append(f"join: no joinable relationship between '{join.left}' and '{join.right}'")
            left_src, right_src = join.left.split(".")[0], join.right.split(".")[0]
            if left_src == right_src:
                issues.append(f"join: '{join.left}' = '{join.right}' joins a source to itself")
            if left_src in connected:
                connected.add(right_src)
            elif right_src in connected:
                connected.add(left_src)
            else:
                issues.append(
                    f"join: '{join.left}' = '{join.right}' doesn't connect to any source joined before it"
                )
        missing = [s for s in plan.sources if s not in connected]
        if missing:
            issues.append(f"sources not connected by any join: {', '.join(missing)}")
        return issues

    def validate(self, plan: LogicalQueryPlan) -> LogicalQueryPlan:
        issues = self.issues(plan)
        if issues:
            raise PlanValidationError(issues)
        return plan

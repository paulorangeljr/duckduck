"""
Logical Query Plan — the contract between interpretation and execution.

Nothing upstream ever writes SQL: the planner produces this structure,
the validator checks it against the catalog, and only the compiler turns
it into SQL. Pydantic enforces the *shape* here (field references are
``source.field`` identifiers, operators come from a closed set, limit is
bounded); ``validator.py`` enforces the *meaning* (fields exist, types
fit the operators, joins follow known relationships, access is allowed).
"""

from datetime import datetime
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

from .catalog import Identifier

FieldRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")]

Operator = Literal["eq", "neq", "contains", "starts_with", "ends_with", "gt", "gte", "lt", "lte", "in"]

Scalar = Union[bool, int, float, str]

#: Hard ceiling on ``limit`` — a plan can never ask for more rows than this.
MAX_LIMIT = 100_000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filter(_Strict):
    field: FieldRef
    operator: Operator
    value: Union[Scalar, List[Scalar]]

    @model_validator(mode="after")
    def _check_value(self) -> "Filter":
        if (self.operator == "in") != isinstance(self.value, list):
            raise ValueError("'in' takes a list value; every other operator takes a scalar")
        if isinstance(self.value, list) and not self.value:
            raise ValueError("'in' needs at least one value")
        return self


class Join(_Strict):
    left: FieldRef
    right: FieldRef
    type: Literal["inner", "left"] = "inner"


class TimeRangeFilter(_Strict):
    field: FieldRef
    last_hours: Optional[float] = Field(default=None, gt=0)
    start: Optional[datetime] = None
    end: Optional[datetime] = None

    @model_validator(mode="after")
    def _check_bounds(self) -> "TimeRangeFilter":
        if self.last_hours is None and self.start is None and self.end is None:
            raise ValueError("a time range needs last_hours, start or end")
        if self.last_hours is not None and (self.start or self.end):
            raise ValueError("use either last_hours or start/end, not both")
        if self.start and self.end and self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class LogicalQueryPlan(_Strict):
    select: List[FieldRef] = Field(min_length=1)
    sources: List[Identifier] = Field(min_length=1)
    joins: List[Join] = Field(default_factory=list)
    filters: List[Filter] = Field(default_factory=list)
    time_range: Optional[TimeRangeFilter] = None
    distinct: bool = False
    limit: int = Field(default=1000, ge=1, le=MAX_LIMIT)

    @model_validator(mode="after")
    def _check_sources(self) -> "LogicalQueryPlan":
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("each source can appear only once in 'sources'")
        declared = set(self.sources)
        refs = list(self.select) + [f.field for f in self.filters]
        refs += [j.left for j in self.joins] + [j.right for j in self.joins]
        if self.time_range:
            refs.append(self.time_range.field)
        for ref in refs:
            if ref.split(".")[0] not in declared:
                raise ValueError(f"'{ref}' references a source not listed in 'sources'")
        return self

    def source_fields(self, source: str) -> List[str]:
        """Every field of ``source`` the plan touches (select/filter/join/time), in first-use order."""
        refs = list(self.select) + [f.field for f in self.filters]
        refs += [ref for j in self.joins for ref in (j.left, j.right)]
        if self.time_range:
            refs.append(self.time_range.field)
        seen: List[str] = []
        for ref in refs:
            src, fld = ref.split(".")
            if src == source and fld not in seen:
                seen.append(fld)
        return seen

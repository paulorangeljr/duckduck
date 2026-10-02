"""
The Spark contract a connector declares on each table method — kept apart from
``duckduck.spark`` so connectors import it without pyspark or the engine.
See ``spark_plan`` and CLAUDE.md "Spark: choosing the strategy".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

STRATEGIES = ("native", "partitioned", "driver")
SPARK_ATTR = "__duckduck_spark__"


@dataclass(frozen=True)
class SparkPlan:
    """How Spark reads a table. See ``spark_plan``."""

    strategy: str
    #: native: the name of the connector method that says where the data is: ``(**args) -> SparkSource``
    source: Optional[str] = None
    #: partitioned: ``"pages"`` (the pager is ``duckduck.common.slicing.pages``) or the name of a connector
    #: method ``(**kwargs) -> [kwargs, ...]`` splitting the call into independent pieces
    by: Optional[str] = None
    #: partitioned: how many pieces may run at once — the API's rate limit decides it
    max_parallel: int = 4
    #: one line on why this strategy: what about the source decided it
    why: str = ""


@dataclass
class SparkSource:
    """Where a native table's data is, for Spark: a format and a path, a catalog table, or JDBC options."""

    format: str
    path: Optional[str] = None
    #: a name the Spark session's catalog may know (a Glue/Hive table): read by name when it does
    table: Optional[str] = None
    options: Dict[str, str] = field(default_factory=dict)


def spark_plan(strategy: str, *, source: Optional[str] = None, by: Optional[str] = None, max_parallel: int = 4,
               why: str = "") -> Callable:
    """
    Declares how Spark reads the table a connector method serves. Every
    table method of a bundled connector carries one (``tests/test_spark.py``
    checks), so the choice is made — and reviewed — when the connector is
    written. See CLAUDE.md "Spark: choosing the strategy".

        @spark_plan("native", source="_spark_table", why="Parquet/Delta/Iceberg on S3")
        def table(self, database, table_name, where=None, limit=None): ...

        @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with totalResources")
        def assets(self, ..., limit=None): ...

        @spark_plan("driver", why="@odata.nextLink cursor: pages can't be read out of order")
        def list_items(self, ...): ...
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"spark_plan: strategy must be one of {', '.join(STRATEGIES)} (got {strategy!r})")
    if strategy == "native" and not source:
        raise ValueError("spark_plan('native') needs source=<connector method returning a SparkSource>")
    if strategy == "partitioned" and not by:
        raise ValueError("spark_plan('partitioned') needs by='pages' or by=<connector method returning kwargs>")
    if max_parallel < 1:
        raise ValueError("spark_plan: max_parallel must be ≥ 1")
    plan = SparkPlan(strategy, source, by, int(max_parallel), why)

    def mark(fn: Callable) -> Callable:
        setattr(fn, SPARK_ATTR, plan)
        return fn

    return mark


def plan_of(fn: Any) -> Optional[SparkPlan]:
    """The ``SparkPlan`` declared on a table function (a bound method, or a callable object's ``__call__``)."""
    plan = getattr(fn, SPARK_ATTR, None)
    if plan is None and not isinstance(fn, type) and hasattr(type(fn), "__call__"):
        plan = getattr(type(fn).__call__, SPARK_ATTR, None)
    return plan


def owner_of(fn: Any) -> Any:
    """The connector a table function belongs to: a bound method's instance (a saved table's bound function
    carries its connector as ``__self__`` too), or a callable object itself (``FileTable``)."""
    import inspect

    if inspect.isfunction(fn):  # a plain function, or a saved table's wrapper (it sets __self__)
        return getattr(fn, "__self__", None)
    return getattr(fn, "__self__", fn)  # a bound method → its instance; a callable object → itself


def requests_at_once(fn: Any, plan: Optional[SparkPlan] = None) -> int:
    """How many requests of this table may run at once: the connector's own ``max_parallel`` (a service's
    ``"max_parallel"`` in duckduck.json, or a pipeline's for its run) when set, else the plan's declared one."""
    plan = plan or plan_of(fn)
    own = getattr(owner_of(fn), "max_parallel", None)
    if isinstance(own, int) and not isinstance(own, bool) and own >= 1:
        return own
    return plan.max_parallel if plan else 1


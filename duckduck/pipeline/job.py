"""
A pipeline whose transformation is PySpark — everything else is the framework's.

The engineer writes one class: what it reads (``inputs``), where it goes (``target``, ``primary_key``, ``load``…)
and ``transform()`` — the PySpark that turns the inputs into the table. Reading, the lake's names, the incremental
load, the audit columns, the target (catalog, schema evolution, merge, partitions), the state, resuming a failed
run and the sip are the same code a SQL pipeline runs (``runner.run_pipeline``): the job becomes a pipeline whose
inputs are plain steps (``CREATE VIEW assets AS SELECT * FROM silver.inventory.assets``) and whose last step is
the DataFrame ``transform`` returns (``JobEngine``).

    from duckduck.pipeline import SparkJob

    class RiskByDepartment(SparkJob):
        pipeline = "gold_risk_by_department"
        inputs = {"assets": "silver.inventory.assets", "owners": "silver.inventory.owners"}
        target = {"layer": "gold", "database": "inventory", "table_name": "risk_by_department", "mode": "overwrite"}
        primary_key = ["department", "os"]

        def transform(self, assets, owners):
            return (assets.join(owners, "ip").groupBy("department", "os")
                    .agg(F.count("*").alias("hosts"), F.round(F.avg("risk"), 1).alias("avg_risk")))

    RiskByDepartment().run()          # or: python -m duckduck.pipeline run jobs/gold_risk.py

``run``, ``plan``, ``spec``, ``debug`` and the reading/writing behind them can't be overridden (``FINAL``): a class
that redefines one fails when it's defined. Any other method is the engineer's (helpers ``transform`` calls).
``check(df)`` is an optional hook for the engineer's own data rules.

``transform`` may be a plain function written outside the class — easier to test and to step through::

    def risk_by_department(assets, owners, params):      # plain DataFrames in, a DataFrame out
        ...

    class RiskByDepartment(SparkJob):
        ...
        transform = risk_by_department

It takes the inputs by name, plus ``params`` (the run's parameters) and ``spark`` (the session) when it declares
them. ``job.debug()`` runs it on what a run would read and writes nothing; ``job.debug(inputs={"assets": df, …})``
on DataFrames of your own.
"""

from __future__ import annotations

import abc
import importlib.util
import inspect
import os
import re
from typing import Any, Dict, List, Optional

from .spec import NAME_RE, PipelineError, load_spec
from .engines import SparkEngine

#: the framework's part: a subclass that defines one of these fails at once, naming ``transform``
FINAL = ("run", "plan", "spec", "debug", "settings_dict", "_engine", "_frames", "_finish", "_call", "_key",
         "_pipeline_run", "_inputs")
#: what ``transform`` may also declare (never an input's name): the run's parameters, the Spark session
EXTRAS = ("params", "spark")
#: pipeline-file keys a job gives as attributes (the rest through ``settings``)
ATTRIBUTES = ("description", "primary_key", "target", "load", "parameters")
#: keys the job writes itself — ``settings`` can't set them
OWN_KEYS = {"pipeline", "engine", "sql", "notebook", "sql_file", "targets", "output", "keys"}
_QUERY = re.compile(r"^\s*(SELECT|WITH|FROM)\b", re.I)
_TABLE = re.compile(r"^[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*)*$")


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


class SparkJob(abc.ABC):
    """A pipeline with a PySpark ``transform`` — see the module docstring. Subclass it; implement ``transform``."""

    # ---- what the engineer declares ---------------------------------------------------------------------------
    #: the pipeline's name — state, sip and run reports go by it (default: the class name in snake_case)
    pipeline: Optional[str] = None
    description: Optional[str] = None
    #: tables — ``["silver.cmdb.assets_cmdb_ci"]``: each a DataFrame argument of ``transform`` named like its table
    #: (``assets_cmdb_ci``) — or ``{argument: table}`` to name them yourself. A table is a lake table
    #: (``silver.inventory.assets`` / ``inventory.assets``), a connector's (``servicenow.incident``), or a query
    #: (``SELECT … FROM silver.x.y WHERE …``) — its filters reach the source as in any pipeline
    inputs: Any = {}
    #: as in a pipeline file: ``{"layer", "database", "table_name", "mode", "format", "partition_by", …}`` or a path
    target: Any = None
    primary_key: Any = None
    #: ``"full"`` / ``"incremental"`` / ``{type, columns, step: <input name>, …}`` — filters the input it names
    load: Any = None
    #: ``{{ name }}`` values for the inputs' queries; ``self.params`` in ``transform`` (built-ins included)
    parameters: Dict[str, Any] = {}
    #: any other pipeline-file key: ``layer``, ``sip``, ``state``, ``schema_evolution``, ``timezone``, ``catalog``…
    settings: Dict[str, Any] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        taken = [name for name in FINAL if name in cls.__dict__]
        if taken:
            raise TypeError(f"{cls.__name__} redefines {', '.join(taken)} — reading, saving and the run are the "
                            f"framework's; put your code in transform() (and data rules in check())")
        fn = cls.__dict__.get("transform")
        if inspect.isfunction(fn) and _outside(fn, cls):  # transform = a_function: called without self
            cls.transform = staticmethod(fn)

    # ---- what the engineer writes -----------------------------------------------------------------------------
    @abc.abstractmethod
    def transform(self, **inputs: Any) -> Any:
        """The inputs (Spark DataFrames, by their ``inputs`` name) → the table to write (a Spark DataFrame).
        ``self.spark`` is the session, ``self.params`` the run's parameters (``run_date``, ``run_id``…) — or declare
        ``params`` / ``spark`` as arguments. May be a plain function: ``transform = my_function``."""

    def check(self, df: Any) -> Optional[List[str]]:
        """Optional: the engineer's data rules on the result, before anything is written. Return the problems
        (or raise); any problem stops the run and nothing is written."""
        return None

    # ---- the framework's part (FINAL) -------------------------------------------------------------------------
    spark: Any = None
    params: Dict[str, Any] = {}

    def settings_dict(self) -> Dict[str, Any]:
        """The pipeline file this job is — its inputs as steps, its result as the last one."""
        name = self.pipeline or _snake(type(self).__name__)
        inputs = self._inputs(name)
        steps = []
        for alias, source in inputs.items():
            if not isinstance(alias, str) or not NAME_RE.match(alias) or alias == name or alias in EXTRAS:
                raise PipelineError(f"{name}: input name {alias!r} must be a plain name (letters, digits, _), "
                                    f"not the pipeline's own name nor {' / '.join(EXTRAS)}")
            if not isinstance(source, str) or not source.strip():
                raise PipelineError(f"{name}: input {alias} is a table (silver.db.table) or a query")
            query = source.strip() if _QUERY.match(source) else (
                f"SELECT * FROM {source.strip()}" if _TABLE.match(source.strip()) else None)
            if query is None:
                raise PipelineError(f"{name}: input {alias}: {source!r} is neither a table name nor a query")
            steps.append(f"CREATE VIEW {alias} AS {query}")
        self._check_signature(name)
        # the last step stands for transform(): the engine runs the PySpark, never this SQL (it only names the
        # inputs, so they're read first and the incremental load filters the right one)
        steps.append(f"CREATE VIEW {name} AS SELECT * FROM {', '.join(inputs)}")
        data: Dict[str, Any] = {"pipeline": name, "engine": "spark", "sql": steps}
        for key in ATTRIBUTES:
            value = getattr(self, key)
            if value not in (None, {}, []):
                data[key] = value
        clash = sorted(OWN_KEYS & set(self.settings or {}))
        if clash:
            raise PipelineError(f"{name}: settings can't set {clash} — the job writes them from its class")
        data.update(self.settings or {})
        if self.primary_key:  # the result's key is its own (no lineage through Python): the sip follows it here
            data["keys"] = {name: self.primary_key}
        return data

    def _inputs(self, name: Optional[str] = None) -> Dict[str, str]:
        """``inputs`` as ``{argument: table}``: a list of tables gives each the name of its table (its last part:
        ``silver.cmdb.assets_cmdb_ci`` → ``assets_cmdb_ci``); a dict names them itself."""
        name = name or self.pipeline or _snake(type(self).__name__)
        inputs = self.inputs
        if isinstance(inputs, str):
            inputs = [inputs]
        if isinstance(inputs, (list, tuple)):
            named: Dict[str, str] = {}
            for table in inputs:
                if not isinstance(table, str) or not _TABLE.match(table.strip()):
                    raise PipelineError(f"{name}: a list of inputs holds table names ({table!r} isn't one) — "
                                        f"give a query a name of its own: {{\"name\": \"SELECT …\"}}")
                leaf = table.strip().split(".")[-1]
                if leaf in named:
                    raise PipelineError(f"{name}: two inputs are tables named {leaf} ({named[leaf]}, {table}) — "
                                        f"name them: {{\"{leaf}\": \"{named[leaf]}\", \"other\": \"{table}\"}}")
                named[leaf] = table.strip()
            inputs = named
        if not isinstance(inputs, dict) or not inputs:
            raise PipelineError(f"{name}: 'inputs' names what transform() reads — tables "
                                f"([\"silver.cmdb.assets_cmdb_ci\"]: the argument is assets_cmdb_ci) or "
                                f"{{argument: table or query}}")
        return dict(inputs)

    def _check_signature(self, name: str) -> None:
        params = inspect.signature(self.transform).parameters
        if any(p.kind is p.VAR_KEYWORD for p in params.values()):
            return
        taken = [p for p in params if params[p].kind in (params[p].POSITIONAL_OR_KEYWORD, params[p].KEYWORD_ONLY)
                 and p not in EXTRAS]
        inputs = self._inputs(name)
        missing = [a for a in inputs if a not in taken]
        extra = [p for p in taken if p not in inputs and params[p].default is inspect.Parameter.empty]
        if missing or extra:
            raise PipelineError(f"{name}: transform({', '.join(taken)}) doesn't match inputs "
                                f"({', '.join(inputs)}) — each input arrives as the argument of its name")

    def spec(self, base_dir: Optional[str] = None) -> Any:
        """The ``PipelineSpec`` — relative paths (state, a target path) from the folder of the job's file."""
        return load_spec(self.settings_dict(), base_dir=base_dir or self._folder())

    def _folder(self) -> str:
        try:
            return os.path.dirname(os.path.abspath(inspect.getfile(type(self))))
        except (TypeError, OSError):
            return os.getcwd()

    def _engine(self, spark: Any, duck: Any) -> "JobEngine":
        if spark is None:
            try:
                from pyspark.sql import SparkSession
            except ImportError:
                raise PipelineError("a SparkJob needs pyspark (pip install \"duckduck[spark]\")") from None
            spark = SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
        return JobEngine(spark, duck, self)

    def run(self, spark: Any = None, duck: Any = None, config_path: Optional[str] = None,
            params: Optional[Dict[str, Any]] = None, run_id: Optional[str] = None, dry_run: bool = False,
            now: Any = None, catalogs: Optional[Dict[str, Any]] = None, resume: Optional[bool] = None) -> Any:
        """Runs it like any pipeline (``run_pipeline``) → ``PipelineRun``. ``dry_run``: transform runs, nothing
        is written."""
        return self._pipeline_run(spark, duck, config_path, params=params, run_id=run_id, dry_run=dry_run, now=now,
                                  catalogs=catalogs, resume=resume)[0]

    def _pipeline_run(self, spark: Any, duck: Any, config_path: Optional[str], **kwargs: Any):
        from .runner import _duck, run_pipeline

        spec = self.spec()
        duck = _duck(duck, config_path)
        engine = self._engine(spark, duck)
        return run_pipeline(spec, duck=duck, spark=spark, config_path=config_path, engine=engine, **kwargs), engine

    def debug(self, inputs: Optional[Dict[str, Any]] = None, spark: Any = None, params: Optional[Dict[str, Any]] = None,
              config_path: Optional[str] = None, duck: Any = None, now: Any = None) -> Any:
        """``transform`` + the checks a run makes (key columns, ``check()``) → the Spark DataFrame; nothing written,
        no state changed. ``inputs`` (``{name: DataFrame}``, every input): run on those — no lake, no connector.
        Without: reads what the next run would (the incremental load included) — a dry run."""
        if inputs is None:
            run, engine = self._pipeline_run(spark, duck, config_path, params=params, dry_run=True, now=now)
            return engine.result
        name = self.settings_dict()["pipeline"]
        declared = self._inputs(name)
        missing = [a for a in declared if a not in inputs]
        extra = [a for a in inputs if a not in declared]
        if missing or extra:
            raise PipelineError(f"{name}: debug(inputs=) takes a DataFrame per input ({', '.join(declared)})"
                                + (f" — missing {missing}" if missing else "") + (f" — no input {extra}" if extra else ""))
        from .spec import run_parameters

        if spark is None:
            spark = next((getattr(df, "sparkSession", None) for df in inputs.values()), None)
        self.spark, self.params = spark, run_parameters(self.spec(), params, now=now)
        return self._finish(self._call(dict(inputs)), name, self._key())

    def _key(self) -> List[str]:
        key = self.primary_key
        return key if isinstance(key, list) else ([key] if key else [])

    def _call(self, frames: Dict[str, Any]) -> Any:
        """``transform(**frames)`` + ``params`` / ``spark`` when it declares them."""
        accepted = inspect.signature(self.transform).parameters
        for extra, value in (("params", self.params), ("spark", self.spark)):
            if extra in accepted:
                frames[extra] = value
        return self.transform(**frames)

    def plan(self, duck: Any = None, config_path: Optional[str] = None, params: Optional[Dict[str, Any]] = None):
        """What a run would read and write (``plan_pipeline``) — nothing runs."""
        from .runner import plan_pipeline

        plan = plan_pipeline(self.spec(), duck=duck, config_path=config_path, params=params)
        final = self.settings_dict()["pipeline"]
        for name, view in plan.views.items():  # said as the engineer wrote it, not as the SQL it stands in for
            view.origin = "transform() — PySpark" if name == final else f"input {name}"
        report = plan.report

        def job_report() -> str:
            head, _, rest = report().partition("\n")
            return f"pipeline {final} — spark, a SparkJob ({type(self).__name__})\n{rest}"

        plan.report = job_report
        return plan

    def _frames(self, spark: Any) -> Dict[str, Any]:
        return {alias: spark.table(alias) for alias in self._inputs()}

    def _finish(self, df: Any, name: str, key: List[str]) -> Any:
        """The result checked before it's written: a Spark DataFrame, unique column names, the key's columns,
        and the engineer's own ``check``."""
        if not (hasattr(df, "createOrReplaceTempView") and hasattr(df, "columns")):
            raise PipelineError(f"{name}: transform() returns the table as a Spark DataFrame, not "
                                f"{type(df).__name__} — the framework writes it (don't call df.write yourself)")
        cols = [c.lower() for c in df.columns]
        dup = sorted({c for c in cols if cols.count(c) > 1})
        if dup:
            raise PipelineError(f"{name}: the result has the column(s) {dup} more than once (a join keeping both "
                                f"sides?) — select or rename them")
        missing = [k for k in key if k.lower() not in cols]
        if missing:
            raise PipelineError(f"{name}: the result has no {missing} — the primary_key it's written by")
        problems = self.check(df)
        if problems:
            raise PipelineError(f"{name}: check() found {len(problems)} problem(s), nothing written — "
                                + "; ".join(str(p) for p in problems))
        return df


class JobEngine(SparkEngine):
    """The Spark engine, except the job's last step: its view is the DataFrame ``transform`` returns."""

    name = "spark"

    def __init__(self, spark: Any, duck: Any, job: SparkJob):
        super().__init__(spark, duck)
        self.job = job
        self.parameters: Dict[str, Any] = {}  # the run's values, set by the runner before the steps run
        self.final = job.settings_dict()["pipeline"]
        self.result: Any = None  # transform()'s DataFrame once it ran (SparkJob.debug returns it)

    def define(self, name: str, sql: str, keep: bool) -> None:
        if name != self.final:
            return super().define(name, sql, keep)
        job = self.job
        job.spark, job.params = self.spark, dict(self.parameters)
        df = job._finish(job._call(job._frames(self.spark)), name, job._key())
        self.result = df
        old = self._kept.pop(name, None)
        if old is not None:
            old.unpersist()
        if keep:
            df = df.persist()
            self._kept[name] = df
        df.createOrReplaceTempView(name)
        if name not in self._views:
            self._views.append(name)


def _outside(fn: Any, cls: type) -> bool:
    """A function written outside the class body (``transform = risk_by_department``) that doesn't take ``self``."""
    if fn.__qualname__.startswith(cls.__qualname__ + "."):
        return False
    first = next(iter(inspect.signature(fn).parameters), None)
    return first != "self"


def jobs_in(path: str) -> List[type]:
    """The ``SparkJob`` classes a ``.py`` file defines (for ``python -m duckduck.pipeline run file.py``)."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise PipelineError(f"no job file at {path}")
    spec = importlib.util.spec_from_file_location(f"duckduck_job_{abs(hash(path))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = [obj for obj in vars(module).values() if inspect.isclass(obj) and issubclass(obj, SparkJob)
             and obj is not SparkJob and obj.__module__ == module.__name__ and not inspect.isabstract(obj)]
    if not found:
        raise PipelineError(f"{path} defines no SparkJob subclass with a transform()")
    return found


__all__ = ["EXTRAS", "FINAL", "JobEngine", "SparkJob", "jobs_in"]

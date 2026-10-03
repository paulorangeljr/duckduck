"""
Apache Airflow on Amazon MWAA, through ``InvokeRestApi`` — the Airflow REST
API called via AWS (``mwaa.invoke_rest_api``), so a webserver in a private
VPC is reachable with IAM permissions alone (``airflow:InvokeRestApi``): no
VPN, peering or running inside the VPC.

Tables (one per REST collection; ``~`` = every DAG / run):

- ``dags``            GET /dags                                   (only active DAGs, as the API lists them)
- ``dag_runs``        GET /dags/{dag_id|~}/dagRuns
- ``task_instances``  GET /dags/{dag_id|~}/dagRuns/{dag_run_id|~}/taskInstances
- ``import_errors``   GET /importErrors

Pagination is ``limit`` / ``offset`` with ``total_entries`` in every
response (Airflow 3's cursor mode isn't used: its total is capped), through
``duckduck.common.slicing.pages`` — so Spark reads it in parallel. Dates come back
as timezone-aware timestamps; a date pushed down without an offset is read
as UTC.

**Airflow 2 or 3.** The query parameters differ (Airflow 2's
``execution_date_*`` is Airflow 3's ``logical_date_*``; ``dag_id_pattern``,
``task_id`` …). ``airflow_version`` says which; unset, the environment's
``AirflowVersion`` is read once (``mwaa:GetEnvironment``), else 2. Paths are
sent as Airflow's API reference writes them (``/dags/…``), the form
botocore's own model shows for ``Path`` — written from botocore's MWAA model
and Airflow's published OpenAPI specs (the AWS docs host was unreachable
while building this; the tests replay those shapes).

``connections`` and ``variables`` are deliberately not tables: they hold
secrets.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd

from ...common import slicing
from ...common.logs import PageProgress, get_logger
from ...common.pushdown import require_like, sortable
from ...common.sparkplan import spark_plan
from ...common.idempotency import checkpoint

logger = get_logger("airflow")

PAGE_SIZE = 100  # Airflow's default maximum_page_limit
#: dag_runs columns the API sorts by (``order_by``), as Airflow names the field — dates only: they sort the
#: same in Airflow's database as here (text would follow its collation). The logical date is
#: ``execution_date`` on Airflow 2, ``logical_date`` on 3 — either column name is accepted.
_RUN_SORTS = {"logical_date": "logical_date", "execution_date": "logical_date", "start_date": "start_date",
              "end_date": "end_date"}

def _order_param(order_by: Optional[List[Tuple[str, bool]]]) -> Dict[str, str]:
    """DuckAPI's ``order_by`` as the API's ``order_by`` (one field, ``-`` for descending)."""
    if not order_by:
        return {}
    column, descending = order_by[0]
    return {"order_by": ("-" if descending else "") + column.lower()}


_DATES = {
    "dags": ("last_parsed_time", "last_expired", "next_dagrun", "next_dagrun_create_after",
             "next_dagrun_data_interval_start", "next_dagrun_data_interval_end", "next_dagrun_logical_date",
             "next_dagrun_run_after"),
    "dag_runs": ("logical_date", "execution_date", "start_date", "end_date", "data_interval_start",
                 "data_interval_end", "last_scheduling_decision", "queued_at", "run_after"),
    "task_instances": ("logical_date", "execution_date", "start_date", "end_date", "queued_when",
                       "scheduled_when", "run_after"),
    "import_errors": ("timestamp",),
}
_WHY = ("limit/offset with total_entries; every call goes through MWAA's InvokeRestApi to one webserver: "
        "2 at a time")


class AirflowError(RuntimeError):
    """An Airflow REST API error, with its HTTP status and what Airflow said."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _moment(value: Any) -> str:
    """A pushed-down date as ISO 8601 with an offset (a naive one is UTC)."""
    if isinstance(value, dt.datetime):
        moment = value
    else:
        text = str(value).strip().replace(" ", "T", 1)
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            moment = dt.datetime.combine(dt.date.fromisoformat(text[:10]), dt.time())
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.isoformat()


def _dates(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """A table method's date arguments (``logical_date``, ``start_date_gte``…), the ones given."""
    return {k: v for k, v in arguments.items() if v is not None and k.split("_date")[0] in
            ("logical", "execution", "start", "end") and "_date" in k}


def _range(dates: Dict[str, Any], *names: str) -> Tuple[Any, Any, Any, Any]:
    """(gte, gt, lte, lt) of a date under any of its names; ``= v`` is ``>= v`` and ``<= v``."""
    def first(suffix: str) -> Any:
        return next((dates[n + suffix] for n in names if dates.get(n + suffix) is not None), None)

    equal = first("")
    if equal is not None:
        return equal, None, equal, None
    return first("_gte"), first("_gt"), first("_lte"), first("_lt")


class Airflow:
    """
    One MWAA environment. ``environment``: its name; ``region_name`` /
    ``profile_name`` / keys: the AWS credentials (default chain when none);
    ``airflow_version``: 2 or 3 (read from the environment when unset).
    ``client``: an injected boto3 ``mwaa`` client (tests).
    """

    #: what a failed batched pipeline read does next (duckduck.common.idempotency)
    IDEMPOTENCY = checkpoint("dag_runs sort by their dates (order_by) and a logical-date filter reaches the API; the other tables don't sort", column="logical_date")

    def __init__(
        self,
        environment: str,
        region_name: Optional[str] = None,
        profile_name: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        airflow_version: Optional[Any] = None,
        default_page_size: int = PAGE_SIZE,
        client: Any = None,
    ):
        if not environment:
            raise ValueError("airflow: 'environment' is the MWAA environment's name")
        self.environment = environment
        self.region_name, self.profile_name = region_name, profile_name
        self._keys = {"aws_access_key_id": aws_access_key_id, "aws_secret_access_key": aws_secret_access_key,
                      "aws_session_token": aws_session_token}
        self._version = int(str(airflow_version).split(".")[0]) if airflow_version else None
        self.default_page_size = int(default_page_size)
        self._client = client
        self.base_url = f"mwaa://{environment}"  # what list_tables() shows as the endpoint

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "Airflow":
        """``environment`` (or ``name``) + optional AWS keys / region / profile — every key may come from either."""
        keys = ("region_name", "profile_name", "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
                "airflow_version")
        environment = overrides.pop("environment", None) or secret.get("environment") or secret.get("name")
        return cls(environment, **{k: overrides.pop(k, None) or secret.get(k) for k in keys}, **overrides)

    def __getstate__(self) -> Dict[str, Any]:  # travels to Spark executors: the client is rebuilt there
        state = dict(self.__dict__)
        state["_client"] = None
        return state

    # ------------------------------------------------------------------
    # The call
    # ------------------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise ImportError("boto3 is required for the Airflow (MWAA) connector.\n"
                                  "Install with:  pip install \"duckduck[aws]\"") from None
            keys = {k: v for k, v in self._keys.items() if v}
            session = boto3.Session(profile_name=None if keys else self.profile_name,
                                    region_name=self.region_name, **keys)
            self._client = session.client("mwaa")
        return self._client

    @property
    def airflow_version(self) -> int:
        """2 or 3 — the configured one, else the environment's (``GetEnvironment``), else 2."""
        if self._version is None:
            try:
                version = self.client.get_environment(Name=self.environment)["Environment"]["AirflowVersion"]
                self._version = int(str(version).split(".")[0])
            except Exception as exc:  # noqa: BLE001 — no permission to read it: assume Airflow 2
                logger.info("airflow: couldn't read %s's Airflow version (%s) — assuming 2; set "
                            "\"airflow_version\" to say", self.environment, exc)
                self._version = 2
        return self._version

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        logger.debug("airflow: GET %s %s", path, query)
        try:
            response = self.client.invoke_rest_api(Name=self.environment, Path=path, Method="GET",
                                                   QueryParameters=query)
        except Exception as exc:  # botocore ClientError: RestApiClientException / RestApiServerException / IAM
            err = getattr(exc, "response", None) or {}
            status = err.get("RestApiStatusCode")
            detail = err.get("RestApiResponse") or (err.get("Error") or {}).get("Message") or str(exc)
            if isinstance(detail, dict):
                detail = detail.get("detail") or detail.get("title") or detail
            raise AirflowError(f"airflow {self.environment}: GET {path} failed"
                               f"{f' ({status})' if status else ''}: {detail}", status) from None
        status = response.get("RestApiStatusCode")
        if status and status >= 400:
            raise AirflowError(f"airflow {self.environment}: GET {path} answered {status}: "
                               f"{response.get('RestApiResponse')}", status)
        return response.get("RestApiResponse") or {}

    def _pages(self, path: str, key: str, params: Dict[str, Any]) -> Iterator[List[Dict[str, Any]]]:
        size = self.default_page_size
        progress = PageProgress("airflow", path)

        def fetch_page(page: int):
            try:
                payload = self._get(path, {**params, "limit": size, "offset": page * size})
            except AirflowError as exc:
                if exc.status == 404:  # an unknown DAG / run: no rows
                    return [], 0
                raise
            rows = payload.get(key) or []
            total = payload.get("total_entries")
            total = int(total) if isinstance(total, (int, float)) else None
            if total is not None and len(rows) < size and page * size + len(rows) < total:
                # the webserver's maximum_page_limit is under the page size asked: the next offset would skip rows
                raise AirflowError(f"airflow {self.environment}: {path} returned {len(rows)} rows of the {size} "
                                   f"asked with more to come — its maximum_page_limit is lower; set "
                                   f"\"default_page_size\": {len(rows)} on the connector")
            progress.page(len(rows), total_pages=-(-total // size) if total else None, total_rows=total)
            return rows, total

        yield from slicing.pages(fetch_page, size)

    def _read(self, table: str, path: str, key: str, params: Dict[str, Any], keep,
              limit: Optional[int]) -> pd.DataFrame:
        """Every page — or, with ``limit``, pages until that many rows matched: the API caps a page at its
        ``maximum_page_limit`` (100 by default), and some filters here are looser than the WHERE (a substring
        pattern for an id, ``>=`` for ``>``), so one request of ``limit`` rows could come back short."""
        frames, kept = [], 0
        pages = self._pages(path, key, params)
        try:
            for page in pages:
                df = keep(self._frame(table, page))
                frames.append(df)
                kept += len(df)
                if limit is not None and kept >= limit:
                    break
        finally:
            pages.close()
        frames = [f for f in frames if len(f.columns)]
        out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return out.head(int(limit)) if limit is not None else out

    def _iter(self, table: str, path: str, key: str, params: Dict[str, Any], keep) -> Iterator[pd.DataFrame]:
        for page in self._pages(path, key, params):
            df = keep(self._frame(table, page))
            if len(df):
                yield df

    @staticmethod
    def _frame(table: str, rows: List[Dict[str, Any]]) -> pd.DataFrame:
        df = pd.json_normalize(rows, sep="_") if rows else pd.DataFrame()
        for col in _DATES.get(table, ()):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], utc=True, errors="coerce", format="ISO8601")
        return df

    # ------------------------------------------------------------------
    # Push-down: what each table's parameters become, per Airflow version
    # ------------------------------------------------------------------

    def _date_params(self, prefix: str, gte: Any, gt: Any, lte: Any, lt: Any) -> Dict[str, str]:
        """``>`` is sent as ``>=`` and ``<`` as ``<=`` (Airflow 2 has no strict ones): a superset."""
        out: Dict[str, str] = {}
        low, high = gte if gte is not None else gt, lte if lte is not None else lt
        if low is not None:
            out[f"{prefix}_gte"] = _moment(low)
        if high is not None:
            out[f"{prefix}_lte"] = _moment(high)
        return out

    @staticmethod
    def _matches(df: pd.DataFrame, column: str, pattern: Optional[str]) -> pd.Series:
        like = require_like(pattern, f"{column}_ilike")
        low, text = df[column].astype(str).str.lower(), like.text.lower()
        return {"equals": low == text, "startswith": low.str.startswith(text), "endswith": low.str.endswith(text),
                "contains": low.str.contains(text, regex=False)}[like.kind]

    def _keep(self, **exact: Any):
        """The rows that really match — the API's own filters are looser (a substring pattern for an id)."""
        def keep(df: pd.DataFrame) -> pd.DataFrame:
            for col, value in exact.items():
                if value is None or col.endswith("_ilike") and col[:-6] not in df.columns:
                    continue
                if col.endswith("_ilike"):
                    df = df[self._matches(df, col[:-6], value)]
                elif col in df.columns:
                    df = df[df[col].astype(str) == str(value) if not isinstance(value, bool) else df[col] == value]
            return df.reset_index(drop=True)
        return keep

    def _dags(self, dag_id, dag_id_ilike, is_paused):
        params: Dict[str, Any] = {"paused": is_paused}
        pattern = dag_id or (require_like(dag_id_ilike, "dag_id_ilike").text if dag_id_ilike else None)
        if pattern:
            params["dag_id_pattern"] = pattern  # a case-insensitive substring: a superset of both
        return "/dags", "dags", params, self._keep(dag_id=dag_id, dag_id_ilike=dag_id_ilike, is_paused=is_paused)

    def _dag_runs(self, dag_id, state, run_type, dates: Dict[str, Any], order_by=None):
        logical = "execution_date" if self.airflow_version < 3 else "logical_date"
        params: Dict[str, Any] = {"state": state}
        if order_by:  # one field, "-" for descending (the API's order_by): DuckAPI reads pages until the top N
            column, descending = order_by[0]
            field = _RUN_SORTS.get(column.lower(), column.lower())
            params["order_by"] = ("-" if descending else "") + (logical if field == "logical_date" else field)
        if run_type is not None and self.airflow_version >= 3:
            params["run_type"] = run_type
        params.update(self._date_params(logical, *_range(dates, "logical_date", "execution_date")))
        params.update(self._date_params("start_date", *_range(dates, "start_date")))
        params.update(self._date_params("end_date", *_range(dates, "end_date")))
        return (f"/dags/{dag_id or '~'}/dagRuns", "dag_runs", params,
                self._keep(dag_id=dag_id, state=state, run_type=run_type))

    def _task_instances(self, dag_id, dag_run_id, task_id, state, start_date_gte, start_date_gt, start_date_lte,
                        start_date_lt, end_date_gte, end_date_gt, end_date_lte, end_date_lt):
        params: Dict[str, Any] = {"state": state}
        if task_id is not None and self.airflow_version >= 3:
            params["task_id"] = task_id
        params.update(self._date_params("start_date", start_date_gte, start_date_gt, start_date_lte, start_date_lt))
        params.update(self._date_params("end_date", end_date_gte, end_date_gt, end_date_lte, end_date_lt))
        return (f"/dags/{dag_id or '~'}/dagRuns/{dag_run_id or '~'}/taskInstances", "task_instances", params,
                self._keep(dag_id=dag_id, dag_run_id=dag_run_id, task_id=task_id, state=state))

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    @spark_plan("partitioned", by="pages", max_parallel=2, why=_WHY)
    def dags(self, dag_id: Optional[str] = None, dag_id_ilike: Optional[str] = None,
             is_paused: Optional[bool] = None, limit: Optional[int] = None) -> pd.DataFrame:
        """The DAGs (active ones, as the API lists them): schedule, owners, tags, paused, next run."""
        path, key, params, keep = self._dags(dag_id, dag_id_ilike, is_paused)
        return self._read("dags", path, key, params, keep, limit)

    def iter_dags(self, dag_id: Optional[str] = None, dag_id_ilike: Optional[str] = None,
                  is_paused: Optional[bool] = None) -> Iterator[pd.DataFrame]:
        path, key, params, keep = self._dags(dag_id, dag_id_ilike, is_paused)
        return self._iter("dags", path, key, params, keep)

    @spark_plan("partitioned", by="pages", max_parallel=2, why=_WHY)
    @sortable(*_RUN_SORTS)
    def dag_runs(self, dag_id: Optional[str] = None, state: Optional[str] = None, run_type: Optional[str] = None,
                 logical_date: Optional[str] = None, logical_date_gte: Optional[str] = None,
                 logical_date_gt: Optional[str] = None, logical_date_lte: Optional[str] = None,
                 logical_date_lt: Optional[str] = None, execution_date: Optional[str] = None,
                 execution_date_gte: Optional[str] = None, execution_date_gt: Optional[str] = None,
                 execution_date_lte: Optional[str] = None, execution_date_lt: Optional[str] = None,
                 start_date: Optional[str] = None, start_date_gte: Optional[str] = None,
                 start_date_gt: Optional[str] = None, start_date_lte: Optional[str] = None,
                 start_date_lt: Optional[str] = None, end_date: Optional[str] = None,
                 end_date_gte: Optional[str] = None, end_date_gt: Optional[str] = None,
                 end_date_lte: Optional[str] = None, end_date_lt: Optional[str] = None,
                 order_by: Optional[List[Tuple[str, bool]]] = None, limit: Optional[int] = None) -> pd.DataFrame:
        """Every DAG run (``dag_id`` narrows it to one DAG): state, run type, logical / start / end dates.
        ``execution_date`` is Airflow 2's name of the logical date: either name filters at the API."""
        path, key, params, keep = self._dag_runs(dag_id, state, run_type, _dates(locals()), order_by)
        return self._read("dag_runs", path, key, params, keep, limit)

    @sortable(*_RUN_SORTS)
    def iter_dag_runs(self, dag_id: Optional[str] = None, state: Optional[str] = None,
                      run_type: Optional[str] = None, logical_date: Optional[str] = None,
                      logical_date_gte: Optional[str] = None, logical_date_gt: Optional[str] = None,
                      logical_date_lte: Optional[str] = None, logical_date_lt: Optional[str] = None,
                      execution_date: Optional[str] = None, execution_date_gte: Optional[str] = None,
                      execution_date_gt: Optional[str] = None, execution_date_lte: Optional[str] = None,
                      execution_date_lt: Optional[str] = None, start_date: Optional[str] = None,
                      start_date_gte: Optional[str] = None, start_date_gt: Optional[str] = None,
                      start_date_lte: Optional[str] = None, start_date_lt: Optional[str] = None,
                      end_date: Optional[str] = None, end_date_gte: Optional[str] = None,
                      end_date_gt: Optional[str] = None, end_date_lte: Optional[str] = None,
                      end_date_lt: Optional[str] = None,
                      order_by: Optional[List[Tuple[str, bool]]] = None) -> Iterator[pd.DataFrame]:
        path, key, params, keep = self._dag_runs(dag_id, state, run_type, _dates(locals()), order_by)
        return self._iter("dag_runs", path, key, params, keep)

    @spark_plan("partitioned", by="pages", max_parallel=2, why=_WHY)
    def task_instances(self, dag_id: Optional[str] = None, dag_run_id: Optional[str] = None,
                       task_id: Optional[str] = None, state: Optional[str] = None,
                       start_date_gte: Optional[str] = None, start_date_gt: Optional[str] = None,
                       start_date_lte: Optional[str] = None, start_date_lt: Optional[str] = None,
                       end_date_gte: Optional[str] = None, end_date_gt: Optional[str] = None,
                       end_date_lte: Optional[str] = None, end_date_lt: Optional[str] = None,
                       limit: Optional[int] = None) -> pd.DataFrame:
        """Every task instance (``dag_id`` / ``dag_run_id`` narrow it): state, try, duration, operator, pool."""
        path, key, params, keep = self._task_instances(dag_id, dag_run_id, task_id, state, start_date_gte,
                                                       start_date_gt, start_date_lte, start_date_lt, end_date_gte,
                                                       end_date_gt, end_date_lte, end_date_lt)
        return self._read("task_instances", path, key, params, keep, limit)

    def iter_task_instances(self, dag_id: Optional[str] = None, dag_run_id: Optional[str] = None,
                            task_id: Optional[str] = None, state: Optional[str] = None,
                            start_date_gte: Optional[str] = None, start_date_gt: Optional[str] = None,
                            start_date_lte: Optional[str] = None, start_date_lt: Optional[str] = None,
                            end_date_gte: Optional[str] = None, end_date_gt: Optional[str] = None,
                            end_date_lte: Optional[str] = None, end_date_lt: Optional[str] = None
                            ) -> Iterator[pd.DataFrame]:
        path, key, params, keep = self._task_instances(dag_id, dag_run_id, task_id, state, start_date_gte,
                                                       start_date_gt, start_date_lte, start_date_lt, end_date_gte,
                                                       end_date_gt, end_date_lte, end_date_lt)
        return self._iter("task_instances", path, key, params, keep)

    @spark_plan("partitioned", by="pages", max_parallel=2, why=_WHY)
    @sortable("timestamp")
    def import_errors(self, order_by: Optional[List[Tuple[str, bool]]] = None,
                      limit: Optional[int] = None) -> pd.DataFrame:
        """DAG files that failed to import: file, when, the stack trace."""
        return self._read("import_errors", "/importErrors", "import_errors", _order_param(order_by), self._keep(),
                          limit)

    @sortable("timestamp")
    def iter_import_errors(self, order_by: Optional[List[Tuple[str, bool]]] = None) -> Iterator[pd.DataFrame]:
        return self._iter("import_errors", "/importErrors", "import_errors", _order_param(order_by), self._keep())


__all__ = ["Airflow", "AirflowError"]

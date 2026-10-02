"""The Airflow (MWAA) connector against a fake ``mwaa`` client replaying InvokeRestApi's documented shapes
(botocore's MWAA model; Airflow's v1 / v2 OpenAPI specs)."""

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.common import slicing
from duckduck.connectors.api.airflow import Airflow, AirflowError


class ClientError(Exception):
    """What botocore raises for RestApiClientException: the Airflow status and body in ``response``."""

    def __init__(self, status, body):
        super().__init__(f"RestApiClientException {status}")
        self.response = {"RestApiStatusCode": status, "RestApiResponse": body,
                         "Error": {"Code": "RestApiClientException", "Message": "client error"}}


def _runs(n=7):
    return [{"dag_id": "etl" if i % 2 else "etl_daily", "dag_run_id": f"run_{i}",
             "state": "failed" if i % 3 == 0 else "success", "run_type": "scheduled",
             "logical_date": f"2026-10-0{1 + i % 2}T0{i}:00:00+00:00",
             "start_date": f"2026-10-01T0{i}:05:00+00:00", "end_date": None} for i in range(n)]


class FakeMWAA:
    def __init__(self, version="2.10.3", page_cap=100, dags=None, runs=None, sorts=True):
        self.version, self.page_cap, self.sorts = version, page_cap, sorts
        self.dags = dags if dags is not None else [
            {"dag_id": "etl", "is_paused": False, "tags": [{"name": "core"}], "owners": ["data"],
             "last_parsed_time": "2026-10-01T10:00:00+00:00"},
            {"dag_id": "etl_daily", "is_paused": True, "tags": [], "owners": ["data"],
             "last_parsed_time": "2026-10-01T09:00:00+00:00"}]
        self.runs = runs if runs is not None else _runs()
        self.calls = []

    def get_environment(self, Name):
        return {"Environment": {"Name": Name, "AirflowVersion": self.version}}

    def invoke_rest_api(self, Name, Path, Method, QueryParameters=None, Body=None):
        q = dict(QueryParameters or {})
        self.calls.append((Path, q))
        limit, offset = min(int(q.get("limit", 100)), self.page_cap), int(q.get("offset", 0))
        if Path == "/dags":
            rows = [d for d in self.dags if q.get("dag_id_pattern", "").lower() in d["dag_id"].lower()
                    and (q.get("paused") is None or d["is_paused"] == q["paused"])]
            key = "dags"
        elif Path.startswith("/dags/") and Path.endswith("/dagRuns"):
            dag = Path.split("/")[2]
            if dag not in ("~", "etl", "etl_daily"):
                raise ClientError(404, {"title": "DAG not found", "status": 404, "detail": f"{dag} not found"})
            rows = [r for r in self.runs if dag in ("~", r["dag_id"]) and q.get("state", r["state"]) == r["state"]]
            if q.get("order_by") and self.sorts:  # like Airflow's Postgres: NULLs last ascending, first descending
                field = q["order_by"].lstrip("-")
                field = "logical_date" if field == "execution_date" else field
                desc = q["order_by"].startswith("-")
                present = sorted((r for r in rows if r.get(field)), key=lambda r: r[field], reverse=desc)
                missing = [r for r in rows if not r.get(field)]
                rows = missing + present if desc else present + missing
            key = "dag_runs"
        elif Path.endswith("/taskInstances"):
            rows = [{"dag_id": "etl", "dag_run_id": "run_1", "task_id": t, "state": "success", "try_number": 1,
                     "start_date": "2026-10-01T01:06:00+00:00"} for t in ("extract", "load")]
            key = "task_instances"
        elif Path == "/importErrors":
            rows = [{"import_error_id": 1, "filename": "/dags/bad.py", "stack_trace": "SyntaxError",
                     "timestamp": "2026-10-01T08:00:00+00:00"}]
            key = "import_errors"
        else:
            raise ClientError(404, {"title": "Not Found"})
        return {"RestApiStatusCode": 200, "RestApiResponse": {key: rows[offset:offset + limit],
                                                               "total_entries": len(rows)}}


def _airflow(**kw):
    client = FakeMWAA(**kw)
    return Airflow("prod-env", client=client, default_page_size=3), client


def _duck(af):
    duck = DuckAPI()
    for t in ("dags", "dag_runs", "task_instances", "import_errors"):
        duck.register_api_function(f"airflow_{t}", getattr(af, t))
        duck.register_streaming_function(f"airflow_{t}", getattr(af, f"iter_{t}"))
    return duck


def test_every_page_is_read_until_the_total():
    af, client = _airflow()
    df = af.dag_runs()
    assert len(df) == 7 and [c[1]["offset"] for c in client.calls] == [0, 3, 6]
    assert all(c[0] == "/dags/~/dagRuns" for c in client.calls)  # every DAG: ~
    assert str(df["start_date"].dtype).startswith("datetime64") and str(df["start_date"].dt.tz) == "UTC"


def test_where_conditions_reach_the_api_in_its_own_parameters():
    af, client = _airflow()
    duck = _duck(af)
    df = duck.sql("SELECT dag_run_id FROM airflow_dag_runs WHERE dag_id = 'etl' AND state = 'failed' "
                  "AND start_date >= '2026-10-01 03:00' AND logical_date > '2026-10-01'").df()
    path, q = client.calls[0]
    assert path == "/dags/etl/dagRuns" and q["state"] == "failed"
    assert q["start_date_gte"] == "2026-10-01T03:00:00+00:00"  # naive = UTC
    assert q["execution_date_gte"] == "2026-10-01T00:00:00+00:00"  # Airflow 2's name; > sent as >=
    assert df["dag_run_id"].tolist() == ["run_3"]


def test_airflow_3_gets_its_own_parameter_names():
    af, client = _airflow(version="3.0.6")
    af.dag_runs(logical_date_lte="2026-10-02T00:00:00Z", run_type="manual")
    af.task_instances(task_id="load")
    assert client.calls[0][1]["logical_date_lte"] == "2026-10-02T00:00:00+00:00"
    assert client.calls[0][1]["run_type"] == "manual"
    path, q = client.calls[-1]
    assert path == "/dags/~/dagRuns/~/taskInstances" and q["task_id"] == "load"
    af2, client2 = _airflow()  # Airflow 2 has neither: filtered here instead
    assert af2.task_instances(task_id="load")["task_id"].tolist() == ["load"]
    assert "task_id" not in client2.calls[0][1]


def test_an_id_matched_by_pattern_is_then_matched_exactly():
    af, client = _airflow()
    assert af.dags(dag_id="etl")["dag_id"].tolist() == ["etl"]  # the API's pattern also returns etl_daily
    assert client.calls[0][1]["dag_id_pattern"] == "etl"
    assert af.dags(dag_id_ilike="%DAILY")["dag_id"].tolist() == ["etl_daily"]
    assert af.dags(is_paused=True)["dag_id"].tolist() == ["etl_daily"]


def test_a_limit_reads_until_enough_rows_matched():
    af, client = _airflow(runs=_runs(9))
    df = af.dag_runs(dag_id="etl", limit=3)  # the path filters exactly: one page is enough
    assert df["dag_run_id"].tolist() == ["run_1", "run_3", "run_5"] and len(client.calls) == 1
    client.calls.clear()
    df = af.dags(dag_id="etl_daily", limit=1)  # the API's pattern is looser: etl comes first, then etl_daily
    assert df["dag_id"].tolist() == ["etl_daily"]
    duck = _duck(af)
    assert len(duck.sql("SELECT * FROM airflow_dag_runs LIMIT 4").df()) == 4


def test_a_server_page_cap_under_the_page_size_fails_instead_of_skipping_rows():
    af, _ = _airflow(page_cap=2)
    with pytest.raises(AirflowError, match="set \"default_page_size\": 2"):
        af.dag_runs()


def test_an_unknown_dag_has_no_runs_and_other_errors_say_what_airflow_said():
    af, client = _airflow()
    assert af.dag_runs(dag_id="nope").empty

    class Denied(FakeMWAA):
        def invoke_rest_api(self, **kw):
            raise ClientError(403, {"title": "Forbidden", "detail": "no airflow:InvokeRestApi"})
    af2 = Airflow("prod-env", client=Denied(), airflow_version=2)
    with pytest.raises(AirflowError, match=r"GET /dags failed \(403\): no airflow:InvokeRestApi"):
        af2.dags()


def test_streaming_yields_one_frame_per_page_with_the_same_filters():
    af, _ = _airflow()
    pages = list(af.iter_dag_runs(state="success"))
    assert sum(len(p) for p in pages) == 4 and all(isinstance(p, pd.DataFrame) for p in pages)
    assert af.iter_import_errors().__next__()["filename"].tolist() == ["/dags/bad.py"]


def test_the_version_is_read_from_the_environment_once_or_assumed():
    af, client = _airflow(version="3.0.6")
    assert af.airflow_version == 3 and af.airflow_version == 3

    class NoPermission(FakeMWAA):
        def get_environment(self, Name):
            raise ClientError(403, {})
    assert Airflow("e", client=NoPermission()).airflow_version == 2
    assert Airflow("e", client=NoPermission(), airflow_version="3.1").airflow_version == 3


def test_spark_splits_it_by_pages():
    af, _ = _airflow()
    with slicing.probing() as probe:
        af.dag_runs()
    assert probe.seen and probe.total_rows == 7 and probe.page_size == 3
    with slicing.window(1, 2):
        assert af.dag_runs()["dag_run_id"].tolist() == ["run_3", "run_4", "run_5", "run_6"]
    import pickle
    clone = pickle.loads(pickle.dumps(af))
    assert clone._client is None and clone.environment == "prod-env"


def test_auto_register_builds_it_from_the_config(monkeypatch):
    import sys
    import types

    made = {}

    class Session:
        def __init__(self, **kw):
            made.update(kw)

        def client(self, name):
            made["service"] = name
            return FakeMWAA()
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(Session=Session))
    duck = DuckAPI()
    duck.auto_register({"airflow": {"connector": "airflow", "environment": "prod-env",
                                    "authentication": {"type": "local", "region_name": "us-east-1",
                                                       "profile_name": "data"}}})
    df = duck.sql("SELECT dag_id FROM airflow.dags ORDER BY dag_id").df()
    assert df["dag_id"].tolist() == ["etl", "etl_daily"]
    assert made == {"profile_name": "data", "region_name": "us-east-1", "service": "mwaa"}


def _many_runs(n=20, missing=()):
    return [{"dag_id": "etl", "dag_run_id": f"run_{i:02d}", "state": "success", "run_type": "scheduled",
             "logical_date": f"2026-09-{1 + i:02d}T00:00:00+00:00",
             "start_date": None if i in missing else f"2026-09-{1 + (i * 7) % 20:02d}T00:05:00+00:00",
             "end_date": None} for i in range(n)]


def _expected(runs, column, desc, n):
    df = pd.DataFrame(runs)
    df[column] = pd.to_datetime(df[column], utc=True)
    return df.sort_values(column, ascending=not desc, na_position="last")["dag_run_id"].head(n).tolist()


@pytest.mark.parametrize("desc", [True, False])
def test_order_by_and_limit_read_only_the_top_pages(desc, caplog):
    import logging

    af, client = _airflow(runs=_many_runs())
    duck = _duck(af)
    sql = f"SELECT dag_run_id FROM airflow_dag_runs ORDER BY start_date {'DESC' if desc else ''} LIMIT 4"
    with caplog.at_level(logging.INFO, logger="duckduck"):
        got = [r[0] for r in duck.sql(sql).fetchall()]
    assert got == _expected(_many_runs(), "start_date", desc, 4)
    assert client.calls[0][1]["order_by"] == ("-" if desc else "") + "start_date"
    assert len(client.calls) <= 4  # 4 rows: 2 pages of 3, plus the pages read ahead (2 at a time) — not all 7
    assert any("✓ ORDER BY start_date" in r.getMessage() for r in caplog.records)


def test_runs_without_a_start_still_rank_last_whatever_the_server_does_with_nulls():
    runs = _many_runs(missing={1, 3, 5, 8})  # the server puts them first when descending
    af, client = _airflow(runs=runs)
    duck = _duck(af)
    got = [r[0] for r in duck.sql("SELECT dag_run_id FROM airflow_dag_runs ORDER BY start_date DESC LIMIT 3")
           .fetchall()]
    assert got == _expected(runs, "start_date", True, 3)  # it read past the NULL runs to 3 with a start
    assert len(client.calls) < 7


def test_the_logical_date_is_execution_date_on_airflow_2_and_offset_is_counted():
    af, client = _airflow(runs=_many_runs())
    rows = _duck(af).sql("SELECT dag_run_id FROM airflow_dag_runs ORDER BY logical_date DESC LIMIT 2 OFFSET 2").fetchall()
    assert [r[0] for r in rows] == ["run_17", "run_16"]
    assert client.calls[0][1]["order_by"] == "-execution_date" and len(client.calls) <= 4


def test_a_server_that_ignores_the_order_is_read_to_the_end(caplog):
    af, client = _airflow(runs=_many_runs(), sorts=False)
    with caplog.at_level("WARNING", logger="duckduck"):
        got = [r[0] for r in _duck(af).sql("SELECT dag_run_id FROM airflow_dag_runs ORDER BY start_date DESC "
                                           "LIMIT 4").fetchall()]
    assert got == _expected(_many_runs(), "start_date", True, 4) and len(client.calls) == 7
    assert any("didn't come sorted by start_date" in r.getMessage() for r in caplog.records)


def test_what_the_source_cant_sort_by_stays_with_duckdb():
    af, client = _airflow(runs=_many_runs())
    duck = _duck(af)
    got = duck.sql("SELECT dag_run_id FROM airflow_dag_runs ORDER BY dag_run_id DESC LIMIT 2").fetchall()
    assert [r[0] for r in got] == ["run_19", "run_18"] and "order_by" not in client.calls[0][1]
    assert len(client.calls) == 7  # text sorts by the database's collation: not sent, every page read
    client.calls.clear()
    duck.sql("SELECT dag_run_id FROM airflow_dag_runs ORDER BY start_date DESC, end_date LIMIT 2").fetchall()
    assert "order_by" not in client.calls[0][1]  # one column only
    assert "ORDER BY end_date / execution_date / logical_date / start_date" in \
        duck.list_tables().set_index("name").loc["airflow_dag_runs", "pushdown"]

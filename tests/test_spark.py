"""Spark: every connector table declares how Spark reads it (``@spark_plan``), and ``SparkReader`` reads it that way.

The contract tests need no Spark: a new connector that doesn't decide its strategy fails here — see CLAUDE.md
"Spark: choosing the strategy". The reader tests run a local Spark when pyspark is installed."""

import inspect
import json
import math
import os

import pandas as pd
import pytest
import requests
from requests.adapters import BaseAdapter

from duckduck import DuckAPI, InsightVM, ServiceNow
from duckduck.common import slicing
from duckduck.connectors.local.files import FileTable, LocalFiles
from duckduck.connectors.registry import SERVICE_REGISTRY
from duckduck.common.sparkplan import STRATEGIES, SparkSource, plan_of, spark_plan


# -- the contract --------------------------------------------------------------------------------------------------


def _connector_class(spec):
    return getattr(spec.factory, "__self__", None)


def _declared():
    for name, spec in SERVICE_REGISTRY.items():
        cls = _connector_class(spec)
        for table, method in (spec.tables or {}).items():
            yield name, cls, table, getattr(cls, method)
    yield "files", FileTable, "<file>", FileTable.__call__


def test_every_table_of_every_connector_declares_how_spark_reads_it():
    missing = [f"{name}.{table}" for name, _, table, fn in _declared() if plan_of(fn) is None]
    assert not missing, (
        f"these tables don't declare a Spark strategy: {missing} — add @spark_plan(...) from duckduck.common.sparkplan "
        f"(CLAUDE.md, 'Spark: choosing the strategy')")


def test_every_plan_is_complete_and_its_hooks_exist():
    for name, cls, table, fn in _declared():
        plan = plan_of(fn)
        assert plan.strategy in STRATEGIES and plan.why, f"{name}.{table}: say why ({plan})"
        if plan.strategy == "native":
            hook = getattr(cls, plan.source, None)
            assert callable(hook), f"{name}.{table}: native source hook {plan.source!r} missing"
            wanted = set(inspect.signature(hook).parameters) - {"self"}
            accepted = set(inspect.signature(fn).parameters) - {"self", "where", "limit"}
            assert wanted <= accepted, f"{name}.{table}: {plan.source} takes {wanted - accepted}, the table doesn't"
        if plan.strategy == "partitioned":
            assert plan.by == "pages" or callable(getattr(cls, plan.by, None)), f"{name}.{table}: by={plan.by!r}"
            assert plan.max_parallel >= 1


def test_spark_plan_refuses_an_incomplete_declaration():
    with pytest.raises(ValueError, match="strategy"):
        spark_plan("fast")
    with pytest.raises(ValueError, match="source"):
        spark_plan("native")
    with pytest.raises(ValueError, match="by="):
        spark_plan("partitioned")


class _FakeInsightVM(BaseAdapter):
    """25 resources, served by page= and size= like InsightVM."""

    def __init__(self, total=25):
        super().__init__()
        self.total, self.pages = total, []

    def send(self, request, **kwargs):
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(request.url).query)
        size, page = int(q.get("size", ["10"])[0]), int(q.get("page", ["0"])[0])
        self.pages.append(page)
        rows = [{"id": i, "name": f"r{i}"} for i in range(page * size, min(self.total, (page + 1) * size))]
        body = {"resources": rows, "page": {"number": page, "size": size, "totalResources": self.total,
                                             "totalPages": math.ceil(self.total / size)}}
        r = requests.Response()
        r.status_code, r._content, r.url = 200, json.dumps(body).encode(), request.url
        r.headers["Content-Type"] = "application/json"
        return r

    def close(self):
        pass


def _insightvm(total=25):
    r7 = InsightVM("console.local", "u", "p", default_page_size=10)
    adapter = _FakeInsightVM(total)
    r7.session.mount("https://", adapter)
    return r7, adapter


def test_paged_tables_really_go_through_the_shared_pager():
    """by='pages' is only true when the table's read goes through duckduck.common.slicing.pages — probed for real."""
    r7, _ = _insightvm()
    for name, cls, table, fn in _declared():
        if cls is not InsightVM or plan_of(fn).strategy != "partitioned":
            continue
        required = {p.name: 1 for p in inspect.signature(fn).parameters.values()
                    if p.default is inspect.Parameter.empty and p.name != "self"}
        with slicing.probing() as probe:
            getattr(r7, fn.__name__)(**required)
        assert probe.seen and probe.total_rows == 25 and probe.page_size == 10, f"insightvm.{table}"


def test_a_window_reads_only_its_pages_and_windows_add_up_to_the_whole():
    r7, adapter = _insightvm(total=25)
    whole = r7.assets()
    adapter.pages.clear()
    with slicing.window(1, 2) as w:
        part = r7.assets()
    assert w.seen and adapter.pages == [1, 2] and part["id"].tolist() == list(range(10, 25))
    pieces = []
    for first, count in [(0, 1), (1, 1), (2, 1)]:
        with slicing.window(first, count):
            pieces += r7.assets()["id"].tolist()
    assert pieces == whole["id"].tolist() == list(range(25))


def test_servicenow_pages_by_offset_with_its_total_header():
    sn = ServiceNow("dev12345", "admin", "secret", default_page_size=2)
    rows = [{"sys_id": str(i), "number": f"INC{i}"} for i in range(5)]

    def get(url, params=None, timeout=None):
        off, lim = params["sysparm_offset"], params["sysparm_limit"]
        r = requests.Response()
        r.status_code, r._content = 200, json.dumps({"result": rows[off:off + lim]}).encode()
        r.headers["X-Total-Count"] = str(len(rows))
        return r

    sn.session.get = get
    with slicing.probing() as probe:
        sn.incidents()
    assert probe.seen and probe.total_rows == 5 and probe.page_size == 2
    with slicing.window(1, 2):
        assert sn.incidents()["sys_id"].tolist() == ["2", "3", "4"]


def test_native_sources_say_where_the_data_is(tmp_path):
    from duckduck.connectors.lake.blob_storage import BlobStorage
    from duckduck.connectors.databases.sql import SQLDatabase
    from duckduck.connectors.lake.glue import GlueTable

    glue = GlueTable.__new__(GlueTable)
    glue._table_cache = {"sec.logs": {"StorageDescriptor": {"Location": "s3://lake/sec/logs/"},
                                       "Parameters": {"table_type": "DELTA"}}}
    src = glue._spark_table("sec", "logs")
    assert (src.format, src.path, src.table) == ("delta", "s3://lake/sec/logs/", "sec.logs")

    blob = BlobStorage.__new__(BlobStorage)
    blob.account_name = "acct"
    assert blob._spark_table("raw", "/events/", "csv").path == "abfss://raw@acct.dfs.core.windows.net/events/"

    db = SQLDatabase(f"sqlite:///{tmp_path / 'x.db'}")
    assert db._spark_table("orders").options["url"] == f"jdbc:sqlite:{tmp_path / 'x.db'}"
    import sqlalchemy as sa

    db.engine = type("E", (), {"url": sa.engine.make_url("postgresql://me:pw@db.local/shop")})()
    opts = db._spark_query("SELECT 1")
    assert opts.options == {"url": "jdbc:postgresql://db.local:5432/shop", "driver": "org.postgresql.Driver",
                            "user": "me", "password": "pw", "query": "SELECT 1"}

    (tmp_path / "logs").mkdir()
    pd.DataFrame({"a": [1]}).to_csv(tmp_path / "logs" / "one.csv", index=False)
    table = LocalFiles(str(tmp_path)).table_functions()["logs"]
    src = table._spark_source()
    assert src.format == "csv" and src.options["recursiveFileLookup"] == "true" and src.path.endswith("logs")


# -- the reader (local Spark) ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def spark():
    pytest.importorskip("pyspark")
    pytest.importorskip("pyarrow")
    from pyspark.sql import SparkSession

    try:
        session = (SparkSession.builder.master("local[2]").appName("duckduck-tests")
                   .config("spark.ui.enabled", "false").config("spark.ui.showConsoleProgress", "false")
                   .config("spark.sql.shuffle.partitions", "2").getOrCreate())
    except Exception as exc:  # no Java, …
        pytest.skip(f"no local Spark: {exc}")
    session.sparkContext.setLogLevel("ERROR")
    import sys

    from pyspark import cloudpickle

    cloudpickle.register_pickle_by_value(sys.modules[__name__])  # the fakes here travel to the executors by value
    yield session
    session.stop()


def _lake(tmp_path):
    (tmp_path / "events").mkdir()
    pd.DataFrame({"host": ["h1", "h2", "h3"], "bytes": [5, 50, 500]}).to_parquet(tmp_path / "events" / "a.parquet")
    pd.DataFrame({"host": ["h4"], "bytes": [7]}).to_parquet(tmp_path / "events" / "b.parquet")
    pd.DataFrame({"host": ["h1", "h3"], "owner": ["ana", "bo"]}).to_csv(tmp_path / "owners.csv", index=False)


def test_each_table_read_its_own_way_in_one_query(spark, tmp_path):
    from duckduck.spark import SparkReader

    _lake(tmp_path)
    r7, _ = _insightvm(total=25)
    duck = DuckAPI()
    duck.auto_register({"lake": {"connector": "files", "path": str(tmp_path)}})
    duck.register_api_function("ivm_assets", r7.assets)
    duck.service_of["ivm_assets"] = "ivm"
    duck.service_prefix["ivm"] = "ivm"
    duck.register_api_function("tiers", lambda limit=None: [{"host": "h3", "tier": "gold"}])
    reader = SparkReader(spark, duck)

    df = reader.sql("SELECT e.host, e.bytes, o.owner, t.tier FROM lake.events e "
                    "JOIN lake.owners o ON o.host = e.host LEFT JOIN tiers t ON t.host = e.host "
                    "WHERE e.bytes > 6 ORDER BY e.host")
    assert [tuple(r) for r in df.collect()] == [("h3", 500, "bo", "gold")]
    used = {d["table"]: d["strategy"] for d in reader.decisions}
    assert used == {"lake_events": "native", "lake_owners": "native", "tiers": "driver"}

    assert reader.sql("SELECT count(*) AS n, max(id) AS top FROM ivm.assets").collect()[0].asDict() == {"n": 25,
                                                                                                        "top": 24}
    assert reader.decisions[-1]["strategy"] == "partitioned" and "3 pages" in reader.decisions[-1]["detail"]
    assert reader.sql("SELECT * FROM ivm.assets LIMIT 3").count() == 3
    assert reader.decisions[-1]["detail"] == "LIMIT reached the source: one request"


def test_custom_pieces_saved_tables_and_explain(spark):
    from duckduck.spark import SparkReader

    class Weather:
        @spark_plan("partitioned", by="days", max_parallel=2, why="one request per day, independent")
        def readings(self, day_gte=None, day_lte=None, limit=None):
            return [{"day": d, "temp": 20 + d} for d in range(int(day_gte or 1), int(day_lte or 5) + 1)]

        def days(self, day_gte=None, day_lte=None, limit=None):
            return [{"day_gte": d, "day_lte": d} for d in range(int(day_gte or 1), int(day_lte or 5) + 1)]

    w = Weather()
    duck = DuckAPI()
    duck.register_api_function("readings", w.readings)
    reader = SparkReader(spark, duck)
    rows = reader.sql("SELECT day, temp FROM readings WHERE day >= 2 AND day <= 4 ORDER BY day").collect()
    assert [tuple(r) for r in rows] == [(2, 22), (3, 23), (4, 24)]
    assert reader.decisions[0]["detail"].startswith("3 pieces")
    assert reader.explain("readings") == {"table": "readings", "strategy": "partitioned", "declared": True,
                                          "why": "one request per day, independent", "source": None, "by": "days",
                                          "max_parallel": 2}

    duck.register_view("hot", {"sql": "SELECT * FROM readings WHERE temp > 23"})
    assert [r.day for r in reader.sql("SELECT day FROM hot ORDER BY day").collect()] == [4, 5]


def test_a_native_read_that_fails_falls_back_to_the_driver(spark):
    from duckduck.spark import SparkReader

    class Odd:
        @spark_plan("native", source="_src", why="test")
        def things(self, where=None, limit=None):
            return [{"a": 1}]

        def _src(self):
            return SparkSource("no-such-format", path="/nowhere")

    duck = DuckAPI()
    duck.register_api_function("things", Odd().things)
    reader = SparkReader(spark, duck)
    assert reader.sql("SELECT a FROM things").collect()[0].a == 1
    assert reader.decisions[0]["strategy"] == "driver" and "native read failed" in reader.decisions[0]["detail"]
    with pytest.raises(Exception):
        SparkReader(spark, duck, fallback=False).sql("SELECT a FROM things").collect()


def test_a_driver_read_can_land_page_by_page_in_parquet(spark, tmp_path):
    from duckduck.spark import SparkReader

    def rows(limit=None):
        return [{"n": i, "tag": None if i < 3 else f"t{i}"} for i in range(6)]

    def iter_rows():
        yield pd.DataFrame([{"n": i, "tag": None} for i in range(3)])  # the first page: tag all empty
        yield pd.DataFrame([{"n": i, "tag": f"t{i}"} for i in range(3, 6)])

    duck = DuckAPI()
    duck.register_api_function("rows", rows)
    duck.register_streaming_function("rows", iter_rows)
    reader = SparkReader(spark, duck, staging_path=str(tmp_path / "stage"))
    out = reader.sql("SELECT n, tag FROM rows ORDER BY n").collect()
    assert [r.tag for r in out] == [None, None, None, "t3", "t4", "t5"]  # the empty first page didn't type it INTEGER
    assert "Parquet" in reader.decisions[0]["detail"]
    assert len(os.listdir(next((tmp_path / "stage").iterdir()))) == 2

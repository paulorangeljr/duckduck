"""The Athena connector against a fake Athena client (the boto3 API's documented shapes): push-down into Athena
SQL, results through the API or UNLOAD → Parquet read by DuckDB, cancel → StopQueryExecution, the catalog."""

import duckdb
import pandas as pd
import pytest

import duckduck.athena as athena_module
from duckduck import DuckAPI, progress
from duckduck.athena import Athena, AthenaError
from duckduck.lakehouse import LakehouseConnection

COLUMNS = [{"Name": "host", "Type": "string"}, {"Name": "n", "Type": "int"},
           {"Name": "tags", "Type": "array<string>"}]
PARTITIONS = [{"Name": "day", "Type": "date"}]


class Pages:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return self.pages(**kwargs)


class FakeAthena:
    def __init__(self, rows=(("a", "1", "[x]", "2026-09-30", "s"),), states=("RUNNING", "SUCCEEDED"), reason=None,
                 on_poll=None):
        self.rows, self.states, self.reason, self.on_poll = list(rows), list(states), reason, on_poll
        self.started, self.stopped, self.polls = [], [], 0

    def start_query_execution(self, **request):
        self.started.append(request)
        return {"QueryExecutionId": f"q{len(self.started)}"}

    def get_query_execution(self, QueryExecutionId):
        self.polls += 1
        if self.on_poll:
            self.on_poll()
        state = self.states[min(self.polls - 1, len(self.states) - 1)]
        return {"QueryExecution": {"QueryExecutionId": QueryExecutionId, "StatementType": "DML",
                                   "Status": {"State": state, "StateChangeReason": self.reason},
                                   "Statistics": {"DataScannedInBytes": 12_000_000}}}

    def stop_query_execution(self, QueryExecutionId):
        self.stopped.append(QueryExecutionId)

    def get_work_group(self, WorkGroup):
        return {"WorkGroup": {"Configuration": {"ResultConfiguration": {"OutputLocation": "s3://results/athena/"}}}}

    def get_table_metadata(self, CatalogName, DatabaseName, TableName):
        return {"TableMetadata": {"Name": TableName, "Columns": COLUMNS, "PartitionKeys": PARTITIONS,
                                  "Parameters": {"location": f"s3://lake/{TableName}/"}}}

    def get_paginator(self, name):
        if name == "get_query_results":
            info = [{"Name": "host", "Type": "varchar"}, {"Name": "n", "Type": "integer"},
                    {"Name": "tags", "Type": "array"}, {"Name": "day", "Type": "date"},
                    {"Name": "src", "Type": "varchar"}]  # not in the table's metadata (a view's extra column)
            header = {"Data": [{"VarCharValue": c["Name"]} for c in info]}
            data = [{"Data": [{"VarCharValue": v} if v is not None else {} for v in r]} for r in self.rows]
            return Pages(lambda **kw: [{"ResultSet": {"ResultSetMetadata": {"ColumnInfo": info},
                                                      "Rows": [header] + data}}])
        if name == "list_databases":
            return Pages(lambda **kw: [{"DatabaseList": [{"Name": "logs"}, {"Name": "sales"}]}])
        if name == "list_table_metadata":
            return Pages(lambda **kw: [{"TableMetadataList": [
                {"Name": "events", "TableType": "EXTERNAL_TABLE", "Columns": COLUMNS, "PartitionKeys": PARTITIONS,
                 "Parameters": {"location": "s3://lake/events/", "classification": "parquet"}}]}])
        raise KeyError(name)


def _duck(fake, **options):
    duck = DuckAPI()
    duck.auto_register({"athena": {"connector": "athena", "client": fake, "poll_interval": 0.001, **options}})
    return duck


def test_where_and_limit_become_athena_sql_with_typed_values():
    fake = FakeAthena()
    duck = _duck(fake, results="api")
    rows = duck.sql("SELECT host, n, day FROM athena.logs.events WHERE host = 'a' AND day >= '2026-09-01' "
                    "AND n > 0 LIMIT 5").fetchall()
    assert [(r[0], r[1], str(r[2])) for r in rows] == [("a", 1, "2026-09-30")]
    request = fake.started[-1]
    assert request["QueryString"] == ('SELECT * FROM "logs"."events" WHERE "host" = \'a\' AND '
                                      '"day" >= DATE \'2026-09-01\' AND "n" > 0 LIMIT 5')
    assert request["QueryExecutionContext"] == {"Catalog": "AwsDataCatalog", "Database": "logs"}
    assert request["WorkGroup"] == "primary"


def test_a_condition_athena_can_t_take_stays_with_duckdb_and_drops_the_limit():
    fake = FakeAthena(rows=[("a", "1", None, "2026-09-30", "x"), ("ab", "2", None, "2026-09-30", "y")])
    duck = _duck(fake, results="api")
    rows = duck.sql("SELECT host FROM athena.logs.events WHERE host ILIKE 'A%' AND src = 'y' LIMIT 1").fetchall()
    assert rows == [("ab",)]
    sql = fake.started[-1]["QueryString"]
    assert "lower(\"host\") LIKE lower('A%')" in sql and '"src"' not in sql and "LIMIT" not in sql


def test_a_join_sends_its_keys_as_an_in():
    fake = FakeAthena()
    duck = _duck(fake, results="api")
    duck.register_api_function("hosts", lambda limit=None: [{"name": "a"}, {"name": "b"}])
    duck.sql("SELECT e.n FROM hosts h JOIN athena.logs.events e ON e.host = h.name").fetchall()
    assert "\"host\" IN ('a', 'b')" in fake.started[-1]["QueryString"] or \
        "\"host\" IN ('b', 'a')" in fake.started[-1]["QueryString"]


def test_big_answers_come_back_as_unloaded_parquet_read_by_duckdb(tmp_path, monkeypatch):
    local = tmp_path / "part-0"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 'a' AS host, 1 AS n) TO '{local}' (FORMAT parquet)")
    fake = FakeAthena()
    listed, deleted = [], []

    class FakeS3:
        def get_paginator(self, name):
            def pages(Bucket, Prefix):
                listed.append((Bucket, Prefix))
                return [{"Contents": [{"Key": Prefix + "20260930_abc_0", "Size": 10}, {"Key": Prefix + "_SUCCESS",
                                                                                         "Size": 0}]}]
            return Pages(pages)

        def delete_objects(self, Bucket, Delete):
            deleted.extend(o["Key"] for o in Delete["Objects"])

    monkeypatch.setattr(LakehouseConnection, "create_secret", lambda self, sql: None)
    a = Athena(client=fake, s3_client=FakeS3(), poll_interval=0.001)
    real = a._unload
    a._unload = lambda select, db: (lambda r: (r[0], [str(local) for _ in r[1]]))(real(select, db))
    df = a.table("logs", "events")
    assert df.to_dict("records") == [{"host": "a", "n": 1}]
    unload = fake.started[-1]["QueryString"]
    assert unload.startswith("UNLOAD (SELECT * FROM \"logs\".\"events\") TO 's3://results/athena/duckduck-unload/")
    assert "format = 'PARQUET'" in unload and "ResultReuseConfiguration" not in fake.started[-1]
    assert listed[0][0] == "results" and deleted and all(k.startswith("athena/duckduck-unload/") for k in deleted)
    # a small LIMIT goes through the API instead
    a.table("logs", "events", limit=10)
    assert not fake.started[-1]["QueryString"].startswith("UNLOAD")


def test_a_failed_query_says_why_and_a_cancel_stops_it():
    with pytest.raises(AthenaError, match="SYNTAX_ERROR: line 1"):
        Athena(client=FakeAthena(states=["FAILED"], reason="SYNTAX_ERROR: line 1"), results="api").query("SELEC 1")
    p = progress.Progress()
    fake = FakeAthena(states=["RUNNING"], on_poll=p.cancel)  # cancelled while Athena runs it
    with progress.tracking(p), pytest.raises(progress.Cancelled):
        Athena(client=fake, results="api", poll_interval=0.001).query("SELECT 1")
    assert fake.stopped == ["q1"]


def test_result_reuse_and_the_catalog():
    fake = FakeAthena()
    a = Athena(client=fake, results="api", reuse_minutes=60, poll_interval=0.001)
    a.query("SELECT 1")
    assert fake.started[-1]["ResultReuseConfiguration"] == {
        "ResultReuseByAgeConfiguration": {"Enabled": True, "MaxAgeInMinutes": 60}}
    assert a.databases()["database"].tolist() == ["logs", "sales"]
    tables = a.tables(database="logs")
    assert tables.loc[0, "table_name"] == "events" and tables.loc[0, "partition_keys"] == "day"
    assert a.columns("logs", "events")["column_name"].tolist() == ["host", "n", "tags", "day"]
    duck = _duck(FakeAthena(), results="api")
    nested, notes = duck.nested_tables(service="athena")
    assert notes == [] and {n["address"] for n in nested} == {"athena.logs.events", "athena.sales.events"}


def test_names_are_checked_and_values_escaped():
    a = Athena(client=FakeAthena(), results="api")
    with pytest.raises(ValueError, match="not an Athena"):
        a.table("logs", 'events"; DROP')
    from duckduck.pushdown import Condition
    sql, complete = a._select("logs", "events", [Condition("host", "eq", "o'brien")], None)
    assert "\"host\" = 'o''brien'" in sql and complete


class ManyDatabases(FakeAthena):
    """A catalog with many databases, each listed in pages of 2 tables — what made `SELECT * FROM athena.tables`
    look stuck: one listing per database, one after the other, with nothing shown meanwhile."""

    def __init__(self, databases=12, tables=3, on_page=None):
        super().__init__()
        self.databases_, self.tables_, self.on_page, self.listed = databases, tables, on_page, []

    def get_paginator(self, name):
        if name == "list_databases":
            return Pages(lambda **kw: [{"DatabaseList": [{"Name": f"db{i:02d}"} for i in range(self.databases_)]}])
        if name == "list_table_metadata":
            def pages(**kw):
                db = kw["DatabaseName"]
                self.listed.append(db)
                names = [f"t{j}" for j in range(self.tables_)]
                for k in range(0, len(names), 2):
                    if self.on_page:
                        self.on_page(db)
                    yield {"TableMetadataList": [{"Name": n, "TableType": "EXTERNAL_TABLE", "Columns": COLUMNS,
                                                  "Parameters": {"classification": "parquet"}}
                                                 for n in names[k:k + 2]]}
            return Pages(pages)
        return super().get_paginator(name)


def test_every_database_is_listed_at_once_in_order_and_says_how_far_it_is():
    fake = ManyDatabases()
    ath = Athena(client=fake, list_threads=4)
    p = progress.Progress()
    with progress.tracking(p):
        p.step("fetching", "athena_tables")
        df = ath.tables()
    assert len(df) == 36 and df["database"].tolist() == [f"db{i:02d}" for i in range(12) for _ in range(3)]
    assert "athena tables: 12/12 database(s) · 36 table(s)" in [e["text"] for e in p.to_dict()["events"]]


def test_a_limit_stops_listing_once_it_has_its_rows():
    fake = ManyDatabases(databases=40)
    df = Athena(client=fake, list_threads=1).tables(limit=5)
    assert len(df) == 5 and fake.listed == ["db00", "db01"]


def test_a_cancel_stops_the_listing_between_pages():
    p = progress.Progress()
    seen = []

    def on_page(db):
        seen.append(db)
        if len(seen) == 3:
            p.cancel()
    fake = ManyDatabases(databases=30, on_page=on_page)
    with progress.tracking(p), pytest.raises(progress.Cancelled):
        Athena(client=fake, list_threads=1).tables()
    assert len(fake.listed) <= 3

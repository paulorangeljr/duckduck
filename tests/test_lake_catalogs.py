"""The catalog apart from the storage: Glue (moto), Unity Catalog (a fake API), Iceberg (pyiceberg's SQL catalog)."""

import json
import os

import duckdb
import pytest

from duckduck import DuckAPI
from duckduck.pipeline import PipelineError, load_spec, run_pipeline
from duckduck.pipeline.catalogs import (
    Column, GlueCatalog, TableInfo, UnityCatalog, arrow_to_hive, fits, hive_escape, make_catalog,
    partitions_from_files, plan_schema,
)
from duckduck.pipeline.spec import watch_key

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")


@pytest.fixture
def glue(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        yield boto3.client("glue", region_name="us-east-1")


@pytest.fixture
def lake(glue, tmp_path):
    return GlueCatalog("lake", region="us-east-1", warehouse=str(tmp_path / "wh"), client=glue)


def pipeline(sql, **target):
    return {"pipeline": "p", "primary_key": "id", "sql": sql, "audit_columns": False,  # exact schemas below
            "target": {"catalog": "lake", "table": "silver.things", **target}}


def partitions(glue, table="things"):
    return sorted(p["Values"][0] for p in glue.get_partitions(DatabaseName="silver", TableName=table)["Partitions"])


# -- types and schema rules --------------------------------------------------------------------------------------------


def test_types_and_what_fits():
    import pyarrow as pa

    assert arrow_to_hive(pa.int64()) == "bigint" and arrow_to_hive(pa.decimal128(10, 2)) == "decimal(10,2)"
    assert arrow_to_hive(pa.list_(pa.struct([("a", pa.int32())]))) == "array<struct<a:int>>"
    assert arrow_to_hive(pa.map_(pa.string(), pa.float64())) == "map<string,double>"
    assert fits("int", "bigint") and fits("float", "double") and fits("decimal(5,2)", "decimal(10,3)")
    assert fits("int", "decimal(12,2)") and not fits("bigint", "decimal(12,2)")
    assert not fits("bigint", "int") and not fits("string", "int") and not fits("double", "float")
    assert hive_escape("2026-10-01 10:00:00") == "2026-10-01 10%3A00%3A00" and hive_escape(None) == \
        "__HIVE_DEFAULT_PARTITION__"
    files = ["/wh/t/day=2026-10-01/region=SP/part-0.parquet", "/wh/t/day=2026-10-01/region=SP/part-1.parquet",
             "/wh/t/day=2026-10-02/region=a%2Fb/part-0.parquet"]
    assert partitions_from_files("/wh/t", files, ["day", "region"]) == [
        ({"day": "2026-10-01", "region": "SP"}, "day=2026-10-01/region=SP"),
        ({"day": "2026-10-02", "region": "a/b"}, "day=2026-10-02/region=a%2Fb")]


def test_schema_plan_evolve_strict_and_overwrite():
    table = TableInfo("silver", "t", "/x", "parquet", columns=[Column("id", "bigint"), Column("v", "string")],
                      partition_keys=[Column("day", "string")])
    view = [Column("id", "int"), Column("score", "double"), Column("day", "string")]
    plan = plan_schema(table, view, "evolve", "append", ["day"])
    assert plan.casts == {"id": "bigint"} and [c.name for c in plan.added] == ["score"]
    assert [c.name for c in plan.missing] == ["v"]
    with pytest.raises(PipelineError) as e:
        plan_schema(table, view, "strict", "append", ["day"])
    assert "score (double) is new" in str(e.value) and "v (string) is in silver.t" in str(e.value)
    with pytest.raises(PipelineError, match="id is string in the view, bigint in silver.t"):
        plan_schema(table, [Column("id", "string"), Column("day", "string")], "evolve", "append", ["day"])
    with pytest.raises(PipelineError, match="partitioned by day, which the view doesn't have"):
        plan_schema(table, [Column("id", "bigint")], "evolve", "append", [])
    assert plan_schema(table, view, "overwrite", "overwrite", ["day"]).replace
    with pytest.raises(PipelineError, match="only with \"mode\": \"overwrite\""):
        plan_schema(table, view, "overwrite", "append", ["day"])


def test_the_file_takes_catalogs_and_composite_watch_keys():
    spec = load_spec({"pipeline": "x", "sql": "SELECT 1", "primary_key": ["order_id", "line"], "catalog": "lake",
                      "catalogs": {"lake": {"type": "glue", "region": "us-east-1"}},
                      "targets": {"a": {"table": "silver.a", "mode": "merge"}, "b": "s3://lake/b/"},
                      "sip": {"watch": ["A-1|1", ["A-1", 2], {"line": 3, "order_id": "A-1"}, ["B", True]]}})
    a, b = spec.targets
    assert (a.catalog, a.table, a.where) == ("lake", "silver.a", "lake:silver.a") and b.catalog is None
    assert spec.sip.watch == ["A-1|1", "A-1|2", "A-1|3", "B|true"]
    with pytest.raises(PipelineError, match="must be the primary_key's"):
        watch_key({"order": 1}, ["order_id", "line"])
    with pytest.raises(PipelineError, match="give it the 'table'"):
        load_spec({"pipeline": "x", "sql": "SELECT 1", "target": {"catalog": "lake", "path": "/x"}})
    with pytest.raises(PipelineError, match="'type' is glue, unity or iceberg"):
        load_spec({"pipeline": "x", "sql": "SELECT 1", "catalogs": {"lake": {"type": "hive"}}})
    with pytest.raises(PipelineError, match="only with \"mode\": \"overwrite\""):
        load_spec({"pipeline": "x", "sql": "SELECT 1", "target": {"catalog": "l", "table": "a.b", "schema": "overwrite"}})


# -- Glue: Parquet -----------------------------------------------------------------------------------------------------


def test_glue_parquet_table_created_evolved_and_its_partitions_kept(glue, lake, tmp_path):
    duck = DuckAPI()
    spec = pipeline("SELECT * FROM (VALUES (1, 'a', '2026-10-01'), (2, 'b', '2026-10-02')) t(id, v, day)",
                    mode="append", partition_by="day")
    run = run_pipeline(spec, duck=duck, catalogs={"lake": lake})
    assert run.writes[0]["created"] and run.writes[0]["partitions_added"] == 2
    t = glue.get_table(DatabaseName="silver", Name="things")["Table"]
    assert t["StorageDescriptor"]["Location"] == str(tmp_path / "wh" / "silver" / "things")
    assert [(c["Name"], c["Type"]) for c in t["StorageDescriptor"]["Columns"]] == [("id", "int"), ("v", "string")]
    assert [c["Name"] for c in t["PartitionKeys"]] == ["day"]
    assert t["StorageDescriptor"]["SerdeInfo"]["SerializationLibrary"].endswith("ParquetHiveSerDe")
    assert partitions(glue) == ["2026-10-01", "2026-10-02"]
    one = glue.get_partition(DatabaseName="silver", TableName="things", PartitionValues=["2026-10-01"])["Partition"]
    assert one["StorageDescriptor"]["Location"].endswith("/silver/things/day=2026-10-01")
    assert os.listdir(one["StorageDescriptor"]["Location"])  # the files are where Glue says

    # a new column joins the table; a narrower int is written as the table's type
    spec["sql"] = "SELECT CAST(3 AS TINYINT) AS id, 'c' AS v, '2026-10-03' AS day, 1.5::DOUBLE AS score"
    run = run_pipeline(spec, duck=duck, catalogs={"lake": lake})
    assert run.writes[0]["columns_added"] == ["score"] and run.writes[0]["partitions_added"] == 1
    t = glue.get_table(DatabaseName="silver", Name="things")["Table"]
    assert [(c["Name"], c["Type"]) for c in t["StorageDescriptor"]["Columns"]] == [
        ("id", "int"), ("v", "string"), ("score", "double")]
    typed = duckdb.sql(f"SELECT typeof(id) FROM read_parquet('{tmp_path}/wh/silver/things/day=2026-10-03/*.parquet')")
    assert typed.fetchone()[0] == "INTEGER"

    # a column of another type: refused before anything is written
    spec["sql"] = "SELECT 'x' AS id, 'd' AS v, '2026-10-04' AS day"
    with pytest.raises(PipelineError, match="id is string in the view, int in silver.things"):
        run_pipeline(spec, duck=duck, catalogs={"lake": lake})
    assert not os.path.exists(tmp_path / "wh" / "silver" / "things" / "day=2026-10-04")

    # an overwrite leaves only its partitions, in the files and in Glue
    spec["sql"] = "SELECT 9 AS id, 'z' AS v, '2026-10-09' AS day, 0.0::DOUBLE AS score"
    spec["target"]["mode"] = "overwrite"
    run = run_pipeline(spec, duck=duck, catalogs={"lake": lake})
    assert run.writes[0]["partitions_removed"] == 3 and partitions(glue) == ["2026-10-09"]


def test_glue_schema_overwrite_and_strict(glue, lake):
    duck = DuckAPI()
    spec = pipeline("SELECT 1 AS id, 'a' AS v")
    run_pipeline(spec, duck=duck, catalogs={"lake": lake})
    spec["target"]["schema"] = "strict"
    spec["sql"] = "SELECT 2 AS id, 'b' AS v, 3 AS extra"
    with pytest.raises(PipelineError, match="extra \\(int\\) is new"):
        run_pipeline(spec, duck=duck, catalogs={"lake": lake})
    spec["target"].update(schema="overwrite", mode="overwrite")
    spec["sql"] = "SELECT 'k' AS id, 2.5::DOUBLE AS amount"
    run = run_pipeline(spec, duck=duck, catalogs={"lake": lake})
    assert run.writes[0]["schema_replaced"]
    cols = glue.get_table(DatabaseName="silver", Name="things")["Table"]["StorageDescriptor"]["Columns"]
    assert [(c["Name"], c["Type"]) for c in cols] == [("id", "string"), ("amount", "double")]


def test_projected_tables_need_no_partitions(glue, lake, tmp_path):
    glue.create_database(DatabaseInput={"Name": "silver"})
    glue.create_table(DatabaseName="silver", TableInput={
        "Name": "things", "TableType": "EXTERNAL_TABLE",
        "Parameters": {"projection.enabled": "true", "projection.day.type": "date"},
        "PartitionKeys": [{"Name": "day", "Type": "string"}],
        "StorageDescriptor": {"Location": str(tmp_path / "proj"), "Columns": [{"Name": "id", "Type": "int"}]}})
    run = run_pipeline(pipeline("SELECT 1 AS id, '2026-10-01' AS day"), duck=DuckAPI(), catalogs={"lake": lake})
    assert "partitions_added" not in run.writes[0] and partitions(glue) == []
    assert os.path.isdir(tmp_path / "proj" / "day=2026-10-01")


def test_a_new_table_needs_a_place(glue):
    lake = GlueCatalog("lake", region="us-east-1", client=glue)
    with pytest.raises(PipelineError, match="Give the target a 'path', or the catalog a 'warehouse'"):
        run_pipeline(pipeline("SELECT 1 AS id"), duck=DuckAPI(), catalogs={"lake": lake})


# -- Glue: Delta -------------------------------------------------------------------------------------------------------


def test_glue_delta_table_registered_and_its_schema_merged(glue, lake):
    pytest.importorskip("deltalake")
    from deltalake import DeltaTable

    duck = DuckAPI()
    spec = pipeline("SELECT 1 AS id, 'a' AS v", format="delta", mode="merge")
    assert run_pipeline(spec, duck=duck, catalogs={"lake": lake}).writes[0]["created"]
    spec["sql"] = "SELECT 1 AS id, 'A' AS v, true AS flag UNION ALL SELECT 2, 'b', false"
    run = run_pipeline(spec, duck=duck, catalogs={"lake": lake})
    assert run.writes[0]["columns_added"] == ["flag"]
    t = glue.get_table(DatabaseName="silver", Name="things")["Table"]
    assert t["Parameters"]["table_type"] == "DELTA" and t["Parameters"]["spark.sql.sources.provider"] == "delta"
    assert t["StorageDescriptor"]["SerdeInfo"]["Parameters"]["path"] == t["StorageDescriptor"]["Location"]
    rows = sorted(DeltaTable(t["StorageDescriptor"]["Location"]).to_pyarrow_table().to_pylist(), key=lambda r: r["id"])
    assert rows == [{"id": 1, "v": "A", "flag": True}, {"id": 2, "v": "b", "flag": False}]
    spec["target"]["format"] = "parquet"
    with pytest.raises(PipelineError, match="is a delta table; the target says parquet"):
        run_pipeline(spec, duck=duck, catalogs={"lake": lake})


# -- Iceberg (pyiceberg) -----------------------------------------------------------------------------------------------


@pytest.fixture
def ice(tmp_path):
    pytest.importorskip("pyiceberg")
    pytest.importorskip("sqlalchemy")
    (tmp_path / "iwh").mkdir()
    return {"type": "iceberg", "uri": f"sqlite:///{tmp_path / 'ice.db'}", "warehouse": f"file://{tmp_path / 'iwh'}"}


def iceberg_rows(block, name="things"):
    from pyiceberg.catalog import load_catalog

    cat = load_catalog("ice", **{k: v for k, v in block.items() if k != "type"})
    t = cat.load_table(("gold", name))
    return t, sorted(t.scan().to_arrow().to_pylist(), key=lambda r: r["id"])


def test_iceberg_tables_created_merged_and_evolved(ice):
    duck = DuckAPI()
    spec = {"pipeline": "i", "primary_key": "id", "catalogs": {"ice": ice}, "audit_columns": False,
            "sql": "SELECT * FROM (VALUES (1, 'a', '2026-10-01'), (2, 'b', '2026-10-02')) t(id, v, day)",
            "target": {"catalog": "ice", "table": "gold.things", "format": "iceberg", "mode": "merge",
                       "partition_by": "day"}}
    assert run_pipeline(spec, duck=duck).writes[0]["created"]
    spec["sql"] = ("SELECT CAST(2 AS SMALLINT) AS id, 'B' AS v, '2026-10-02' AS day, 7 AS n "
                   "UNION ALL SELECT 3, 'c', '2026-10-03', NULL")
    w = run_pipeline(spec, duck=duck).writes[0]
    assert (w["columns_added"], w["rows_updated"], w["rows_inserted"]) == (["n"], 1, 1)
    table, rows = iceberg_rows(ice)
    assert [f.name for f in table.schema().fields] == ["id", "v", "day", "n"]
    assert [f.name for f in table.spec().fields] == ["day"]
    assert rows == [{"id": 1, "v": "a", "day": "2026-10-01", "n": None},
                    {"id": 2, "v": "B", "day": "2026-10-02", "n": 7},
                    {"id": 3, "v": "c", "day": "2026-10-03", "n": None}]
    spec["target"]["mode"] = "overwrite_partitions"
    spec["sql"] = "SELECT 9 AS id, 'z' AS v, '2026-10-01' AS day, 1 AS n"
    run_pipeline(spec, duck=duck)
    assert [r["id"] for r in iceberg_rows(ice)[1]] == [2, 3, 9]  # 2026-10-01 replaced, the others kept
    spec["target"].update(mode="append", schema="strict")
    spec["sql"] = "SELECT 10 AS id, 'q' AS v, '2026-10-05' AS day"
    with pytest.raises(PipelineError, match="\"schema\": \"strict\""):
        run_pipeline(spec, duck=duck)


def test_an_iceberg_catalog_keeps_only_iceberg_tables(ice):
    spec = {"pipeline": "i", "catalogs": {"ice": ice}, "sql": "SELECT 1 AS id",
            "target": {"catalog": "ice", "table": "gold.t", "format": "parquet"}}
    with pytest.raises(PipelineError, match="keeps Iceberg tables only"):
        run_pipeline(spec, duck=DuckAPI())


def test_iceberg_in_glue_goes_through_pyiceberg(glue, tmp_path):
    pytest.importorskip("pyiceberg")
    lake = GlueCatalog("lake", region="us-east-1", warehouse=f"file://{tmp_path / 'gwh'}", client=glue)
    (tmp_path / "gwh").mkdir()
    spec = pipeline("SELECT 1 AS id, 'a' AS v", format="iceberg", mode="append",
                    path=f"file://{tmp_path / 'gwh' / 'things'}")
    assert run_pipeline(spec, duck=DuckAPI(), catalogs={"lake": lake}).writes[0]["created"]
    t = glue.get_table(DatabaseName="silver", Name="things")["Table"]
    assert t["Parameters"]["table_type"].upper() == "ICEBERG" and "metadata_location" in t["Parameters"]
    assert lake.table("silver", "things").format == "iceberg"


# -- Unity Catalog (Azure) ---------------------------------------------------------------------------------------------


class FakeUnity:
    """The Unity Catalog REST API's tables and schemas, in memory."""

    def __init__(self):
        self.tables, self.schemas, self.calls = {}, {"main.default"}, []

    def request(self, method, url, json=None, headers=None, timeout=None):
        path = url.split("/api/2.1/unity-catalog")[1]
        self.calls.append((method, path, json, headers))
        ok = lambda body=None: Resp(200, body or {})  # noqa: E731
        if method == "GET" and path.startswith("/schemas/"):
            return ok() if path[9:] in self.schemas else Resp(404)
        if method == "POST" and path == "/schemas":
            self.schemas.add(f"{json['catalog_name']}.{json['name']}")
            return ok(json)
        if method == "GET" and path.startswith("/tables/"):
            return ok(self.tables[path[8:]]) if path[8:] in self.tables else Resp(404)
        if method == "POST" and path == "/tables":
            self.tables[f"{json['catalog_name']}.{json['schema_name']}.{json['name']}"] = json
            return ok(json)
        if method == "DELETE" and path.startswith("/tables/"):
            self.tables.pop(path[8:])
            return ok()
        return Resp(400)


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    @property
    def content(self):
        return b"{}" if self._body is not None else b""

    @property
    def text(self):
        return str(self._body)

    def json(self):
        return self._body


def test_unity_catalog_external_tables(tmp_path):
    api = FakeUnity()
    unity = UnityCatalog("uc", host="https://adb-1.azuredatabricks.net", catalog_name="main",
                         warehouse=str(tmp_path / "abfss"), token="dapi-secret", session=api)
    duck = DuckAPI()
    spec = {"pipeline": "u", "primary_key": "id", "sql": "SELECT 1 AS id, [1, 2] AS tags, 'SP' AS region",
            "audit_columns": False,
            "target": {"catalog": "uc", "table": "silver.things", "mode": "append", "partition_by": "region"}}
    assert run_pipeline(spec, duck=duck, catalogs={"uc": unity}).writes[0]["created"]
    body = api.tables["main.silver.things"]
    assert (body["table_type"], body["data_source_format"]) == ("EXTERNAL", "PARQUET")
    assert body["storage_location"] == str(tmp_path / "abfss" / "silver" / "things")
    cols = {c["name"]: c for c in body["columns"]}
    assert cols["id"]["type_name"] == "INT" and cols["tags"]["type_text"] == "array<int>"
    assert json.loads(cols["tags"]["type_json"])["type"] == {"type": "array", "elementType": "integer",
                                                             "containsNull": True}
    assert cols["region"]["partition_index"] == 0 and "partition_index" not in cols["id"]
    assert ("POST", "/schemas", {"name": "silver", "catalog_name": "main"}, {"Authorization": "Bearer dapi-secret"}) \
        in api.calls
    assert unity.table("silver", "things").partition_keys[0].name == "region"

    spec["sql"] = "SELECT 2 AS id, [3] AS tags, 'RJ' AS region, 'x' AS note"
    with pytest.raises(PipelineError, match="recreate_on_schema_change"):
        run_pipeline(spec, duck=duck, catalogs={"uc": unity})
    unity.recreate_on_schema_change = True
    assert run_pipeline(spec, duck=duck, catalogs={"uc": unity}).writes[0]["columns_added"] == ["note"]
    assert [c["name"] for c in api.tables["main.silver.things"]["columns"]] == ["id", "tags", "note", "region"]
    assert [m for m, *_ in api.calls].count("DELETE") == 1


def test_unity_delta_tables_take_new_columns_in_the_log(tmp_path):
    pytest.importorskip("deltalake")
    api = FakeUnity()
    unity = UnityCatalog("uc", host="https://h", warehouse=str(tmp_path / "w"), token="t", session=api)
    spec = {"pipeline": "u", "primary_key": "id", "sql": "SELECT 1 AS id",
            "target": {"catalog": "uc", "table": "main.gold.t", "format": "delta", "mode": "merge"}}
    run_pipeline(spec, duck=DuckAPI(), catalogs={"uc": unity})
    spec["sql"] = "SELECT 1 AS id, 'new' AS label"
    run_pipeline(spec, duck=DuckAPI(), catalogs={"uc": unity})
    assert "DELETE" not in [m for m, *_ in api.calls] and api.tables["main.gold.t"]["data_source_format"] == "DELTA"


# -- where catalogs come from -------------------------------------------------------------------------------------------


def test_catalogs_from_duckduck_json_and_unknown_names(tmp_path, monkeypatch):
    config = tmp_path / "duckduck.json"
    config.write_text(json.dumps({"services": {}, "catalogs": {"uc": {
        "type": "unity", "host": "https://h", "authentication": {"type": "local", "token": "from-config"}}}}))
    duck = DuckAPI()
    duck._config_path = str(config)
    built = {}

    def fake(name, block, duck=None):
        built[name] = make_catalog(name, block, duck=duck)
        built[name]._session = FakeUnity()
        built[name].warehouse = str(tmp_path / "w")
        return built[name]

    monkeypatch.setattr("duckduck.pipeline.catalogs.make_catalog", fake)
    spec = {"pipeline": "c", "sql": "SELECT 1 AS id", "target": {"catalog": "uc", "table": "s.t"}}
    run_pipeline(spec, duck=duck)
    assert built["uc"]._token == "from-config"
    spec["target"]["catalog"] = "nope"
    with pytest.raises(PipelineError, match="no catalog 'nope'.*known: uc"):
        run_pipeline(spec, duck=duck)


def test_validate_config_knows_catalogs():
    pytest.importorskip("pydantic")
    from duckduck.semantic.admin import validate_config

    ok = validate_config({"services": {}, "catalogs": {"lake": {"type": "glue"}}})
    assert not ok["errors"] and not any("catalogs" in w for w in ok["warnings"])
    assert validate_config({"services": {}, "catalogs": {"lake": {"type": "hive"}}})["errors"]


# -- Spark writes the files, the catalog API registers them --------------------------------------------------------------


@pytest.fixture(scope="module")
def spark():
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    try:
        session = (SparkSession.builder.master("local[1]").appName("duckduck-catalog-tests")
                   .config("spark.ui.enabled", "false").config("spark.ui.showConsoleProgress", "false")
                   .config("spark.sql.shuffle.partitions", "2").getOrCreate())
    except Exception as exc:  # no Java, …
        pytest.skip(f"no local Spark: {exc}")
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


def test_spark_and_duckdb_keep_the_same_glue_table(glue, lake, spark, tmp_path):
    spec = pipeline("SELECT * FROM (VALUES (1, 'a', '2026-10-01'), (2, 'b', '2026-10-02')) t(id, v, day)",
                    mode="append", partition_by="day")
    run_pipeline({**spec, "engine": "spark"}, duck=DuckAPI(), spark=spark, catalogs={"lake": lake})
    assert partitions(glue) == ["2026-10-01", "2026-10-02"]
    one = glue.get_partition(DatabaseName="silver", TableName="things", PartitionValues=["2026-10-02"])["Partition"]
    assert any(f.endswith(".parquet") for f in os.listdir(one["StorageDescriptor"]["Location"]))
    spec["sql"] = "SELECT 3 AS id, 'c' AS v, '2026-10-03' AS day, 1.5::DOUBLE AS score"
    run = run_pipeline(spec, duck=DuckAPI(), catalogs={"lake": lake})  # the next run on DuckDB, same table
    assert run.writes[0]["columns_added"] == ["score"]
    assert partitions(glue) == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert spark.read.parquet(str(tmp_path / "wh" / "silver" / "things")).count() == 3


# -- schema evolution on / off -----------------------------------------------------------------------------------------


def test_schema_evolution_is_on_by_default_and_can_be_turned_off():
    base = {"pipeline": "x", "sql": "SELECT 1", "targets": {"a": {"catalog": "l", "table": "s.a"},
                                                             "b": {"catalog": "l", "table": "s.b",
                                                                   "schema_evolution": True}}}
    assert [t.schema for t in load_spec(base).targets] == ["evolve", "evolve"]
    off = load_spec({**base, "schema_evolution": False})
    assert [t.schema for t in off.targets] == ["fixed", "evolve"]  # a target's own setting wins
    with pytest.raises(PipelineError, match="keep one"):
        load_spec({"pipeline": "x", "sql": "SELECT 1", "target": {
            "catalog": "l", "table": "s.a", "schema_evolution": False, "schema": "evolve"}})
    with pytest.raises(PipelineError, match="true \\(default"):
        load_spec({**base, "schema_evolution": "no"})


def test_with_schema_evolution_off_a_new_column_fails_and_the_rest_still_fits(glue, lake):
    duck = DuckAPI()
    spec = {**pipeline("SELECT 1 AS id, 'a' AS v"), "schema_evolution": False}
    run_pipeline(spec, duck=duck, catalogs={"lake": lake})  # a new table takes the view's columns
    spec["sql"] = "SELECT 2 AS id, 'b' AS v, 3 AS extra"
    with pytest.raises(PipelineError, match="extra \\(int\\) is new — schema evolution is off"):
        run_pipeline(spec, duck=duck, catalogs={"lake": lake})
    spec["sql"] = "SELECT CAST(3 AS TINYINT) AS id"  # narrower, and v missing: the table doesn't change
    run_pipeline(spec, duck=duck, catalogs={"lake": lake})
    cols = glue.get_table(DatabaseName="silver", Name="things")["Table"]["StorageDescriptor"]["Columns"]
    assert [(c["Name"], c["Type"]) for c in cols] == [("id", "int"), ("v", "string")]


def test_iceberg_with_schema_evolution_off(ice):
    duck = DuckAPI()
    spec = {"pipeline": "i", "catalogs": {"ice": ice}, "schema_evolution": False, "sql": "SELECT 1 AS id",
            "target": {"catalog": "ice", "table": "gold.t", "format": "iceberg"}}
    run_pipeline(spec, duck=duck)
    spec["sql"] = "SELECT 2 AS id, 'x' AS label"
    with pytest.raises(PipelineError, match="label — new column\\(s\\), and schema evolution is off"):
        run_pipeline(spec, duck=duck)

"""A pipeline reading the lake's own tables (FROM corp.feed / raw.corp.feed), and past ingestion only those."""

import json
import logging

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.pipeline import PipelineError, plan_pipeline, run_pipeline
from duckduck.pipeline.catalogs import Column, TableInfo
from duckduck.pipeline.lakeread import LakeTable
from duckduck.pipeline.settings import lake_problems

pytest.importorskip("pyarrow")


def _setup(tmp_path, name="duckduck.json", **lake):
    rows = {"batch": [pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})]}
    duck = DuckAPI()
    duck.register_api_function("feed", lambda: rows["batch"][-1])
    config = tmp_path / name
    config.write_text(json.dumps({"services": {}, "lake": {
        "layers": {"raw": str(tmp_path / "lake/raw"), "silver": str(tmp_path / "lake/silver"),
                   "gold": str(tmp_path / "lake/gold")},
        "state": str(tmp_path / "lake/_state"), **lake}}))
    return duck, str(config), rows


RAW = {"pipeline": "raw_feed", "primary_key": "id", "layer": "raw", "sql": "SELECT * FROM feed",
       "target": {"database": "corp", "table_name": "feed", "mode": "append", "partition_by": "_load_date"}}
SILVER = {"pipeline": "silver_feed", "primary_key": "id", "layer": "silver", "sql": "SELECT * FROM raw.corp.feed",
          "load": "incremental", "target": {"database": "corp", "table_name": "feed", "mode": "merge"}}


def test_silver_reads_raw_by_its_address_and_only_what_is_new(tmp_path, caplog):
    duck, config, rows = _setup(tmp_path)
    run_pipeline(RAW, duck=duck, config_path=config)
    first = run_pipeline(SILVER, duck=duck, config_path=config)
    assert first.writes[0]["rows"] == 3
    assert "_lake_" not in "".join(duck.functions)  # registered for the run only

    rows["batch"].append(pd.DataFrame({"id": [3, 4], "name": ["c2", "d"]}))
    run_pipeline(RAW, duck=duck, config_path=config, now=pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=5))
    with caplog.at_level(logging.INFO, logger="duckduck"):
        second = run_pipeline(SILVER, duck=duck, config_path=config)
    assert second.steps[0]["rows"] == 2  # only what raw appended since: the WHERE went into the read
    assert any('read ' in r.getMessage() and '"_loaded_at" >' in r.getMessage() for r in caplog.records)
    merged = pd.read_parquet(tmp_path / "lake/silver/corp/feed").sort_values("id")
    assert merged["name"].tolist() == ["a", "b", "c2", "d"]

    # nothing new: an empty read still has the table's columns (the key, the sip, the merge are fine)
    third = run_pipeline(SILVER, duck=duck, config_path=config)
    assert third.steps[0]["rows"] == 0 and not third.warnings


def test_a_name_without_its_layer_is_found_unless_two_layers_have_it(tmp_path):
    duck, config, _ = _setup(tmp_path)
    run_pipeline(RAW, duck=duck, config_path=config)
    gold = {"pipeline": "gold_n", "layer": "gold", "sql": "SELECT count(*) AS n FROM corp.feed",
            "target": {"database": "corp", "table_name": "n", "mode": "overwrite"}}
    assert run_pipeline(gold, duck=duck, config_path=config).writes[0]["rows"] == 1
    run_pipeline(SILVER, duck=duck, config_path=config)  # now silver has corp/feed too
    with pytest.raises(PipelineError, match="write which: raw.corp.feed or silver.corp.feed"):
        run_pipeline(gold, duck=duck, config_path=config)


def test_past_ingestion_a_virtualized_table_must_be_declared(tmp_path):
    duck, config, _ = _setup(tmp_path)
    reads_feed = {**SILVER, "sql": "SELECT * FROM feed", "load": "full"}
    with pytest.raises(PipelineError) as e:
        plan_pipeline(reads_feed, duck=duck, config_path=config)
    assert "silver reads only the lake: feed is not a table of the lake" in str(e.value)
    assert '"virtualized_table": true (or ["feed"])' in str(e.value)
    assert plan_pipeline({**reads_feed, "virtualized_table": True}, duck=duck, config_path=config)
    assert plan_pipeline({**reads_feed, "virtualized_table": ["feed"]}, duck=duck, config_path=config)
    assert plan_pipeline(RAW, duck=duck, config_path=config)  # raw reads the sources
    # another ingestion layer, by name
    _, landing, _ = _setup(tmp_path, "landing.json", ingestion_layers=["raw", "silver"])
    assert plan_pipeline(reads_feed, duck=duck, config_path=landing)


class _Glue:
    """A catalog that knows one table (enough to read it), Glue-flavoured for Athena."""
    kind, name = "glue", "glue"

    def __init__(self, location):
        self.info = TableInfo("corp", "feed", location, "parquet", columns=[Column("id", "bigint")],
                              partition_keys=[Column("_load_date", "date")])

    def table(self, database, name):
        return self.info if (database, name) == ("corp", "feed") else None


def test_a_catalog_table_by_its_name_read_from_its_files_or_on_athena(tmp_path, monkeypatch):
    duck, config, _ = _setup(tmp_path)
    run_pipeline(RAW, duck=duck, config_path=config)
    _, with_catalog, _ = _setup(tmp_path, "glue.json", catalog="glue")
    catalog = _Glue(str(tmp_path / "lake/raw/corp/feed"))
    gold = {"pipeline": "gold_ids", "layer": "gold", "sql": "SELECT id, _load_date FROM corp.feed WHERE id > 1",
            "target": {"path": str(tmp_path / "out"), "mode": "overwrite"}}
    run = run_pipeline({**gold, "layer": None}, duck=duck, config_path=with_catalog, catalogs={"glue": catalog})
    assert run.writes[0]["rows"] == 2
    typed = pd.read_parquet(tmp_path / "out")
    assert str(typed["_load_date"].dtype).startswith(("datetime", "object"))  # the catalog's date, not text

    asked = []

    class FakeAthena:
        def table(self, database, table_name, where=None, limit=None):
            asked.append((database, table_name, [(c.column, c.op, c.value) for c in where or []]))
            return pd.DataFrame({"id": [2, 3], "_load_date": ["2026-10-02"] * 2})

        def iter_table(self, database, table_name, where=None):
            yield self.table(database, table_name, where)

    monkeypatch.setattr("duckduck.pipeline.lakeread._athena_of", lambda lake, account: FakeAthena())
    run = run_pipeline({**gold, "layer": None, "read_engine": "athena"}, duck=duck, config_path=with_catalog,
                       catalogs={"glue": catalog})
    assert run.writes[0]["rows"] == 2 and asked[0][:2] == ("corp", "feed")
    assert ("id", "gt", 1) in asked[0][2]  # the WHERE ran on Athena

    with pytest.raises(PipelineError, match="is at .*/raw/corp/feed, not in the silver layer"):
        run_pipeline({**gold, "layer": None, "sql": "SELECT * FROM silver.corp.feed"}, duck=duck,
                     config_path=with_catalog, catalogs={"glue": catalog})
    with pytest.raises(PipelineError, match="athena\" reads tables of a Glue catalog"):
        run_pipeline({**gold, "layer": None, "read_engine": "athena"}, duck=duck, config_path=config)


def test_spark_reads_it_by_its_catalog_name_else_its_files():
    info = TableInfo("corp", "feed", "s3://lake/raw/corp/feed", "parquet")
    assert LakeTable(info, "duckdb", catalog=object())._spark_source().table == "corp.feed"
    source = LakeTable(info, "duckdb")._spark_source()
    assert source.table is None and source.path == "s3://lake/raw/corp/feed" and source.format == "parquet"


def test_the_settings_are_checked():
    assert lake_problems({"layers": {"raw": "r"}, "read_engine": "pandas"})[0].startswith(
        "lake.read_engine is one of ['duckdb', 'athena'] (pandas: use \"duckdb\"")
    assert lake_problems({"layers": {"raw": "r"}, "ingestion_layers": ["landing"]}) == [
        "lake.ingestion_layers: ['landing'] isn't in lake.layers"]
    assert lake_problems({"athena": {"workgroup": "w", "bucket": "x"}})[0].startswith("lake.athena: unknown key(s)")
    with pytest.raises(PipelineError, match="'virtualized_table' is true"):
        plan_pipeline({**SILVER, "virtualized_table": 3})


def test_a_notebook_reads_the_lake_too(tmp_path):
    from duckduck.pipeline import notebook

    duck, config, _ = _setup(tmp_path)
    run_pipeline(RAW, duck=duck, config_path=config)
    nb = notebook(duck=duck, config_path=config)
    assert nb.run_cell("SELECT count(*) AS n FROM raw.corp.feed")["n"].tolist() == [3]

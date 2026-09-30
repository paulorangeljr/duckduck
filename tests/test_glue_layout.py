"""Finding a Glue table's files the way Athena does (``duckduck.s3layout`` + ``GlueTable._parquet_read``):
the partitions the WHERE selects — from ``get_partitions`` or a partition projection —, only their locations
listed, hidden files skipped, extensionless files read. The scans run on a real DuckDB over local files."""

from datetime import datetime
from unittest.mock import MagicMock

import duckdb
import pandas as pd
import pytest

import duckduck.glue as glue_module
from duckduck import DuckAPI, GlueTable
from duckduck import s3layout
from duckduck.pushdown import Condition as C


def _glue(monkeypatch, table, partitions=(), listing=None):
    """A GlueTable with a fake Glue/S3 client and a real DuckDB; ``listing`` maps a location to its files."""
    fake_boto3, client = MagicMock(), MagicMock()
    fake_boto3.Session.return_value.client.return_value = client
    monkeypatch.setattr(glue_module, "boto3", fake_boto3)
    monkeypatch.setattr(glue_module.LakehouseConnection, "create_secret", lambda self, sql: None)
    client.get_table.return_value = {"Table": table}
    client.get_paginator.return_value.paginate.return_value = [{"Partitions": list(partitions)}]
    gt = GlueTable()
    listed = []

    def list_locations(locations):
        listed.append(list(locations))
        return {loc: listing.get(loc, []) for loc in locations}

    gt._list = list_locations
    return gt, client, listed


def _write(path, df):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.register("df", df)
    con.execute(f"COPY df TO '{path}' (FORMAT parquet)")  # no extension, like Athena's output
    return str(path)


def test_only_the_partitions_the_where_selects_are_listed_and_read(tmp_path, monkeypatch):
    root = tmp_path / "events"
    files = {d: _write(root / f"dt={d}" / f"20260930_000_{d}", pd.DataFrame({"user": [f"u{d[-1]}"], "n": [1]}))
             for d in ("2026-09-29", "2026-09-30")}
    table = {"StorageDescriptor": {"Location": f"{root}/", "Columns": [{"Name": "user"}, {"Name": "n"}]},
             "PartitionKeys": [{"Name": "dt", "Type": "string"}], "Parameters": {}}
    partitions = [{"Values": ["2026-09-30"], "StorageDescriptor": {"Location": f"{root}/dt=2026-09-30"}}]
    gt, client, listed = _glue(monkeypatch, table, partitions, {f"{root}/dt=2026-09-30": [files["2026-09-30"]]})
    df = gt.table("db", "events", where=[C("dt", "eq", "2026-09-30"), C("user", "eq", "u0")])
    assert [(r["user"], r["n"], str(r["dt"])[:10]) for r in df.to_dict("records")] == [("u0", 1, "2026-09-30")]
    kwargs = client.get_paginator.return_value.paginate.call_args.kwargs
    assert kwargs["Expression"] == "dt = '2026-09-30'" and kwargs["ExcludeColumnSchema"] is True
    assert listed == [[f"{root}/dt=2026-09-30"]]  # not the whole table


def test_partitions_outside_the_table_location_get_their_values_from_glue(tmp_path, monkeypatch):
    a = _write(tmp_path / "moved" / "part-a", pd.DataFrame({"user": ["ana"]}))
    table = {"StorageDescriptor": {"Location": f"{tmp_path}/t/"}, "PartitionKeys": [{"Name": "year", "Type": "int"}],
             "Parameters": {}}
    partitions = [{"Values": ["2025"], "StorageDescriptor": {"Location": f"{tmp_path}/moved"}}]
    gt, client, _ = _glue(monkeypatch, table, partitions, {f"{tmp_path}/moved": [a]})
    df = gt.table("db", "t", where=[C("year", "gte", 2025)])
    assert df.to_dict("records") == [{"user": "ana", "year": 2025}]
    assert client.get_paginator.return_value.paginate.call_args.kwargs["Expression"] == "year >= 2025"


def test_no_condition_on_a_partition_key_lists_the_whole_location(tmp_path, monkeypatch):
    f = _write(tmp_path / "t" / "dt=1" / "x", pd.DataFrame({"a": [1]}))
    table = {"StorageDescriptor": {"Location": f"{tmp_path}/t/"}, "PartitionKeys": [{"Name": "dt", "Type": "int"}],
             "Parameters": {}}
    gt, client, listed = _glue(monkeypatch, table, listing={f"{tmp_path}/t/": [f]})
    assert gt.table("db", "t", where=[C("a", "eq", 1)])["dt"].tolist() == [1]
    assert listed == [[f"{tmp_path}/t/"]] and not client.get_paginator.called


def test_nothing_selected_is_an_empty_table_with_its_columns(tmp_path, monkeypatch):
    table = {"StorageDescriptor": {"Location": f"{tmp_path}/t/", "Columns": [{"Name": "user"}]},
             "PartitionKeys": [{"Name": "dt", "Type": "string"}], "Parameters": {}}
    gt, _, _ = _glue(monkeypatch, table, partitions=[])
    df = gt.table("db", "t", where=[C("dt", "eq", "2030-01-01")])
    assert list(df.columns) == ["user", "dt"] and df.empty


def test_a_projected_table_needs_no_glue_call(tmp_path, monkeypatch):
    f = _write(tmp_path / "logs" / "eu" / "2026" / "09" / "30" / "data", pd.DataFrame({"msg": ["hi"]}))
    table = {"StorageDescriptor": {"Location": f"{tmp_path}/logs/"},
             "PartitionKeys": [{"Name": "region", "Type": "string"}, {"Name": "day", "Type": "string"}],
             "Parameters": {"projection.enabled": "true", "projection.region.type": "enum",
                            "projection.region.values": "us,eu", "projection.day.type": "date",
                            "projection.day.format": "yyyy/MM/dd", "projection.day.range": "2020/01/01,NOW",
                            "storage.location.template": f"{tmp_path}/logs/${{region}}/${{day}}"}}
    gt, client, listed = _glue(monkeypatch, table, listing={f"{tmp_path}/logs/eu/2026/09/30/": [f]})
    df = gt.table("db", "logs", where=[C("region", "eq", "eu"), C("day", "eq", "2026/09/30")])
    assert df.to_dict("records") == [{"msg": "hi", "region": "eu", "day": "2026/09/30"}]
    assert listed == [[f"{tmp_path}/logs/eu/2026/09/30/"]] and not client.get_paginator.called


def test_listing_skips_hidden_files_and_keeps_extensionless_ones(monkeypatch):
    table = {"StorageDescriptor": {"Location": "s3://b/t/"}, "Parameters": {}}
    fake_boto3, client = MagicMock(), MagicMock()
    fake_boto3.Session.return_value.client.return_value = client
    monkeypatch.setattr(glue_module, "boto3", fake_boto3)
    monkeypatch.setattr(glue_module.LakehouseConnection, "create_secret", lambda self, sql: None)
    client.get_paginator.return_value.paginate.return_value = [{"Contents": [
        {"Key": "t/20260930_000_abc", "Size": 10}, {"Key": "t/_SUCCESS", "Size": 0},
        {"Key": "t/dt=1/part-0.parquet", "Size": 5}, {"Key": "t/.hive-staging/x", "Size": 5},
        {"Key": "t/_temporary/0/y", "Size": 5}, {"Key": "t/dt=1_$folder$", "Size": 0}]}]
    gt = GlueTable(listing_ttl=60)
    assert gt._list(["s3://b/t/"])["s3://b/t/"] == ["s3://b/t/20260930_000_abc", "s3://b/t/dt=1/part-0.parquet"]
    gt._list(["s3://b/t/"])
    assert client.get_paginator.return_value.paginate.call_count == 1  # kept for listing_ttl


def test_glue_expressions_and_projection_math():
    keys = {"dt": "string", "year": "int"}
    assert s3layout.glue_expression([C("dt", "in", ("a", "b'c")), C("year", "lt", "2020"), C("year", "eq", "x")],
                                    keys) == "dt IN ('a', 'b''c') AND year < 2020"
    table = {"Parameters": {"projection.enabled": "true", "projection.h.type": "date",
                            "projection.h.format": "yyyy-MM-dd-HH", "projection.h.range": "2015-01-01-00,NOW",
                            "projection.h.interval.unit": "HOURS", "projection.n.type": "integer",
                            "projection.n.range": "0,99", "projection.n.digits": "2"},
             "PartitionKeys": [{"Name": "h"}, {"Name": "n"}], "StorageDescriptor": {"Location": "s3://b/t"}}
    parts = s3layout.projected_partitions(table, [C("h", "gte", "2026-09-30-22"), C("n", "in", (3, 7))],
                                          now=datetime(2026, 9, 30, 23, 30))
    assert [p[0] for p in parts] == ["s3://b/t/h=2026-09-30-22/n=03/", "s3://b/t/h=2026-09-30-22/n=07/",
                                     "s3://b/t/h=2026-09-30-23/n=03/", "s3://b/t/h=2026-09-30-23/n=07/"]
    # an injected key without a value can't be projected: the whole location instead
    injected = {"Parameters": {"projection.enabled": "true", "projection.acct.type": "injected"},
                "PartitionKeys": [{"Name": "acct"}], "StorageDescriptor": {"Location": "s3://b/t"}}
    assert s3layout.projected_partitions(injected, [C("acct", "gt", "1")]) is None

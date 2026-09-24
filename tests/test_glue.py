"""Unit tests for the GlueTable connector (S3 + AWS Glue Data Catalog)."""

from unittest.mock import MagicMock

import pandas as pd
import pytest

import duckduck.glue as glue_module
import duckduck.lakehouse as lakehouse_module
from duckduck import GlueTable


def _make_glue(monkeypatch, aws_access_key_id=None, aws_secret_access_key=None, **kwargs):
    fake_duck_conn = MagicMock()
    monkeypatch.setattr(lakehouse_module.duckdb, "connect", MagicMock(return_value=fake_duck_conn))

    fake_boto3 = MagicMock()
    fake_glue_client = MagicMock()
    fake_boto3.Session.return_value.client.return_value = fake_glue_client
    monkeypatch.setattr(glue_module, "boto3", fake_boto3)

    gt = GlueTable(
        aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key, **kwargs
    )
    return gt, fake_boto3, fake_glue_client, fake_duck_conn


# ---------------------------------------------------------------------------
# Construction / secret setup
# ---------------------------------------------------------------------------


def test_without_boto3_raises_import_error(monkeypatch):
    monkeypatch.setattr(glue_module, "boto3", None)
    with pytest.raises(ImportError, match="boto3"):
        GlueTable()


def test_init_builds_glue_client_via_session(monkeypatch):
    gt, fake_boto3, fake_glue_client, _ = _make_glue(
        monkeypatch, region_name="us-east-1", profile_name="prod"
    )

    fake_boto3.Session.assert_called_once_with(
        profile_name="prod", region_name="us-east-1",
        aws_access_key_id=None, aws_secret_access_key=None,
    )
    fake_boto3.Session.return_value.client.assert_called_once_with("glue")
    assert gt._glue is fake_glue_client


def test_credential_chain_secret_with_profile_and_region(monkeypatch):
    _, _, _, fake_duck_conn = _make_glue(monkeypatch, region_name="us-east-1", profile_name="prod")

    executed = [call.args[0] for call in fake_duck_conn.execute.call_args_list]
    secret_calls = [sql for sql in executed if "CREATE OR REPLACE SECRET" in sql]
    assert len(secret_calls) == 1
    assert "PROVIDER credential_chain" in secret_calls[0]
    assert "PROFILE 'prod'" in secret_calls[0]
    assert "REGION 'us-east-1'" in secret_calls[0]


def test_explicit_keys_secret(monkeypatch):
    _, _, _, fake_duck_conn = _make_glue(
        monkeypatch,
        aws_access_key_id="AKIA123",
        aws_secret_access_key="shh",
        region_name="us-east-1",
    )

    executed = [call.args[0] for call in fake_duck_conn.execute.call_args_list]
    secret_calls = [sql for sql in executed if "CREATE OR REPLACE SECRET" in sql]
    assert len(secret_calls) == 1
    assert "KEY_ID 'AKIA123'" in secret_calls[0]
    assert "SECRET 'shh'" in secret_calls[0]
    assert "REGION 'us-east-1'" in secret_calls[0]


def test_from_secret_builds_instance(monkeypatch):
    fake_duck_conn = MagicMock()
    monkeypatch.setattr(lakehouse_module.duckdb, "connect", MagicMock(return_value=fake_duck_conn))
    fake_boto3 = MagicMock()
    monkeypatch.setattr(glue_module, "boto3", fake_boto3)

    GlueTable.from_secret({"region_name": "us-east-1", "profile_name": "prod"})

    fake_boto3.Session.assert_called_once_with(
        profile_name="prod", region_name="us-east-1",
        aws_access_key_id=None, aws_secret_access_key=None,
    )


# ---------------------------------------------------------------------------
# Format auto-detection
# ---------------------------------------------------------------------------


def test_scan_expression_defaults_to_parquet(monkeypatch):
    gt, _, fake_glue_client, _ = _make_glue(monkeypatch)
    fake_glue_client.get_table.return_value = {
        "Table": {"StorageDescriptor": {"Location": "s3://bucket/events"}, "Parameters": {}}
    }

    expr = gt._scan_expression("analytics", "events")

    assert expr == "read_parquet('s3://bucket/events/**/*.parquet', hive_partitioning=true, union_by_name=true)"


def test_scan_expression_detects_delta_via_table_type(monkeypatch):
    gt, _, fake_glue_client, fake_duck_conn = _make_glue(monkeypatch)
    fake_glue_client.get_table.return_value = {
        "Table": {
            "StorageDescriptor": {"Location": "s3://bucket/orders"},
            "Parameters": {"table_type": "DELTA"},
        }
    }

    expr = gt._scan_expression("analytics", "orders")

    assert expr == "delta_scan('s3://bucket/orders')"
    fake_duck_conn.execute.assert_any_call("INSTALL delta")


def test_scan_expression_detects_delta_via_spark_provider(monkeypatch):
    gt, _, fake_glue_client, _ = _make_glue(monkeypatch)
    fake_glue_client.get_table.return_value = {
        "Table": {
            "StorageDescriptor": {"Location": "s3://bucket/orders"},
            "Parameters": {"spark.sql.sources.provider": "delta"},
        }
    }

    assert gt._scan_expression("analytics", "orders") == "delta_scan('s3://bucket/orders')"


def test_scan_expression_detects_iceberg_with_metadata_location(monkeypatch):
    gt, _, fake_glue_client, fake_duck_conn = _make_glue(monkeypatch)
    fake_glue_client.get_table.return_value = {
        "Table": {
            "StorageDescriptor": {"Location": "s3://bucket/customers"},
            "Parameters": {
                "table_type": "ICEBERG",
                "metadata_location": "s3://bucket/customers/metadata/00001.metadata.json",
            },
        }
    }

    expr = gt._scan_expression("analytics", "customers")

    assert expr == "iceberg_scan('s3://bucket/customers/metadata/00001.metadata.json')"
    fake_duck_conn.execute.assert_any_call("INSTALL iceberg")


def test_scan_expression_iceberg_without_metadata_location_guesses(monkeypatch):
    gt, _, fake_glue_client, _ = _make_glue(monkeypatch)
    fake_glue_client.get_table.return_value = {
        "Table": {
            "StorageDescriptor": {"Location": "s3://bucket/customers"},
            "Parameters": {"table_type": "ICEBERG"},
        }
    }

    expr = gt._scan_expression("analytics", "customers")

    assert expr == "iceberg_scan('s3://bucket/customers', allow_moved_paths => true)"


def test_scan_expression_missing_location_raises(monkeypatch):
    gt, _, fake_glue_client, _ = _make_glue(monkeypatch)
    fake_glue_client.get_table.return_value = {"Table": {"StorageDescriptor": {}, "Parameters": {}}}

    with pytest.raises(ValueError, match="StorageDescriptor.Location"):
        gt._scan_expression("analytics", "broken")


def test_describe_is_cached(monkeypatch):
    gt, _, fake_glue_client, _ = _make_glue(monkeypatch)
    fake_glue_client.get_table.return_value = {
        "Table": {"StorageDescriptor": {"Location": "s3://bucket/events"}, "Parameters": {}}
    }

    gt._describe("analytics", "events")
    gt._describe("analytics", "events")

    fake_glue_client.get_table.assert_called_once_with(DatabaseName="analytics", Name="events")


# ---------------------------------------------------------------------------
# table() / path()
# ---------------------------------------------------------------------------


def test_table_scans_via_lakehouse(monkeypatch):
    gt, _, fake_glue_client, fake_duck_conn = _make_glue(monkeypatch)
    fake_glue_client.get_table.return_value = {
        "Table": {"StorageDescriptor": {"Location": "s3://bucket/events"}, "Parameters": {}}
    }
    fake_duck_conn.sql.return_value.df.return_value = pd.DataFrame([{"id": 1}])

    df = gt.table("analytics", "events", limit=50)

    fake_duck_conn.sql.assert_called_once_with(
        "SELECT * FROM read_parquet('s3://bucket/events/**/*.parquet', "
        "hive_partitioning=true, union_by_name=true) LIMIT 50"
    )
    assert list(df["id"]) == [1]


def test_path_reads_parquet_directly(monkeypatch):
    gt, _, _, fake_duck_conn = _make_glue(monkeypatch)
    fake_duck_conn.sql.return_value.df.return_value = pd.DataFrame([{"id": 1}])

    gt.path("s3://bucket/adhoc/")

    fake_duck_conn.sql.assert_called_once_with(
        "SELECT * FROM read_parquet('s3://bucket/adhoc/**/*.parquet', "
        "hive_partitioning=true, union_by_name=true)"
    )


def test_path_reads_delta(monkeypatch):
    gt, _, _, fake_duck_conn = _make_glue(monkeypatch)
    fake_duck_conn.sql.return_value.df.return_value = pd.DataFrame([{"id": 1}])

    gt.path("s3://bucket/delta_table", format="delta")

    fake_duck_conn.sql.assert_called_once_with("SELECT * FROM delta_scan('s3://bucket/delta_table')")


def test_path_invalid_format_raises(monkeypatch):
    gt, _, _, _ = _make_glue(monkeypatch)
    with pytest.raises(ValueError, match="Unsupported format"):
        gt.path("s3://bucket/x", format="csv")


def test_iter_table_chunks_result(monkeypatch):
    gt, _, fake_glue_client, fake_duck_conn = _make_glue(monkeypatch)
    fake_glue_client.get_table.return_value = {
        "Table": {"StorageDescriptor": {"Location": "s3://bucket/events"}, "Parameters": {}}
    }
    fake_duck_conn.sql.return_value.df.return_value = pd.DataFrame([{"id": i} for i in range(5)])

    chunks = list(gt.iter_table("analytics", "events", chunksize=2))

    assert [len(c) for c in chunks] == [2, 2, 1]

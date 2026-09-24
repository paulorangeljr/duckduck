"""Unit tests for the Azure Data Explorer (Kusto) connector — Kusto client faked."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

import duckduck.adx as adx_module
from duckduck import DataExplorer, DuckAPI
from duckduck.adx import kql_string, like_to_regex, normalize_cluster
from duckduck.pushdown import Condition

SCHEMA = pd.DataFrame({
    "ColumnName": ["Timestamp", "UserName", "Url", "Bytes", "Blocked"],
    "ColumnType": ["datetime", "string", "string", "long", "bool"],
})
ROWS = pd.DataFrame({
    "Timestamp": pd.to_datetime(["2026-09-24 10:00", "2026-09-24 11:00", "2026-09-20 11:00"]),
    "UserName": ["alice", "bob", "carol"],
    "Url": ["https://github.com/x", "https://GITHUB.com/y", "https://github.com/z"],
    "Bytes": [100, 2000, 50],
    "Blocked": [False, True, False],
})


class FakeKusto:
    """Records every query; answers getschema with SCHEMA and anything else with ``rows``."""

    def __init__(self, rows=ROWS, mgmt=None):
        self.rows, self.mgmt = rows, mgmt or {}
        self.queries, self.commands = [], []

    def execute_query(self, database, query, properties=None):
        self.queries.append((database, query, properties))
        df = SCHEMA if query.endswith("| getschema") else self.rows
        return SimpleNamespace(primary_results=[df])

    def execute_mgmt(self, database, command, properties=None):
        self.commands.append(command)
        result = self.mgmt[command]
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(primary_results=[result])

    def execute(self, *a, **k):  # must never be used: it would run control commands
        raise AssertionError("execute() used instead of execute_query()")


@pytest.fixture(autouse=True)
def passthrough_results(monkeypatch):
    monkeypatch.setattr(adx_module, "dataframe_from_result_table", lambda table: table.copy())


def _adx(**kw):
    fake = FakeKusto(**kw)
    return DataExplorer("mycluster.westeurope", "SecurityLogs", client=fake), fake


def _last_kql(fake):
    return next(q for _, q, _ in reversed(fake.queries) if not q.endswith("getschema"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given, url", [
    ("mycluster.westeurope", "https://mycluster.westeurope.kusto.windows.net"),
    ("mycluster.westeurope.kusto.windows.net", "https://mycluster.westeurope.kusto.windows.net"),
    ("https://x.kusto.fabric.microsoft.com/", "https://x.kusto.fabric.microsoft.com"),
])
def test_normalize_cluster(given, url):
    assert normalize_cluster(given) == url


def test_kql_string_escapes():
    assert kql_string('a"b\\c\nd') == '"a\\"b\\\\c\\nd"'
    with pytest.raises(ValueError):
        kql_string("bell\x07")


def test_like_to_regex_is_exact():
    assert like_to_regex("web_prod%.com") == "(?s)^web.prod.*\\.com$"


# ---------------------------------------------------------------------------
# push-down translation
# ---------------------------------------------------------------------------


def test_where_is_translated_to_typed_kql():
    adx, fake = _adx()
    adx.table("ProxyLogs", where=[
        Condition("username", "eq", "bob"),                       # lowercased by DuckAPI → UserName
        Condition("timestamp", "gte", "2026-09-23 12:00:00"),     # string → todatetime
        Condition("bytes", "gt", 100),                            # int on long: bare number
        Condition("bytes", "lt", "5000"),                         # string on long → tolong
        Condition("blocked", "eq", True),
        Condition("url", "like", "%github%"),                     # case-sensitive substring
        Condition("url", "ilike", "https://github%"),             # case-insensitive prefix
        Condition("url", "like", "%git_ub%"),                     # '_' → exact regex
        Condition("url", "ilike", "%.com/_"),
        Condition("not_a_column", "eq", "x"),                     # skipped
    ], limit=10)
    assert _last_kql(fake) == "\n".join([
        "['ProxyLogs']",
        "| where ['UserName'] == \"bob\"",
        "    and ['Timestamp'] >= todatetime(\"2026-09-23 12:00:00\")",
        "    and ['Bytes'] > 100",
        "    and ['Bytes'] < tolong(\"5000\")",
        "    and ['Blocked'] == true",
        "    and ['Url'] contains_cs \"github\"",
        "    and ['Url'] startswith \"https://github\"",
        "    and ['Url'] matches regex \"(?s)^.*git.ub.*$\"",
        "    and ['Url'] matches regex \"(?i)(?s)^.*\\\\.com/.$\"",
        "| take 10",
    ])
    assert fake.queries[0][0] == "SecurityLogs"


def test_schema_is_fetched_once_per_table():
    adx, fake = _adx()
    adx.table("ProxyLogs", where=[Condition("url", "eq", "a")])
    adx.table("ProxyLogs", where=[Condition("url", "eq", "b")])
    assert sum(q.endswith("getschema") for _, q, _ in fake.queries) == 1


def test_hostile_values_stay_literals():
    adx, fake = _adx()
    adx.table("ProxyLogs", where=[Condition("username", "eq", 'x" or 1==1 | take 1000000 //')])
    assert '== "x\\" or 1==1 | take 1000000 //"' in _last_kql(fake)


@pytest.mark.parametrize("name", ["T'; .drop table x", "a]b", "", "T\nx"])
def test_bad_table_names_are_rejected(name):
    adx, _ = _adx()
    with pytest.raises(ValueError):
        adx.table(name)


def test_duckapi_pushes_like_and_limit_to_the_cluster():
    adx, fake = _adx()
    duck = DuckAPI()
    duck.register_api_function("adx_table", adx.table)
    df = duck.sql(
        "SELECT UserName FROM adx_table(table_name='ProxyLogs') "
        "WHERE Url LIKE '%github%' AND Bytes >= 100 LIMIT 5"
    ).df()
    kql = _last_kql(fake)
    assert "['Url'] contains_cs \"github\"" in kql and "['Bytes'] >= 100" in kql and kql.endswith("| take 5")
    # the fake ignores filters; DuckDB re-applies them: GITHUB (uppercase) is out
    assert df["UserName"].tolist() == ["alice"]


def test_notruncation_option():
    fake = FakeKusto()
    DataExplorer("c", "db", client=fake, notruncation=True).table("ProxyLogs")
    props = fake.queries[-1][2]
    assert props.get_option("notruncation", None) is True


# ---------------------------------------------------------------------------
# query / tables / columns
# ---------------------------------------------------------------------------


def test_query_is_read_only_and_takes_limit():
    adx, fake = _adx()
    adx.query("ProxyLogs | summarize n=count() by UserName;", limit=3)
    assert fake.queries[-1][1] == "ProxyLogs | summarize n=count() by UserName\n| take 3"
    with pytest.raises(ValueError, match="control commands"):
        adx.query("  .drop table ProxyLogs")


def test_tables_lists_details_with_friendly_names():
    details = pd.DataFrame({
        "TableName": ["ProxyLogs", "AuthLogs"], "DatabaseName": ["SecurityLogs"] * 2,
        "Folder": ["network", "identity"], "DocString": ["Proxy", "Sign-ins"],
        "TotalRowCount": [10, 20], "TotalExtents": [1, 2],
    })
    adx, fake = _adx(mgmt={".show tables details": details})
    df = adx.tables()
    assert list(df.columns) == ["table_name", "database", "folder", "description", "row_count"]
    assert df["table_name"].tolist() == ["AuthLogs", "ProxyLogs"]
    assert adx.tables(folder="network")["table_name"].tolist() == ["ProxyLogs"]


def test_tables_falls_back_without_details_permission():
    basic = pd.DataFrame({"TableName": ["ProxyLogs"], "DatabaseName": ["SecurityLogs"], "Folder": [""], "DocString": [""]})
    adx, fake = _adx(mgmt={".show tables details": PermissionError("forbidden"), ".show tables": basic})
    assert adx.tables()["table_name"].tolist() == ["ProxyLogs"]
    assert fake.commands == [".show tables details", ".show tables"]


def test_columns():
    adx, _ = _adx()
    df = adx.columns("ProxyLogs")
    assert df.iloc[0].to_dict() == {"table_name": "ProxyLogs", "column_name": "Timestamp", "data_type": "datetime"}


def test_tables_is_queryable_through_duckapi():
    details = pd.DataFrame({"TableName": ["ProxyLogs", "AuthLogs"], "Folder": ["network", "identity"]})
    adx, _ = _adx(mgmt={".show tables details": details})
    duck = DuckAPI()
    duck.register_api_function("adx_tables", adx.tables)
    assert duck.sql("SELECT table_name FROM adx_tables WHERE folder = 'identity'").df()["table_name"].tolist() == ["AuthLogs"]


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_sdk(monkeypatch):
    kcsb = MagicMock()
    client_cls = MagicMock()
    monkeypatch.setattr(adx_module, "KustoConnectionStringBuilder", kcsb)
    monkeypatch.setattr(adx_module, "KustoClient", client_cls)
    return kcsb, client_cls


def test_from_secret_app_registration(fake_sdk):
    kcsb, client_cls = fake_sdk
    adx = DataExplorer.from_secret(
        {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        cluster="mycluster.westeurope", database="SecurityLogs",
    )
    kcsb.with_aad_application_key_authentication.assert_called_once_with(
        "https://mycluster.westeurope.kusto.windows.net", "c", "s", "t"
    )
    client_cls.assert_called_once_with(kcsb.with_aad_application_key_authentication.return_value)
    assert adx.database == "SecurityLogs" and adx.base_url.endswith("kusto.windows.net")


def test_from_secret_default_credential(fake_sdk, monkeypatch):
    kcsb, _ = fake_sdk
    cred_cls = MagicMock()
    import azure.identity

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", cred_cls)
    DataExplorer.from_secret({"cluster": "c1.region", "database": "db", "tenant_id": "t"})
    cred_cls.assert_called_once_with(interactive_browser_tenant_id="t", additionally_allowed_tenants=["t"])
    kcsb.with_azure_token_credential.assert_called_once_with(
        "https://c1.region.kusto.windows.net", cred_cls.return_value
    )


def test_app_registration_needs_tenant(fake_sdk):
    with pytest.raises(ValueError, match="tenant_id"):
        DataExplorer.from_secret({"cluster": "c", "database": "d", "client_id": "c", "client_secret": "s"})


def test_missing_cluster_or_database(fake_sdk):
    with pytest.raises(ValueError, match="'cluster' and 'database'"):
        DataExplorer.from_secret({"cluster": "c"})


def test_without_sdk_raises_import_error(monkeypatch):
    monkeypatch.setattr(adx_module, "KustoClient", None)
    with pytest.raises(ImportError, match="duckduck\\[adx\\]"):
        DataExplorer.from_app("c", "d", "t", "i", "s")


def test_auto_register_adx(fake_sdk):
    duck = DuckAPI()
    duck.auto_register({
        "adx": {
            "connector": "adx",
            "cluster": "mycluster.westeurope",
            "database": "SecurityLogs",
            "authentication": {"type": "local", "tenant_id": "t", "client_id": "c", "client_secret": "s"},
        },
    })
    listed = duck.list_tables().set_index("table_name")
    assert {"adx_table", "adx_query", "adx_tables", "adx_columns"} <= set(listed.index)
    assert listed.loc["adx_table", "source"] == "Azure Data Explorer (KQL)"
    assert listed.loc["adx_table", "endpoint"] == "https://mycluster.westeurope.kusto.windows.net"

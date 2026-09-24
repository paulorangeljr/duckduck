"""Unit tests for DuckAPI predicate push-down."""

import pandas as pd
import pytest

from duckduck import DuckAPI, PushDownContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_assets(hostname=None, ip=None, limit=None):
    data = [
        {"id": 1, "hostname": "web-prod-01", "ip": "10.0.0.1", "severity": "critical"},
        {"id": 2, "hostname": "web-prod-02", "ip": "10.0.0.2", "severity": "high"},
        {"id": 3, "hostname": "db-prod-01",  "ip": "10.0.0.3", "severity": "critical"},
        {"id": 4, "hostname": "lb-prod-01",  "ip": "10.0.0.4", "severity": "medium"},
        {"id": 5, "hostname": "web-dev-01",  "ip": "10.0.1.1", "severity": "low"},
    ]
    if hostname:
        data = [r for r in data if hostname in r["hostname"]]
    if ip:
        data = [r for r in data if r["ip"] == ip]
    if limit:
        data = data[:limit]
    return data


def make_vulns(severity=None, limit=None):
    data = [
        {"id": "CVE-2024-001", "title": "RCE via log4j",   "severity": "critical", "cvss": 9.8},
        {"id": "CVE-2024-002", "title": "SQLi in webapp",  "severity": "high",     "cvss": 8.1},
        {"id": "CVE-2024-003", "title": "XSS reflected",   "severity": "medium",   "cvss": 6.1},
        {"id": "CVE-2024-004", "title": "Weak cipher",     "severity": "low",      "cvss": 3.2},
        {"id": "CVE-2024-005", "title": "Heap overflow",   "severity": "critical", "cvss": 9.1},
    ]
    if severity:
        data = [r for r in data if r["severity"] == severity]
    if limit:
        data = data[:limit]
    return data


@pytest.fixture()
def duck():
    d = DuckAPI()
    d.register_api_function("assets", make_assets)
    d.register_api_function("vulns", make_vulns)
    yield d
    d.close()


# ---------------------------------------------------------------------------
# LIMIT push-down
# ---------------------------------------------------------------------------


def test_limit_pushdown(duck):
    calls = []

    def tracked_assets(hostname=None, ip=None, limit=None):
        calls.append({"hostname": hostname, "ip": ip, "limit": limit})
        return make_assets(hostname=hostname, ip=ip, limit=limit)

    duck.register_api_function("assets", tracked_assets)
    df = duck.sql("SELECT * FROM assets LIMIT 3").df()

    assert calls[0]["limit"] == 3
    assert len(df) <= 3


# ---------------------------------------------------------------------------
# Simple WHERE push-down
# ---------------------------------------------------------------------------


def test_where_equality_pushdown(duck):
    calls = []

    def tracked_vulns(severity=None, limit=None):
        calls.append({"severity": severity, "limit": limit})
        return make_vulns(severity=severity, limit=limit)

    duck.register_api_function("vulns", tracked_vulns)
    df = duck.sql("SELECT * FROM vulns WHERE severity = 'critical'").df()

    assert calls[0]["severity"] == "critical"
    assert all(df["severity"] == "critical")


# ---------------------------------------------------------------------------
# Explicit parameter in the call
# ---------------------------------------------------------------------------


def test_explicit_param_in_call(duck):
    calls = []

    def tracked_assets(hostname=None, ip=None, limit=None):
        calls.append({"hostname": hostname, "ip": ip, "limit": limit})
        return make_assets(hostname=hostname, ip=ip, limit=limit)

    duck.register_api_function("assets", tracked_assets)
    duck.sql("SELECT * FROM assets(hostname='web-prod')").df()

    assert calls[0]["hostname"] == "web-prod"


# ---------------------------------------------------------------------------
# Explicit overrides push-down
# ---------------------------------------------------------------------------


def test_explicit_overrides_pushdown(duck):
    calls = []

    def tracked_assets(hostname=None, ip=None, limit=None):
        calls.append({"hostname": hostname, "ip": ip, "limit": limit})
        return make_assets(hostname=hostname, ip=ip, limit=limit)

    duck.register_api_function("assets", tracked_assets)
    # LIMIT 10 in the SQL, but explicit limit=3 in the call → explicit wins
    duck.sql("SELECT * FROM assets(limit=3) LIMIT 10").df()

    assert calls[0]["limit"] == 3


# ---------------------------------------------------------------------------
# Parameter unknown to the function is ignored during push-down
# ---------------------------------------------------------------------------


def test_unknown_where_column_ignored(duck):
    calls = []

    def tracked_assets(hostname=None, ip=None, limit=None):
        calls.append({"hostname": hostname, "ip": ip, "limit": limit})
        return make_assets(hostname=hostname, ip=ip, limit=limit)

    duck.register_api_function("assets", tracked_assets)
    # "severity" is not a parameter of make_assets, but DuckDB filters it afterwards
    df = duck.sql("SELECT * FROM assets WHERE severity = 'critical'").df()

    assert calls[0].get("severity") is None  # didn't reach the API
    assert all(df["severity"] == "critical")  # DuckDB filtered the result


# ---------------------------------------------------------------------------
# Inline kwargs parsing
# ---------------------------------------------------------------------------


def test_parse_kwargs_types(duck):
    d = DuckAPI()

    result = d._parse_kwargs("a=1, b='hello', c=3.14, d=True")
    assert result == {"a": 1, "b": "hello", "c": 3.14, "d": True}

    d.close()


def test_parse_kwargs_empty(duck):
    d = DuckAPI()
    assert d._parse_kwargs("") == {}
    assert d._parse_kwargs("   ") == {}
    d.close()


def test_parse_kwargs_positional_raises(duck):
    d = DuckAPI()
    with pytest.raises(ValueError, match="named parameters"):
        d._parse_kwargs("123")
    d.close()


# ---------------------------------------------------------------------------
# _extract_pushdown
# ---------------------------------------------------------------------------


def test_extract_pushdown_limit():
    d = DuckAPI()
    ctx = d._extract_pushdown("SELECT * FROM t LIMIT 42")
    assert ctx.limit == 42
    d.close()


def test_extract_pushdown_where():
    d = DuckAPI()
    ctx = d._extract_pushdown("SELECT * FROM t WHERE severity = 'critical'")
    assert ctx.filters.get("severity") == "critical"
    d.close()


def test_extract_pushdown_combined():
    d = DuckAPI()
    ctx = d._extract_pushdown(
        "SELECT * FROM t WHERE severity = 'high' AND hostname = 'web' LIMIT 5"
    )
    assert ctx.limit == 5
    assert ctx.filters.get("severity") == "high"
    assert ctx.filters.get("hostname") == "web"
    d.close()


# ---------------------------------------------------------------------------
# _to_dataframe normalizes a dict with a resources key
# ---------------------------------------------------------------------------


def test_to_dataframe_resources_key():
    d = DuckAPI()
    data = {"resources": [{"id": 1}, {"id": 2}], "totalResources": 2}
    df = d._to_dataframe(data, "test")
    assert list(df["id"]) == [1, 2]
    d.close()


def test_to_dataframe_empty_raises():
    d = DuckAPI()
    with pytest.raises(ValueError, match="returned no data"):
        d._to_dataframe([], "test")
    d.close()


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------


def test_validate_missing_required_arg():
    d = DuckAPI()

    def fn(asset_id: int, limit=100):
        return []

    with pytest.raises(ValueError, match="asset_id"):
        d._validate_arguments("fn", fn, {})
    d.close()


# ---------------------------------------------------------------------------
# WHERE with structural parameters (not columns in the result)
# ---------------------------------------------------------------------------


def test_structural_where_param_stripped_from_query(duck):
    """
    Parameters that are in the WHERE clause but aren't columns of the
    result (e.g. site_name, list_name) must be consumed by push-down and
    removed before DuckDB executes the query.
    """
    calls = []

    def tracked_vulns(severity=None, site_name=None, limit=None):
        calls.append({"severity": severity, "site_name": site_name})
        return make_vulns(severity=severity, limit=limit)

    duck.register_api_function("vulns", tracked_vulns)

    # site_name is not a column in the result — must be removed from the WHERE
    df = duck.sql(
        "SELECT * FROM vulns WHERE site_name = 'Intranet' AND severity = 'critical'"
    ).df()

    assert calls[0]["site_name"] == "Intranet"    # reached the function
    assert calls[0]["severity"] == "critical"      # also reached it
    assert all(df["severity"] == "critical")       # DuckDB filtered severity
    assert "site_name" not in df.columns           # not a column in the result


# ---------------------------------------------------------------------------
# list_tables() / SHOW TABLES
# ---------------------------------------------------------------------------


def test_list_tables_lists_registered_functions(duck):
    df = duck.list_tables()

    assert set(df["table_name"]) == {"assets", "vulns"}
    assert list(df.columns) == [
        "table_name", "source", "endpoint", "streaming", "signature", "description",
    ]


def test_list_tables_flags_streaming_registration(duck):
    def iter_assets(hostname=None):
        yield None

    duck.register_streaming_function("assets", iter_assets)

    df = duck.list_tables().set_index("table_name")
    assert bool(df.loc["assets", "streaming"]) is True
    assert bool(df.loc["vulns", "streaming"]) is False


def test_list_tables_empty_when_nothing_registered():
    d = DuckAPI()
    df = d.list_tables()
    assert len(df) == 0
    assert list(df.columns) == [
        "table_name", "source", "endpoint", "streaming", "signature", "description",
    ]
    d.close()


def test_list_tables_source_for_plain_function_is_its_module():
    """A hand-rolled function (not from a bundled connector) shows its own __module__."""
    d = DuckAPI()
    d.register_api_function("v", lambda: [{"x": 1}])
    df = d.list_tables().set_index("table_name")
    assert df.loc["v", "source"] == "test_core"
    d.close()


def test_list_tables_endpoint_none_for_plain_function(duck):
    df = duck.list_tables().set_index("table_name")
    assert df.loc["assets", "endpoint"] is None


def test_list_tables_description_uses_docstring_first_line():
    d = DuckAPI()

    def documented(limit=None):
        """Returns some rows. Second line is ignored."""
        return [{"x": 1}]

    d.register_api_function("documented", documented)
    df = d.list_tables().set_index("table_name")
    assert df.loc["documented", "description"] == "Returns some rows. Second line is ignored."
    d.close()


def test_list_tables_description_empty_without_docstring():
    d = DuckAPI()
    d.register_api_function("undocumented", lambda: [{"x": 1}])
    df = d.list_tables().set_index("table_name")
    assert df.loc["undocumented", "description"] == ""
    d.close()


def test_list_tables_source_and_endpoint_for_bound_method_with_base_url():
    """A registered bound method exposing base_url (the HTTP-wrapper convention) shows it."""
    class FakeConnector:
        base_url = "https://example.service-now.com/api/now"

        def incidents(self, limit=None):
            """Lists incidents."""
            return [{"number": "INC0001"}]

    FakeConnector.incidents.__module__ = "duckduck.servicenow"
    d = DuckAPI()
    d.register_api_function("incidents", FakeConnector().incidents)

    df = d.list_tables().set_index("table_name")
    assert df.loc["incidents", "source"] == "ServiceNow (HTTP API)"
    assert df.loc["incidents", "endpoint"] == "https://example.service-now.com/api/now"
    assert df.loc["incidents", "description"] == "Lists incidents."
    d.close()


def test_sql_show_tables_shortcut(duck):
    df = duck.sql("SHOW TABLES").df()
    assert set(df["table_name"]) == {"assets", "vulns"}


def test_sql_list_tables_shortcut_is_case_insensitive_and_flexible(duck):
    for query in ("list tables", "LIST ALL TABLES", "  Show Tables ; ", "show all tables"):
        df = duck.sql(query).df()
        assert set(df["table_name"]) == {"assets", "vulns"}


def test_sql_show_tables_does_not_shadow_a_real_table_named_tables(duck):
    """A registered function literally named 'tables' must still work normally."""
    duck.register_api_function("tables", lambda: [{"x": 1}])
    df = duck.sql("SELECT * FROM tables").df()
    assert list(df["x"]) == [1]


# ---------------------------------------------------------------------------
# context manager
# ---------------------------------------------------------------------------


def test_context_manager():
    with DuckAPI() as d:
        d.register_api_function("v", lambda: [{"x": 1}])
        df = d.sql("SELECT * FROM v").df()
    assert len(df) == 1


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


def iter_vulns_pages(severity=None):
    """Simulates a 3-page generator."""
    pages = [
        [{"id": "CVE-001", "severity": "critical"}, {"id": "CVE-002", "severity": "high"}],
        [{"id": "CVE-003", "severity": "critical"}, {"id": "CVE-004", "severity": "medium"}],
        [{"id": "CVE-005", "severity": "critical"}],
    ]
    for page in pages:
        df = pd.DataFrame(page)
        if severity:
            df = df[df["severity"] == severity]
        if not df.empty:
            yield df


def test_stream_yields_multiple_chunks():
    d = DuckAPI()
    d.register_api_function("vulns", make_vulns)
    d.register_streaming_function("vulns", iter_vulns_pages)

    chunks = list(d.stream("SELECT * FROM vulns"))
    assert len(chunks) == 3
    assert all(hasattr(c, "columns") for c in chunks)
    d.close()


def test_stream_applies_where_per_chunk():
    d = DuckAPI()
    d.register_api_function("vulns", make_vulns)
    d.register_streaming_function("vulns", iter_vulns_pages)

    chunks = list(d.stream("SELECT * FROM vulns WHERE severity = 'critical'"))
    for chunk in chunks:
        assert all(chunk["severity"] == "critical")
    d.close()


def test_stream_pushes_down_filter_to_generator():
    received = []

    def spy_iter(severity=None):
        received.append({"severity": severity})
        yield pd.DataFrame([{"id": "CVE-001", "severity": severity or "any"}])

    d = DuckAPI()
    d.register_api_function("vulns", make_vulns)
    d.register_streaming_function("vulns", spy_iter)

    list(d.stream("SELECT * FROM vulns WHERE severity = 'critical'"))
    assert received[0]["severity"] == "critical"
    d.close()


def test_stream_no_streaming_function_raises():
    d = DuckAPI()
    d.register_api_function("vulns", make_vulns)

    with pytest.raises(ValueError, match="streaming function"):
        list(d.stream("SELECT * FROM vulns"))
    d.close()


def test_stream_inline_structural_param():
    pages_received = []

    def iter_asset_vulns(asset_id):
        pages_received.append(asset_id)
        yield pd.DataFrame([{"id": "CVE-001", "asset_id": asset_id, "severity": "critical"}])

    def asset_vulns(asset_id, limit=None):
        return [{"id": "CVE-001", "asset_id": asset_id, "severity": "critical"}]

    d = DuckAPI()
    d.register_api_function("asset_vulns", asset_vulns)
    d.register_streaming_function("asset_vulns", iter_asset_vulns)

    list(d.stream("SELECT * FROM asset_vulns(asset_id=42)"))
    assert pages_received == [42]
    d.close()

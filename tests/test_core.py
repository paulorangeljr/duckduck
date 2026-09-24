"""Testes unitários para DuckAPI com push-down de predicados."""

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
# push-down de LIMIT
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
# push-down de WHERE simples
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
# parâmetro explícito na chamada
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
# explícito sobrescreve push-down
# ---------------------------------------------------------------------------


def test_explicit_overrides_pushdown(duck):
    calls = []

    def tracked_assets(hostname=None, ip=None, limit=None):
        calls.append({"hostname": hostname, "ip": ip, "limit": limit})
        return make_assets(hostname=hostname, ip=ip, limit=limit)

    duck.register_api_function("assets", tracked_assets)
    # LIMIT 10 no SQL, mas explícito limit=3 na chamada → explícito vence
    duck.sql("SELECT * FROM assets(limit=3) LIMIT 10").df()

    assert calls[0]["limit"] == 3


# ---------------------------------------------------------------------------
# parâmetro desconhecido pela função é ignorado no push-down
# ---------------------------------------------------------------------------


def test_unknown_where_column_ignored(duck):
    calls = []

    def tracked_assets(hostname=None, ip=None, limit=None):
        calls.append({"hostname": hostname, "ip": ip, "limit": limit})
        return make_assets(hostname=hostname, ip=ip, limit=limit)

    duck.register_api_function("assets", tracked_assets)
    # "severity" não é parâmetro de make_assets, mas o DuckDB filtra depois
    df = duck.sql("SELECT * FROM assets WHERE severity = 'critical'").df()

    assert calls[0].get("severity") is None  # não foi para a API
    assert all(df["severity"] == "critical")  # DuckDB filtrou no resultado


# ---------------------------------------------------------------------------
# parse de kwargs inline
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
    with pytest.raises(ValueError, match="nomeados"):
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
# _to_dataframe normaliza dict com chave resources
# ---------------------------------------------------------------------------


def test_to_dataframe_resources_key():
    d = DuckAPI()
    data = {"resources": [{"id": 1}, {"id": 2}], "totalResources": 2}
    df = d._to_dataframe(data, "test")
    assert list(df["id"]) == [1, 2]
    d.close()


def test_to_dataframe_empty_raises():
    d = DuckAPI()
    with pytest.raises(ValueError, match="não retornou"):
        d._to_dataframe([], "test")
    d.close()


# ---------------------------------------------------------------------------
# validação de assinatura
# ---------------------------------------------------------------------------


def test_validate_missing_required_arg():
    d = DuckAPI()

    def fn(asset_id: int, limit=100):
        return []

    with pytest.raises(ValueError, match="asset_id"):
        d._validate_arguments("fn", fn, {})
    d.close()


# ---------------------------------------------------------------------------
# context manager
# ---------------------------------------------------------------------------


def test_context_manager():
    with DuckAPI() as d:
        d.register_api_function("v", lambda: [{"x": 1}])
        df = d.sql("SELECT * FROM v").df()
    assert len(df) == 1

"""Verbose mode: push-down report, HTTP request lines, pagination progress, redaction."""

import json
import logging
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest
import requests
from requests.adapters import BaseAdapter

from duckduck import DuckAPI, InsightVM
from duckduck.logs import PageProgress, redact_url, set_verbose

ASSETS = [{"id": i, "hostName": f"{'web' if i % 3 else 'db'}-{i:03d}", "riskScore": i * 10} for i in range(1, 26)]


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def logs():
    """Collects everything the duckduck logger tree emits during a test."""
    handler = Collect()
    root = logging.getLogger("duckduck")
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)
    set_verbose(False)


class FakeInsightVM(BaseAdapter):
    def send(self, request, **kw):
        q = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        size, page = int(q.get("size", 10)), int(q.get("page", 0))
        rows = ASSETS
        filters = json.loads(request.body or b"{}").get("filters") if request.method == "POST" else None
        if filters:
            rows = [a for a in ASSETS if a["hostName"].startswith(filters[0]["value"])]
        body = {"resources": rows[page * size:(page + 1) * size],
                "page": {"totalPages": -(-len(rows) // size), "totalResources": len(rows)}}
        r = requests.Response()
        r.status_code, r._content, r.request, r.url = 200, json.dumps(body).encode(), request, request.url
        r.elapsed = timedelta(seconds=0.01)
        return r

    def close(self):
        pass


@pytest.fixture
def duck_r7():
    r7 = InsightVM("console.local", "admin", "s3cr3t", default_page_size=10)
    r7.session.mount("https://", FakeInsightVM())
    duck = DuckAPI(verbose="info")
    duck.register_api_function("assets", r7.assets)
    yield duck
    duck.close()


def test_report_explains_what_was_and_wasnt_pushed(logs, duck_r7):
    duck_r7.sql("SELECT hostName FROM assets WHERE riskScore >= 200 LIMIT 3").df()
    text = logs.text()
    assert "▶ assets()" in text
    assert "✗ riskscore >= 200 — no riskscore_gte parameter, DuckDB filters" in text
    assert "✗ LIMIT 3 — not every WHERE condition reached the source" in text


def test_pushed_conditions_and_limit_are_reported(logs, duck_r7):
    duck_r7.sql("SELECT hostName FROM assets WHERE hostname LIKE 'db%' LIMIT 5").df()
    text = logs.text()
    assert "▶ assets(hostname_ilike='db%', limit=5)" in text
    assert "✓ hostname LIKE 'db%' → hostname_ilike" in text and "✓ LIMIT 5 → limit" in text
    assert "POST https://console.local/api/3/assets/search?size=5&page=0 → 200" in text


@pytest.mark.parametrize("query, reason", [
    ("SELECT * FROM assets ORDER BY riskScore LIMIT 1", "ORDER BY in the query"),
    ("SELECT count(*) FROM assets LIMIT 1", "aggregate in the query"),
    ("SELECT * FROM assets WHERE hostname = 'a' OR riskScore > 1 LIMIT 1", "WHERE has conditions DuckDB must apply first"),
])
def test_limit_blockers_are_named(logs, duck_r7, query, reason):
    duck_r7.sql(query).df()
    assert f"— {reason}" in logs.text()


def test_untranslatable_like_is_explained(logs, duck_r7):
    duck_r7.sql("SELECT * FROM assets WHERE hostname LIKE 'db_0%'").df()
    assert "pattern not translatable" in logs.text()


def test_every_page_logs_progress_with_time_left(logs, duck_r7):
    duck_r7.sql("SELECT * FROM assets").df()
    pages = [line for line in logs.lines if line.startswith("/assets: page")]
    assert len(pages) == 3
    assert pages[0].startswith("/assets: page 1/3 · 10/25 rows") and "left" in pages[0]
    assert pages[-1].startswith("/assets: page 3/3 · 25/25 rows") and "left" not in pages[-1]
    assert sum("GET https://console.local/api/3/assets?size=10&page=" in line for line in logs.lines) == 3
    assert any(line.startswith("  assets: 25 rows × 3 columns in") for line in logs.lines)


def test_debug_shows_bodies_but_never_secrets(logs):
    r7 = InsightVM("console.local", "admin", "s3cr3t", default_page_size=10)
    r7.session.mount("https://", FakeInsightVM())
    set_verbose("debug")
    r7.assets(hostname="web")
    r7.session.post("https://console.local/api/3/x?api_key=AAA&size=1", json={"password": "hunter2", "q": 1})
    text = logs.text()
    assert '"field": "host-name"' in text  # the search body
    assert "hunter2" not in text and "AAA" not in text and "s3cr3t" not in text
    assert '"password": "***"' in text and "api_key=***" in text


def test_servicenow_token_request_is_logged_without_its_body(logs, monkeypatch):
    from duckduck.servicenow import ServiceNow

    sn = ServiceNow.from_oauth2(token_url="https://login.example/token", client_id="cid",
                                client_secret="TOPSECRET", instance="dev1")
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
    response.request = requests.Request("POST", "https://login.example/token",
                                        data={"client_secret": "TOPSECRET"}).prepare()
    response.elapsed = timedelta(seconds=0.2)
    monkeypatch.setattr("duckduck.servicenow.requests.post", lambda *a, **k: response)
    set_verbose("debug")
    sn._ensure_token()
    text = logs.text()
    assert "POST https://login.example/token → 200" in text and "TOPSECRET" not in text


def test_stream_logs_chunks(logs):
    def iter_rows():
        yield pd.DataFrame({"a": [1, 2, 3]})
        yield pd.DataFrame({"a": [4, 5]})

    duck = DuckAPI(verbose=True)
    duck.register_api_function("t", lambda limit=None: [])
    duck.register_streaming_function("t", iter_rows)
    list(duck.stream("SELECT * FROM t WHERE a > 2"))
    chunks = [line for line in logs.lines if "chunk" in line]
    assert chunks[0].startswith("  t: chunk 1 · 3 rows read · 1 kept")
    assert chunks[1].startswith("  t: chunk 2 · 5 rows read · 3 kept")


def test_page_progress_estimates_from_row_totals(logs):
    set_verbose("info")
    p = PageProgress("x", "things")
    p.page(100, total_rows=400)
    assert logs.lines[-1].startswith("things: page 1 · 100/400 rows ·") and "left" in logs.lines[-1]
    p = PageProgress("x", "unknown total")
    p.page(50)
    assert "left" not in logs.lines[-1]


def test_verbose_levels_and_env(monkeypatch, logs):
    root = logging.getLogger("duckduck")
    DuckAPI(verbose="debug")
    assert root.level == logging.DEBUG
    DuckAPI(verbose=False)
    assert root.level == logging.NOTSET and not [h for h in root.handlers if h is not logs]
    monkeypatch.setenv("DUCKDUCK_VERBOSE", "info")
    DuckAPI()
    assert root.level == logging.INFO
    DuckAPI()  # handler attached once, not per instance
    assert len([h for h in root.handlers if h is not logs]) == 1
    with pytest.raises(ValueError):
        DuckAPI(verbose="loud")


def test_quiet_by_default(logs, monkeypatch):
    monkeypatch.delenv("DUCKDUCK_VERBOSE", raising=False)
    duck = DuckAPI()
    duck.register_api_function("t", lambda limit=None: [{"a": 1}])
    duck.sql("SELECT * FROM t").df()
    assert logs.lines == []


def test_redact_url():
    assert redact_url("https://h/p?size=1&api_key=x&token=y") == "https://h/p?size=1&api_key=***&token=***"

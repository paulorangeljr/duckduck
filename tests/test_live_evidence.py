"""Live evidence: probing the question's values at the source and telling the decision engine."""

import json
import time
from unittest.mock import MagicMock

import pandas as pd
import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, JEVAdapter, JevClient, SemanticSearch  # noqa: E402
from duckduck.semantic.evidence import EvidenceProber  # noqa: E402

PROXY = pd.DataFrame({"user": ["alice", "bob"], "domain": ["github.com", "example.org"],
                      "referrer": ["google.com", "github.com"]})
DNS = pd.DataFrame({"host": ["web-1"], "query": ["example.org"]})

CATALOG = {
    "sources": {
        "proxy": {"table": "proxy", "description": "Web proxy traffic.", "entities": ["user"],
                  "activities": ["web_access"],
                  "fields": {"user": {"semantic_type": "user"},
                             "domain": {"semantic_type": "domain", "description": "Domain requested", "match": "contains"},
                             "referrer": {"semantic_type": "domain", "description": "Referring domain", "match": "contains"}}},
        "dns": {"table": "dns", "description": "DNS lookups.", "entities": ["host"],
                "fields": {"host": {"semantic_type": "host"}, "query": {"semantic_type": "domain", "match": "contains"}}},
    },
    "entities": {"user": {"keywords": ["users"]}, "host": {"keywords": ["hosts"]}},
    "activities": {"web_access": {"keywords": ["accessed", "visited"], "resource": "domain", "resource_role": None}},
}


def _filtering(df, calls):
    """A connector that applies ILIKE conditions and LIMIT itself (like SQL/ADX/files)."""
    def fn(where=None, limit=None):
        calls.append({"where": where, "limit": limit})
        out = df
        for c in where or []:
            needle = str(c.value).strip("%").lower()
            out = out[out[c.column].astype(str).str.lower().str.contains(needle, regex=False)]
        return out.head(limit) if limit else out
    return fn


def _duck(calls=None):
    calls = [] if calls is None else calls
    duck = DuckAPI()
    duck.register_api_function("proxy", _filtering(PROXY, calls))
    duck.register_api_function("dns", _filtering(DNS, calls))
    return duck, calls


def test_a_probe_is_one_filtered_row_at_the_source():
    duck, calls = _duck()
    prober = EvidenceProber(Catalog.model_validate(CATALOG), duck)
    assert prober.found("proxy.domain", "github") is True
    assert prober.found("dns.query", "github") is False
    assert calls[0]["limit"] == 1 and calls[0]["where"][0].op == "ilike" and calls[0]["where"][0].value == "%github%"
    assert prober.found("proxy.domain", "GitHub") is True and len(calls) == 2  # cached, case-insensitively
    assert [(p.field, p.found) for p in prober.probes] == [("proxy.domain", True), ("dns.query", False)]


def test_no_probe_without_pushdown_or_limit():
    duck = DuckAPI()
    duck.register_api_function("proxy", lambda: PROXY)  # neither filters nor limits: would download everything
    duck.register_api_function("dns", lambda limit=None: DNS.head(limit))  # limits, can't filter
    prober = EvidenceProber(Catalog.model_validate(CATALOG), duck)
    assert prober.found("proxy.domain", "github") is None and "takes no limit" in prober.probes[-1].note
    assert prober.found("dns.query", "github") is None and "can't filter" in prober.probes[-1].note


def test_budget_timeout_and_errors_never_break_the_question():
    duck, _ = _duck()
    prober = EvidenceProber(Catalog.model_validate(CATALOG), duck, max_probes=1)
    prober.found("proxy.domain", "a")
    assert prober.found("proxy.domain", "b") is None and "budget" in prober.probes[-1].note

    slow = DuckAPI()
    slow.register_api_function("proxy", lambda where=None, limit=None: time.sleep(1) or PROXY)
    prober = EvidenceProber(Catalog.model_validate(CATALOG), slow, timeout=0.05)
    assert prober.found("proxy.domain", "github") is None and "timed out" in prober.probes[-1].note

    def broken(where=None, limit=None):
        raise RuntimeError("API down")

    bad = DuckAPI()
    bad.register_api_function("proxy", broken)
    prober = EvidenceProber(Catalog.model_validate(CATALOG), bad)
    assert prober.found("proxy.domain", "github") is None and "API down" in prober.probes[-1].note


class Jev:
    """Fake Decisions API recording every request; says yes to everything."""

    def __init__(self):
        self.bodies = []

    def __call__(self, url, data, timeout):
        body = json.loads(data)
        self.bodies.append(body)
        wanted = {"user", "web_access", "domain"}
        wanted |= {"data"}  # what a question about hosts and users is about
        answers = {k: ({"probabilities": {c: (0.9 if c in wanted else 0.01) for c in q["criteria"]}}
                       if q["type"] == "choice" else {"noul": 0.95})
                   for k, q in body["questions"].items()}
        r = MagicMock(status_code=200, ok=True)
        r.json.return_value = {"answers": answers}
        return r


def _search(monkeypatch, live=True):
    fake = Jev()
    monkeypatch.setattr("requests.Session.post", lambda self, url, data, timeout: fake(url, data, timeout))
    duck, calls = _duck()
    search = SemanticSearch(Catalog.model_validate(CATALOG), duck, live_evidence=live,
                            engine=JEVAdapter(JevClient(api_key="k"), cache_size=0))
    return search, fake, calls


def test_where_the_value_was_found_reaches_the_engine(monkeypatch):
    search, fake, calls = _search(monkeypatch)
    result = search.search("Which users accessed github?")
    items = fake.bodies[0]["state"]["items"]
    proxy = items["source:proxy"]["facts"]["live_check"]
    # not typed yet when probed ("accessed X" types it later), so every typed text field is checked
    assert proxy == [{"value": "github", "found_in": ["proxy.domain", "proxy.referrer"], "not_found_in": ["proxy.user"]}]
    assert items["value:github"]["facts"]["live_check"] == {"value_found_in_fields_of_type": ["domain"]}
    assert items["source:dns"]["facts"]["live_check"][0]["not_found_in"] == ["dns.host", "dns.query"]
    assert {(p.field, p.found) for p in result.evidence} >= {("proxy.domain", True), ("dns.query", False)}
    assert json.loads(result.to_json())["evidence"][0]["value"] == "github"


def test_the_field_confirmation_carries_the_probe(monkeypatch):
    search, fake, calls = _search(monkeypatch)
    assert search.search("Which users accessed github?").status == "ok"
    fields = {k: v for b in fake.bodies[1:] for k, v in b["state"].get("items", {}).items() if k.startswith("field:")}
    live = [v["facts"]["live_check"] for v in fields.values() if "live_check" in v.get("facts", {})]
    assert live == [{"value": "github", "found": True}]  # proxy has two domain fields: the choice is asked


def test_off_by_default(monkeypatch):
    search, fake, calls = _search(monkeypatch, live=None)
    result = search.search("Which users accessed github?")
    assert not result.evidence and not any("live_check" in json.dumps(b) for b in fake.bodies)
    assert all(c["limit"] != 1 for c in calls)  # no probe queries, only the real fetch


def test_config(tmp_path):
    from duckduck.semantic import SemanticConfig

    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps({"services": {}, "semantic": {"live_evidence": {"enabled": True, "max_probes": 3}}}))
    cfg = SemanticConfig.load(DuckAPI(), str(path))
    assert cfg.live_evidence.enabled and cfg.live_evidence.max_probes == 3 and cfg.live_evidence.timeout == 5.0

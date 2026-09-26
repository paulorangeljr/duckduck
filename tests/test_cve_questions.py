"""Questions about CVEs (the NVD connector): "catalog" in the data's own words is not a question about this assistant."""

from datetime import datetime

import pytest

pytest.importorskip("pydantic")

from duckduck import NVD, DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, SemanticSearch  # noqa: E402
from duckduck.semantic.shapes import AnswerShapes  # noqa: E402

from test_public_apis import LOG4SHELL, XSS, FakeNVD  # noqa: E402

NOW = datetime(2026, 9, 26, 12, 0)
RECENT_KEV = {"cve": {**LOG4SHELL["cve"], "id": "CVE-2026-2000", "published": "2026-03-01T10:00:00.000"}}

CATALOG = {
    "sources": {
        "alerts": {"table": "alerts", "description": "Security alerts raised about IP addresses.",
                   "entities": ["ip_address"], "fields": {"ip": {"semantic_type": "ip_address"}}},
        "nvd_cves": {
            "table": "nvd_cves", "time_field": "published", "entities": ["vulnerability"],
            "description": "Each row is a CVE from the National Vulnerability Database, with its CVSS severity, "
                           "its weakness (CWE) and whether it is in CISA's Known Exploited Vulnerabilities (KEV) "
                           "catalog.",
            "fields": {
                "id": {"semantic_type": "vulnerability", "description": "CVE identifier"},
                "published": {"type": "datetime", "semantic_type": "event_time", "description": "When it was published"},
                "severity": {"description": "CVSS v3 severity",
                             "values": {"LOW": [], "MEDIUM": [], "HIGH": [], "CRITICAL": ["critical-severity"]}},
                "in_kev": {"type": "boolean", "description": "In CISA's Known Exploited Vulnerabilities catalog",
                           "values": {"true": ["cisa kev catalog", "kev catalog", "cisa kev", "kev", "known exploited"]}},
                "description": {"description": "What the vulnerability is"},
            },
        },
    },
    "entities": {"ip_address": {"description": "An IP address", "keywords": ["ip", "host"]},
                 "vulnerability": {"description": "A CVE", "keywords": ["cve", "cves", "vulnerabilities"]}},
}


def _search():
    nvd = NVD(sleep=lambda s: None)
    nvd.session.get = FakeNVD([RECENT_KEV, XSS, LOG4SHELL])
    duck = DuckAPI()
    duck.register_api_function("nvd_cves", nvd.cves)
    duck.register_api_function("alerts", lambda limit=None: [{"ip": "10.0.0.1"}])
    return SemanticSearch(Catalog.model_validate(CATALOG), duck, clock=lambda: NOW), nvd


QUESTION = "Which critical-severity CVEs published in the last year are in the CISA KEV catalog?"


@pytest.mark.parametrize("question, candidates", [
    (QUESTION, ["list"]),                                        # someone else's catalog: the data's words
    ("Which products are in the catalog of Apple?", ["list"]),
    ("Show me your catalog", ["catalog"]),                        # this assistant's
    ("Which entities exist in the catalog?", ["catalog"]),
    ("Quais entidades existem no seu catalogo", ["catalog"]),
])
def test_whose_catalog(question, candidates):
    assert AnswerShapes().candidates(question)[0] == candidates


def test_a_question_that_filters_on_the_data_is_about_the_data():
    # even worded like a catalog question, a time window or a known value makes it about the data
    assert AnswerShapes().candidates("Show me your catalog of CVEs", about_data=True)[0] == ["list"]
    search, nvd = _search()
    result = search.search(QUESTION)
    assert result.intent.answer_shape == "list" and result.intent.target_entity == "vulnerability"
    assert result.intent.time_range is not None
    assert result.status == "ok", result.report()
    assert "CVE-2026-2000" in result.results.to_string()
    urls = nvd.session.get.urls
    assert any("pubStartDate=2025-09-26" in u for u in urls)  # the year went to NVD, cut into its windows


def test_what_it_is_about_is_asked_of_the_engine_with_the_wording_as_a_fact():
    from duckduck.semantic.extraction import Extraction, Reading
    from duckduck.semantic.intent import SemanticIntent

    search, _ = _search()
    intent = SemanticIntent(question="Show me your catalog",
                            reading=Reading(answer="list", about_kind="entity", about="vulnerability"))
    assert search.interpreter._candidates(intent)[0] == ["list"]  # answer kinds about the data only
    ask = search.interpreter._subject_ask(intent, Extraction(), 0.0, {})
    assert set(ask.options) == {"data", "assistant", "other"}
    assert "your catalog" in ask.state.facts["wording_about_this_assistant"]
    assert ask.state.default == "assistant"  # the offline engine's reading; Jev reads the question itself

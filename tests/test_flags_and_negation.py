"""Boolean flags get words in the catalog; "not X" filters X out; accents don't stop a match."""

import json
from datetime import datetime

import pytest

pytest.importorskip("pydantic")

from duckduck import NVD, DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, CatalogGenerator, SemanticSearch  # noqa: E402
from duckduck.semantic.extraction import RuleBasedExtractor  # noqa: E402
from duckduck.semantic.generation import GenEntity, GenField, GenSource, GenVocabulary, _flag_values, _flag_words  # noqa: E402
from duckduck.semantic.text import tokenize  # noqa: E402

from test_answer_shapes import CATALOG, _duck  # noqa: E402
from test_public_apis import LOG4SHELL, XSS, FakeNVD  # noqa: E402

NOW = datetime(2026, 9, 26, 12, 0)
CRITICAL_NOT_KEV = {"cve": {**LOG4SHELL["cve"], "id": "CVE-2026-3000", "cisaExploitAdd": None,
                            "cisaActionDue": None, "cisaRequiredAction": None, "cisaVulnerabilityName": None}}


@pytest.mark.parametrize("name, words", [
    ("in_kev", ["in kev", "kev"]), ("mfa_enabled", ["mfa enabled", "mfa"]), ("is_active", ["is active", "active"]),
    ("exploited", ["exploited"]),
])
def test_words_from_a_flag_s_name(name, words):
    assert _flag_words(name) == words


def test_a_flag_s_values_are_normalized_and_always_say_true():
    field = {"type": "boolean", "values": {"True": ["known exploited"], "no": []}}
    _flag_values(field, "in_kev")
    assert field["values"] == {"true": ["known exploited", "in kev", "kev"], "false": []}
    other = {"type": "string", "values": {"a": []}}
    _flag_values(other, "x")
    assert other["values"] == {"a": []}  # only true/false columns


class SilentLLM:
    """Drafts the CVE table as an LLM might: in_kev typed boolean, but no words for it."""

    def generate(self, system, prompt, output_model):
        if output_model is GenVocabulary:
            return GenVocabulary(entities=[GenEntity(name="vulnerability", description="A CVE",
                                                     keywords=["cve", "cves", "vulnerabilities"])])
        columns = json.loads(prompt.split("Table profile:\n", 1)[1])["columns"]
        kinds = {"in_kev": "boolean", "published": "datetime", "cvss_score": "float"}
        return GenSource(
            description="CVEs from the National Vulnerability Database.", time_field="published",
            entities=["vulnerability"],
            fields=[GenField(name=c, type=kinds.get(c, "string"),
                             semantic_type={"id": "vulnerability", "published": "event_time"}.get(c),
                             values=[{"stored": "CRITICAL", "synonyms": ["critical"]}] if c == "severity" else [])
                    for c in columns],
        )


def _nvd_duck():
    nvd = NVD(sleep=lambda s: None)
    nvd.session.get = FakeNVD([LOG4SHELL, XSS, CRITICAL_NOT_KEV])
    duck = DuckAPI()
    duck.register_api_function("nvd_cves", nvd.cves)
    return duck


def test_the_generated_catalog_lets_a_question_name_the_flag():
    duck = _nvd_duck()
    result = CatalogGenerator(SilentLLM(), duck, clock=lambda: NOW).generate()
    in_kev = result.catalog.sources["nvd_cves"].fields["in_kev"]
    assert in_kev.type == "boolean" and in_kev.values == {"true": ["in kev", "kev"]}

    search = SemanticSearch(result.catalog, duck, clock=lambda: NOW)
    kev = search.search("Which critical CVEs are in the KEV catalog?")
    assert kev.status == "ok", kev.report()
    assert '"in_kev" = TRUE' in kev.sql and "CVE-2021-44228" in set(kev.results["id"])
    assert "CVE-2026-3000" not in set(kev.results["id"])
    not_kev = search.search("Which critical CVEs are not in KEV?")
    assert not_kev.status == "ok", not_kev.report()
    assert '"in_kev" <> TRUE' in not_kev.sql and set(not_kev.results["id"]) == {"CVE-2026-3000"}


def test_not_before_a_known_value_filters_it_out():
    search = SemanticSearch(Catalog.model_validate(CATALOG), _duck())
    result = search.search("Which hosts have alerts that are not critical?")
    match = next(m for m in result.intent.value_filters if m.field == "alerts.severity")
    assert match.negated and '"severity" <> \'critical\'' in result.sql
    assert search.search("Which hosts have critical alerts?").intent.value_filters[0].negated is False


def test_accents_and_portuguese_negation():
    assert tokenize("não críticos") == ["nao", "criticos"]
    catalog = Catalog.model_validate({**CATALOG, "sources": {**CATALOG["sources"], "alerts": {
        **CATALOG["sources"]["alerts"], "fields": {**CATALOG["sources"]["alerts"]["fields"], "severity": {
            "description": "Alert severity", "values": {"low": ["baixa"], "high": ["alta"], "critical": ["crítico"]}}}}}})
    extractor = RuleBasedExtractor(catalog)
    [crit] = extractor.extract("hosts com alertas críticos", NOW).enum_matches
    assert (crit.value, crit.negated) == ("critical", False)
    [neg] = extractor.extract("hosts com alertas não críticos", NOW).enum_matches
    assert (neg.value, neg.negated) == ("critical", True)
    [kev] = extractor.extract("CVEs no catálogo crítico", NOW).enum_matches  # "no" is "in the", not a negation
    assert kev.negated is False

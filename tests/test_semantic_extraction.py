from datetime import datetime

import pytest

from duckduck.semantic import Catalog, RuleBasedExtractor
from duckduck.semantic.text import stem
from semantic_helpers import CATALOG_PATH, NOW


@pytest.fixture(scope="module")
def extractor():
    return RuleBasedExtractor(Catalog.load(CATALOG_PATH))


@pytest.mark.parametrize("a, b", [
    ("machines", "machine"), ("queried", "query"), ("queries", "query"),
    ("connection", "connected"), ("users", "user"), ("denied", "deny"),
])
def test_stem_conflates_inflections(a, b):
    assert stem(a) == stem(b)


@pytest.mark.parametrize("phrase, hours", [
    ("in the last 24hrs", 24), ("last 24 hours", 24), ("past 7 days", 168),
    ("over the last hour", 1), ("previous 30 minutes", 0.5), ("last 2 weeks", 336),
])
def test_relative_time_ranges(extractor, phrase, hours):
    tr = extractor.extract(f"Which users accessed github {phrase}?", NOW).time_range
    assert tr.last_hours == pytest.approx(hours)


def test_today_and_yesterday_are_absolute(extractor):
    today = extractor.extract("failed logins today", NOW).time_range
    assert today.start == datetime(2026, 9, 24) and today.end is None
    yesterday = extractor.extract("failed logins yesterday", NOW).time_range
    assert (yesterday.start, yesterday.end) == (datetime(2026, 9, 23), datetime(2026, 9, 24))


def test_shaped_literals(extractor):
    ex = extractor.extract("hosts that talked to 10.1.2.3 or evil.example.com, owned by bob@corp.com", NOW)
    kinds = {lit.value: lit.kind for lit in ex.literals if lit.kind != "term"}
    assert kinds == {"10.1.2.3": "ip_address", "evil.example.com": "domain", "bob@corp.com": "email"}


def test_invalid_ip_is_not_an_ip(extractor):
    kinds = {lit.value: lit.kind for lit in extractor.extract("connected to 999.1.1.1", NOW).literals}
    assert "999.1.1.1" not in kinds or kinds["999.1.1.1"] != "ip_address"


def test_quoted_literal_keeps_spaces(extractor):
    lits = extractor.extract("which users accessed 'my site'", NOW).literals
    assert [(lit.value, lit.kind) for lit in lits] == [("my site", "quoted")]


def test_enum_synonyms_map_to_stored_values(extractor):
    ex = extractor.extract("Show me denied connections from production machines.", NOW)
    matches = {(m.field, m.value) for m in ex.enum_matches}
    assert ("firewall_logs.action", "DENY") in matches
    assert ("asset_inventory.environment", "production") in matches
    assert ex.focus_terms == [stem("connections")]


def test_focus_is_the_head_noun_and_free_text_is_the_leftover(extractor):
    ex = extractor.extract("Which users accessed github in the last 24hrs?", NOW)
    assert ex.focus_terms == ["user"]
    assert set(ex.terms) == {"user", "access"}
    assert [(lit.value, lit.kind) for lit in ex.literals] == [("github", "term")]

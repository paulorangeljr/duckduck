"""Time windows in questions — English and Portuguese, ranges with both ends, open-ended ones."""

from datetime import datetime as D

import pytest

pytest.importorskip("pydantic")

from duckduck.semantic.timeparse import find_window  # noqa: E402

NOW = D(2026, 9, 25, 15, 0)  # a Friday


@pytest.mark.parametrize("question, start, end, hours", [
    ("show me the logins between today and tomorrow", D(2026, 9, 25), D(2026, 9, 27), None),  # tomorrow counts whole
    ("mostre os logins entre hoje e amanhã", D(2026, 9, 25), D(2026, 9, 27), None),
    ("logins between 2026-09-20 and 2026-09-22", D(2026, 9, 20), D(2026, 9, 23), None),
    ("logins from 2026-09-20 to 2026-09-22", D(2026, 9, 20), D(2026, 9, 23), None),
    ("de 20/09/2026 até 22/09/2026", D(2026, 9, 20), D(2026, 9, 23), None),               # day-first
    ("logins on 09/25/2026", D(2026, 9, 25), None, None),                                  # month-first: 25 isn't a month
    ("from 2026-09-20 10:00 to 2026-09-20 18:00", D(2026, 9, 20, 10), D(2026, 9, 20, 18), None),
    ("logins yesterday", D(2026, 9, 24), D(2026, 9, 25), None),
    ("logins de ontem", D(2026, 9, 24), D(2026, 9, 25), None),
    ("anteontem", D(2026, 9, 23), D(2026, 9, 24), None),
    ("logins today", D(2026, 9, 25), None, None),                                          # so far today
    ("logins this week", D(2026, 9, 21), None, None),                                      # weeks start on Monday
    ("semana passada", D(2026, 9, 14), D(2026, 9, 21), None),
    ("mês passado", D(2026, 8, 1), D(2026, 9, 1), None),
    ("em setembro de 2025", D(2025, 9, 1), D(2025, 10, 1), None),
    ("logins in december", D(2025, 12, 1), D(2026, 1, 1), None),                            # not yet this year
    ("failed logins since monday", D(2026, 9, 21), None, None),
    ("desde segunda", D(2026, 9, 21), None, None),
    ("logins after yesterday", D(2026, 9, 25), None, None),
    ("logins before 2026-09-21", None, D(2026, 9, 21), None),
    ("até ontem", None, D(2026, 9, 25), None),                                              # until: ontem counts
    ("in the last 24 hours", None, None, 24.0),
    ("nas últimas 24 horas", None, None, 24.0),
    ("últimos 7 dias", None, None, 168.0),
    ("CVEs published in the last year", None, None, 8760.0),                                # rolling, like last week
    ("nos últimos 2 anos", None, None, 17520.0),
    ("logins this year", D(2026, 1, 1), None, None),                                         # calendar
    ("logins do ano passado", D(2025, 1, 1), D(2026, 1, 1), None),
])
def test_windows(question, start, end, hours):
    window, _, _ = find_window(question, NOW)
    assert (window.start, window.end, window.last_hours) == (start, end, hours)


@pytest.mark.parametrize("question", ["may I see the logins", "the second table", "show me the owners", "10.0.0.5"])
def test_no_window(question):
    assert find_window(question, NOW) is None


def test_a_range_becomes_a_filter_on_the_time_field_and_is_not_a_value():
    import os

    from duckduck.semantic import SemanticSearch, connect

    config = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "examples", "semantic", "duckduck.local.json")
    search = SemanticSearch.from_config(connect(config), config, clock=lambda: NOW)
    result = search.search("mostre os logins entre hoje e amanhã")
    assert result.status == "ok" and result.intent.target_entity == "event"  # "the logins": their records
    assert ">= TIMESTAMP '2026-09-25 00:00:00'" in result.sql and "< TIMESTAMP '2026-09-27 00:00:00'" in result.sql
    assert not [lit for lit in result.intent.literals if lit.value.lower() in ("hoje", "amanhã")]

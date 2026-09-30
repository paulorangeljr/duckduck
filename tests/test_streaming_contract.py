"""Every table whose API pages has a streaming function (``iter_<table>``) registered next to it, taking the same
filters — so ``sql()`` filters it page by page instead of holding the whole API result. New connectors are held to
it here; the add-connector skill says the same."""

import inspect

import pytest

from duckduck.kinds import CATALOG, RAW_QUERY, kind_of
from duckduck.registry import SERVICE_REGISTRY
from duckduck.sparkplan import plan_of

#: tables that don't page — why, in one line each (a new exemption needs one too)
ONE_READ = {
    ("insightvm", "report_templates"): "one request, no pages",
    ("restcountries", "countries"): "~250 rows, filters applied after one read of the whole list",
}


def _tables():
    for service, spec in SERVICE_REGISTRY.items():
        cls = getattr(spec.factory, "__self__", None)
        if not isinstance(cls, type):
            continue
        for table, method in spec.tables.items():
            yield service, spec, cls, table, method


@pytest.mark.parametrize("service,spec,cls,table,method",
                         [pytest.param(*t, id=f"{t[0]}.{t[3]}") for t in _tables()])
def test_a_paging_table_streams_with_the_same_filters(service, spec, cls, table, method):
    fn = getattr(cls, method)
    plan = plan_of(fn)
    if kind_of(fn) in (CATALOG, RAW_QUERY) or (plan and plan.strategy == "native"):
        return  # listings are small; native sources filter inside their own scan
    if (service, table) in ONE_READ:
        return
    assert table in spec.streaming_tables, (
        f"{service}.{table} pages but has no iter_{table} in streaming_tables — write one (one DataFrame per page, "
        f"same filters) or add it to ONE_READ with the reason")
    stream = getattr(cls, spec.streaming_tables[table])
    takes = set(inspect.signature(stream).parameters) - {"self"}
    needs = set(inspect.signature(fn).parameters) - {"self", "limit"}
    assert needs <= takes, f"{service}.{spec.streaming_tables[table]} lacks {sorted(needs - takes)}"

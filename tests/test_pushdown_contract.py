"""Push-down is a contract, not a per-connector review: every bundled table must let a WHERE or a join key
narrow its read, so it is never read whole when the query knows better. This test walks **every** registered
table and fails a new connector that doesn't meet it, exactly like `@spark_plan` (test_spark), `NO_SORT`
(test_sorting), `IDEMPOTENCY` (test_batches) and `ONE_READ` (test_streaming_contract) do for their contracts.
A table whose source genuinely offers no filter goes in `NO_PUSHDOWN` with a one-line reason — the add-connector
skill and CLAUDE.md say the same.
"""

import inspect

import pytest

from duckduck.common.kinds import CATALOG, RAW_QUERY, kind_of
from duckduck.connectors.registry import SERVICE_REGISTRY

#: tables whose source offers no way to filter — a fixed listing read whole every time. Why, in one line each
#: (a new exemption needs one too). A by-id endpoint is NOT here: its id is a structural argument a join key
#: or `WHERE arg.x =` fills, so it still narrows.
NO_PUSHDOWN = {
    ("airflow", "import_errors"): "the MWAA endpoint lists every import error, no filter parameters",
    ("insightvm", "scan_engines"): "a short fixed listing of the console's scan engines",
    ("insightvm", "report_templates"): "a short fixed listing of report templates",
    ("insightvm", "reports"): "lists the configured reports, no filter parameters",
    ("sharepoint", "sites"): "lists the sites the app can see, no filter parameters",
}


def _tables():
    for service, spec in SERVICE_REGISTRY.items():
        cls = getattr(spec.factory, "__self__", None)
        if not isinstance(cls, type):
            continue
        for table, method in spec.tables.items():
            yield service, spec, cls, table, method


def _narrowing_params(fn):
    """The call's arguments a WHERE or a join key can fill: everything but `self` / `limit` / `order_by`
    (`where`, a `col`/`col_ilike`/`col_gt` filter, or a structural arg a lookup join / `WHERE arg.x =` fills)."""
    return [n for n in inspect.signature(fn).parameters if n not in ("self", "limit", "order_by")]


@pytest.mark.parametrize("service,spec,cls,table,method",
                         [pytest.param(*t, id=f"{t[0]}.{t[3]}") for t in _tables()])
def test_a_table_narrows_its_read_or_says_why_not(service, spec, cls, table, method):
    """A WHERE or a join key must reach the source — the table declares `where`, a column-filter param, or a
    structural argument — unless its source can't filter at all (then it's in NO_PUSHDOWN)."""
    fn = getattr(cls, method)
    if kind_of(fn) in (CATALOG, RAW_QUERY):
        return  # a catalog / raw-query listing isn't a data table
    if (service, table) in NO_PUSHDOWN:
        return
    assert _narrowing_params(fn), (
        f"{service}.{table} takes only limit/order_by, so every WHERE and every join reads it whole — give it a "
        f"`where` param (any column) or the column-filter params it can push (see the operator → parameter "
        f"convention), or add it to NO_PUSHDOWN with the reason its source can't filter")


@pytest.mark.parametrize("service,spec,cls,table,method",
                         [pytest.param(*t, id=f"{t[0]}.{t[3]}") for t in _tables()])
def test_a_where_connector_takes_a_join_key_as_in(service, spec, cls, table, method):
    """A `where` connector must declare `in` in its `WHERE_OPS`: a join sends the other side's key values as one
    `col IN (…)` condition, so without `in` the whole probe table is read on a cross-source join (the bug the
    cross-source tests caught). Every bundled `where` connector already does; a new one must too."""
    fn = getattr(cls, method)
    if "where" not in inspect.signature(fn).parameters:
        return  # a col-param table takes a join key as a lookup call per value instead
    where_ops = getattr(cls, "WHERE_OPS", None)
    assert where_ops is not None and "in" in where_ops, (
        f"{service}.{table} takes `where` but its class has no WHERE_OPS with 'in' — a join key can't be pushed "
        f"as `col IN (…)`, so a cross-source join reads the whole table. Add "
        f'WHERE_OPS = frozenset({{"eq", "like", "ilike", "gt", "gte", "lt", "lte", "in"}}) to {cls.__name__} '
        f"(and make its `where` apply an IN)")

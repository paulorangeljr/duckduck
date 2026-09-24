from datetime import datetime

import duckdb
import pytest
from pydantic import ValidationError

from duckduck.semantic import (
    Catalog,
    Filter,
    Join,
    LexicalDecisionEngine,
    LogicalQueryPlan,
    PlanValidationError,
    QueryValidator,
    RelationshipGraph,
    TimeRangeFilter,
)
from duckduck.semantic.compiler import compile_plan, render_literal
from semantic_helpers import CATALOG_PATH, NOW


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(CATALOG_PATH)


# ---------------------------------------------------------------------------
# Relationship graph
# ---------------------------------------------------------------------------


def test_graph_prefers_highest_confidence_edge(catalog):
    path = RelationshipGraph(catalog).find_path("firewall_logs", lambda s: s == "asset_inventory")
    assert [(e.left, e.right) for e in path.edges] == [("firewall_logs.src_ip", "asset_inventory.ip_address")]
    assert path.confidence == pytest.approx(0.95)


def test_graph_multi_hop_and_max_hops(catalog):
    graph = RelationshipGraph(catalog)
    path = graph.find_path("proxy_logs", lambda s: s == "dns_logs")
    assert path.sources == ["proxy_logs", "asset_inventory", "dns_logs"]
    assert path.confidence == pytest.approx(0.95 * 0.95)
    assert graph.find_path("proxy_logs", lambda s: s == "dns_logs", max_hops=1) is None


def test_graph_start_satisfying_goal_is_an_empty_path(catalog):
    path = RelationshipGraph(catalog).find_path("dns_logs", lambda s: s == "dns_logs")
    assert path.edges == [] and path.confidence == 1.0


def test_graph_respects_allowed(catalog):
    graph = RelationshipGraph(catalog)
    assert graph.find_path("proxy_logs", lambda s: s == "dns_logs", allowed=lambda s: s != "asset_inventory") is None


def test_graph_ignores_non_joinable_relationships(catalog):
    graph = RelationshipGraph(catalog)
    assert all(e.type != "represents" for e in graph.neighbors("asset_inventory"))


# ---------------------------------------------------------------------------
# Logical plan (structure) + validator (semantics)
# ---------------------------------------------------------------------------


def _plan(**overrides):
    base = dict(
        select=["firewall_logs.src_ip"],
        sources=["firewall_logs"],
        filters=[Filter(field="firewall_logs.dst_ip", operator="eq", value="10.0.0.5")],
        distinct=True,
    )
    base.update(overrides)
    return LogicalQueryPlan(**base)


def test_valid_plan_has_no_issues(catalog):
    assert QueryValidator(catalog).issues(_plan()) == []


@pytest.mark.parametrize("bad", [
    {"select": ["firewall_logs.src_ip; DROP TABLE x"]},
    {"select": ["src_ip"]},
    {"limit": 0},
    {"limit": 10**9},
    {"select": ["proxy_logs.username"]},  # source not listed
    {"sources": ["firewall_logs", "firewall_logs"]},
    {"filters": [{"field": "firewall_logs.dst_ip", "operator": "like", "value": "x"}]},
    {"filters": [{"field": "firewall_logs.dst_ip", "operator": "in", "value": "x"}]},
])
def test_plan_structure_is_enforced_by_pydantic(bad):
    with pytest.raises(ValidationError):
        _plan(**bad)


def test_time_range_needs_consistent_bounds():
    with pytest.raises(ValidationError):
        TimeRangeFilter(field="firewall_logs.timestamp")
    with pytest.raises(ValidationError):
        TimeRangeFilter(field="firewall_logs.timestamp", last_hours=1, start=datetime(2026, 1, 1))


@pytest.mark.parametrize("overrides, issue", [
    ({"select": ["firewall_logs.nope"]}, "unknown field 'firewall_logs.nope'"),
    ({"filters": [Filter(field="firewall_logs.dst_port", operator="contains", value="2")]}, "needs a string field"),
    ({"filters": [Filter(field="firewall_logs.src_ip", operator="gt", value="1")]}, "needs an ordered field"),
    ({"filters": [Filter(field="firewall_logs.dst_port", operator="eq", value="22")]}, "doesn't fit integer"),
    ({"filters": [Filter(field="firewall_logs.dst_port", operator="eq", value=True)]}, "doesn't fit integer"),
    ({"time_range": TimeRangeFilter(field="firewall_logs.src_ip", last_hours=1)}, "not datetime"),
])
def test_validator_semantic_issues(catalog, overrides, issue):
    issues = QueryValidator(catalog).issues(_plan(**overrides))
    assert any(issue in i for i in issues), issues


def test_validator_rejects_unknown_and_unauthorized_sources(catalog):
    plan = _plan(sources=["firewall_logs", "asset_inventory"],
                 joins=[Join(left="firewall_logs.src_ip", right="asset_inventory.ip_address")])
    assert QueryValidator(catalog).issues(plan) == []
    issues = QueryValidator(catalog, allowed_sources=["firewall_logs"]).issues(plan)
    assert issues == ["source 'asset_inventory' is not authorized"]
    with pytest.raises(PlanValidationError):
        QueryValidator(catalog).validate(_plan(select=["ghost.col"], sources=["ghost"], filters=[]))


def test_validator_rejects_joins_without_a_relationship(catalog):
    plan = _plan(sources=["firewall_logs", "auth_logs"],
                 joins=[Join(left="firewall_logs.src_ip", right="auth_logs.source_ip")])
    assert any("no joinable relationship" in i for i in QueryValidator(catalog).issues(plan))


def test_validator_rejects_disconnected_sources(catalog):
    plan = _plan(sources=["firewall_logs", "asset_inventory"])
    assert any("not connected" in i for i in QueryValidator(catalog).issues(plan))


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


def test_render_literal_escapes_quotes():
    assert render_literal("O'Brien") == "'O''Brien'"
    assert render_literal("x'); DROP TABLE t; --") == "'x''); DROP TABLE t; --'"
    assert render_literal(True) == "TRUE" and render_literal(3) == "3"
    assert render_literal(datetime(2026, 1, 2, 3, 4, 5)) == "TIMESTAMP '2026-01-02 03:04:05'"
    with pytest.raises(ValueError):
        render_literal(float("inf"))


def test_compiled_sql_runs_and_hostile_values_stay_values(catalog):
    conn = duckdb.connect()
    conn.execute("CREATE TABLE fw AS SELECT * FROM (VALUES "
                 "(TIMESTAMP '2026-09-24 11:00:00', '10.0.0.1', 'x''); DROP TABLE fw; --', 443, 'DENY'), "
                 "(TIMESTAMP '2026-09-20 11:00:00', '10.0.0.2', '10.0.0.5', 22, 'DENY')"
                 ") t(timestamp, src_ip, dst_ip, dst_port, action)")
    plan = _plan(
        filters=[
            Filter(field="firewall_logs.dst_ip", operator="eq", value="x'); DROP TABLE fw; --"),
            Filter(field="firewall_logs.dst_port", operator="in", value=[443, 8443]),
        ],
        time_range=TimeRangeFilter(field="firewall_logs.timestamp", last_hours=24),
    )
    sql = compile_plan(plan, catalog, {"firewall_logs": "fw"}, NOW)
    assert conn.sql(sql).fetchall() == [("10.0.0.1",)]
    assert conn.sql("SELECT count(*) FROM fw").fetchone() == (2,)


def test_compiler_maps_physical_columns_and_orders_joins(catalog):
    data = catalog.model_dump(by_alias=True)
    data["sources"]["asset_inventory"]["fields"]["hostname"]["column"] = "host_name"
    cat = Catalog.model_validate(data)
    plan = LogicalQueryPlan(
        select=["asset_inventory.hostname"],
        sources=["firewall_logs", "asset_inventory"],
        joins=[Join(left="firewall_logs.src_ip", right="asset_inventory.ip_address")],
        distinct=True,
    )
    sql = compile_plan(plan, cat, {"firewall_logs": "fw", "asset_inventory": "assets"}, NOW)
    assert '"host_name" AS "hostname"' in sql
    assert 'INNER JOIN "asset_inventory" ON "firewall_logs"."src_ip" = "asset_inventory"."ip_address"' in sql
    assert "SELECT DISTINCT" in sql and sql.rstrip().endswith("LIMIT 1000")

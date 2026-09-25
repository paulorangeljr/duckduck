import json
import warnings

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.semantic import (
    Catalog,
    JEVAdapter,
    SemanticSearch,
    evaluate,
    load_dataset,
)
from semantic_helpers import CATALOG_PATH, DATASET_PATH, NOW, sample_sources


@pytest.fixture
def duck():
    d = DuckAPI()
    sample_sources.register_sample_sources(d, NOW)
    yield d
    d.close()


@pytest.fixture
def search(duck):
    return SemanticSearch(CATALOG_PATH, duck, clock=lambda: NOW)


# ---------------------------------------------------------------------------
# MVP question set, end to end
# ---------------------------------------------------------------------------


def test_mvp_question_set_scores_perfectly(search):
    report = evaluate(search, load_dataset(DATASET_PATH))
    failures = [c for c in report.cases if c.status != "ok" or not all(
        v is not False for v in (c.source_ok, c.entity_ok, c.activity_ok, c.answer_ok)
    )]
    assert not failures, failures
    metrics = report.metrics
    assert {k: v for k, v in metrics.items() if k != "mean_latency_ms"} == {
        "source_accuracy": 1.0, "entity_accuracy": 1.0, "activity_accuracy": 1.0,
        "plan_validity": 1.0, "execution_success": 1.0, "answer_accuracy": 1.0,
    }


def test_github_question_matches_the_architecture_doc(search):
    result = search.search("Which users accessed github in the last 24hrs?")
    plan = result.query_plan
    assert plan.select == ["proxy_logs.username"] and plan.distinct
    assert [(f.field, f.operator, f.value) for f in plan.filters] == [
        ("proxy_logs.destination_domain", "contains", "github")
    ]
    assert plan.time_range.field == "proxy_logs.timestamp" and plan.time_range.last_hours == 24
    assert "TIMESTAMP '2026-09-23 12:00:00'" in result.sql
    assert sorted(result.results["username"]) == ["alice", "bob"]


def test_join_path_through_asset_inventory(search):
    result = search.search("Which machines communicated with 203.0.113.9?")
    assert [(j.left, j.right) for j in result.query_plan.joins] == [
        ("firewall_logs.src_ip", "asset_inventory.ip_address")
    ]
    assert [f.field for f in result.query_plan.filters] == ["firewall_logs.dst_ip"]
    kinds = [d.kind for d in result.decisions]
    assert "relationship_relevance" in kinds and "field_relevance" in kinds


def test_every_decision_is_recorded_with_its_probability(search):
    result = search.search("Show me denied connections from production machines.")
    assert all(0.0 <= d.probability <= 1.0 for d in result.decisions)
    assert {d.kind for d in result.decisions} >= {
        "entity", "activity", "source_relevance", "field_relevance", "relationship_relevance"
    }


# ---------------------------------------------------------------------------
# Per-source push-down
# ---------------------------------------------------------------------------


def test_eq_filters_push_down_per_source(search):
    result = search.search("Show me denied connections from production machines.")
    fetches = {f.source: f for f in result.fetches}
    assert fetches["firewall_logs"].kwargs == {"action": "DENY"}
    assert fetches["asset_inventory"].kwargs == {"environment": "production"}
    # a join: capping either side before joining could drop valid rows
    assert not any(f.limit_pushed for f in result.fetches)


def _spy_auth_logs(duck):
    calls = []
    original = duck.functions["auth_logs"]

    def spy(outcome=None, limit=None):
        calls.append({"outcome": outcome, "limit": limit})
        return original(outcome=outcome, limit=limit)

    duck.register_api_function("auth_logs", spy)
    return calls


def test_limit_never_pushes_down_under_an_order_by(duck, search):
    calls = _spy_auth_logs(duck)
    result = search.search("Show me failed authentication attempts")
    # row-level results come back newest first — the API's first 1000 rows
    # wouldn't be the newest 1000, so the cap can't go to the source
    assert "ORDER BY CAST" in result.sql
    assert calls[-1] == {"outcome": "failure", "limit": None}


def test_limit_pushes_down_only_when_every_filter_did(duck):
    data = Catalog.load(CATALOG_PATH).model_dump(by_alias=True)
    auth = data["sources"]["auth_logs"]
    auth.update(time_field=None)
    del auth["fields"]["timestamp"]  # unordered: no time field to sort by
    search = SemanticSearch(Catalog.model_validate(data), duck, clock=lambda: NOW)
    calls = _spy_auth_logs(duck)

    search.search("Show me failed authentication attempts")
    assert calls[-1] == {"outcome": "failure", "limit": 1000}
    result = search.search("Show me failed authentication attempts from user bob")
    assert calls[-1] == {"outcome": "failure", "limit": None}  # username filter is residual
    assert result.results["username"].tolist() == ["bob"]


def test_untypeable_value_asks_instead_of_being_dropped(search):
    result = search.search("Show me failed authentication attempts from alice")
    assert result.status == "needs_clarification"
    assert "'user alice'" in result.clarification


def test_word_before_a_value_types_it(search):
    result = search.search("Show me failed authentication attempts from user bob")
    assert result.status == "ok"
    assert [(f.field, f.value) for f in result.query_plan.filters] == [
        ("auth_logs.username", "bob"), ("auth_logs.outcome", "failure")
    ]


def test_contains_filters_are_residual_not_pushed(search):
    result = search.search("Which users accessed github in the last 24hrs?")
    fetch = result.fetches[0]
    assert fetch.kwargs == {} and "proxy_logs.destination_domain" in fetch.residual_filters


def test_source_returning_no_rows_yields_empty_result(duck):
    duck.register_api_function("auth_logs", lambda outcome=None, limit=None: [])
    result = SemanticSearch(CATALOG_PATH, duck, clock=lambda: NOW).search(
        "Which users generated failed authentication events?"
    )
    assert result.status == "ok" and result.results.empty
    assert list(result.results.columns) == ["username"]


def test_catalog_out_of_sync_with_data_is_a_clear_error(duck):
    duck.register_api_function("auth_logs", lambda outcome=None, limit=None: [{"user": "x", "outcome": "failure"}])
    with pytest.raises(RuntimeError, match="out of sync"):
        SemanticSearch(CATALOG_PATH, duck, clock=lambda: NOW).search(
            "Which users generated failed authentication events?"
        )


def test_relation_backed_source_scans_natively(duck, tmp_path):
    parquet = tmp_path / "proxy.parquet"
    rows = pd.DataFrame(sample_sources.build_sources(NOW)["proxy_logs"]())
    duck.conn.register("_rows", rows)
    duck.conn.execute(f"COPY _rows TO '{parquet}' (FORMAT parquet)")
    data = Catalog.load(CATALOG_PATH).model_dump(by_alias=True)
    data["sources"]["proxy_logs"].update(table=None, relation=f"read_parquet('{parquet}')")
    search = SemanticSearch(Catalog.model_validate(data), duck, clock=lambda: NOW)
    result = search.search("Which users accessed github in the last 24hrs?")
    assert sorted(result.results["username"]) == ["alice", "bob"]
    assert result.fetches[0].scan.startswith("read_parquet(")


# ---------------------------------------------------------------------------
# Availability / authorization / clarification
# ---------------------------------------------------------------------------


def test_unregistered_source_is_skipped_with_a_warning():
    duck = DuckAPI()
    for name, fn in sample_sources.build_sources(NOW).items():
        if name != "dns_logs":
            duck.register_api_function(name, fn)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        search = SemanticSearch(CATALOG_PATH, duck, clock=lambda: NOW)
    assert any("dns_logs" in str(w.message) for w in caught)
    assert "dns_logs" not in search.catalog.sources
    assert search.search("Which users accessed github in the last 24hrs?").status == "ok"


def test_strict_mode_raises_on_unregistered_source():
    with pytest.raises(ValueError, match="dns_logs"):
        SemanticSearch(CATALOG_PATH, DuckAPI(), strict=True)


def test_unauthorized_join_source_needs_clarification(duck):
    search = SemanticSearch(CATALOG_PATH, duck, clock=lambda: NOW,
                            allowed_sources=["firewall_logs", "proxy_logs"])
    result = search.search("Which machines communicated with 203.0.113.9?")
    assert result.status == "needs_clarification"
    assert result.results is None


@pytest.mark.parametrize("question", [
    "How is the weather?",
    "Which users accessed github or gitlab?",
])
def test_low_confidence_is_never_executed(search, question):
    result = search.search(question)
    assert result.status == "needs_clarification"
    assert result.clarification and result.results is None and result.fetches == []


def test_plan_only_mode_does_not_fetch(search, duck):
    calls = []
    duck.register_api_function("auth_logs", lambda outcome=None, limit=None: calls.append(1) or [])
    result = search.plan("Which users generated failed authentication events?")
    assert result.status == "planned" and result.sql and calls == []


def test_to_dict_is_json_serializable(search):
    payload = search.search("Show me denied connections from production machines.").to_dict()
    text = json.dumps(payload)
    assert payload["status"] == "ok" and len(payload["results"]) == 2
    assert set(payload) >= {"intent", "sources", "query_plan", "execution", "results"}
    assert "firewall_logs" in text


# ---------------------------------------------------------------------------
# Real JEV plugged in through the adapter
# ---------------------------------------------------------------------------


class ScriptedJEV:
    """Stands in for the real JEV service: very sure about everything."""

    def decide(self, state, question, options):
        return {"YES": 0.99, "NO": 0.01}

    def classify(self, state, question, options):
        wanted = {"What entity is the user asking for?": "user",
                  "What activity is being investigated?": "authentication"}.get(question)
        return {label: (0.97 if label == wanted else 0.01) for label in options}


def test_search_runs_on_a_jev_adapter(duck):
    search = SemanticSearch(CATALOG_PATH, duck, engine=JEVAdapter(ScriptedJEV(), backoff=0), clock=lambda: NOW)
    result = search.search("Which users generated failed authentication events?")
    assert result.status == "ok"
    assert sorted(result.results["username"]) == ["bob", "erin"]
    entity = next(d for d in result.decisions if d.kind == "entity")
    assert entity.answer == "user" and entity.probability == pytest.approx(0.97 / (0.97 + 0.01 * 4))


# ---------------------------------------------------------------------------
# DuckAPI.fetch
# ---------------------------------------------------------------------------


def test_duckapi_fetch_validates_and_allows_empty(duck):
    assert len(duck.fetch("firewall_logs", action="DENY")) == 3
    duck.register_api_function("nothing", lambda: [])
    assert duck.fetch("nothing").empty
    with pytest.raises(ValueError, match="Invalid call"):
        duck.fetch("firewall_logs", bogus=1)
    with pytest.raises(KeyError):
        duck.fetch("missing")


def test_contains_and_time_range_push_down_through_the_same_convention(duck, search):
    calls = []
    rows = sample_sources.build_sources(NOW)["proxy_logs"]()

    def proxy_logs(destination_domain_ilike=None, timestamp_gte=None, limit=None):
        calls.append({"destination_domain_ilike": destination_domain_ilike, "timestamp_gte": timestamp_gte})
        return rows

    duck.register_api_function("proxy_logs", proxy_logs)
    result = search.search("Which users accessed github in the last 24hrs?")
    assert calls[-1] == {"destination_domain_ilike": "%github%", "timestamp_gte": "2026-09-23 12:00:00"}
    fetch = result.fetches[0]
    assert fetch.residual_filters == [] and sorted(fetch.pushed_filters) == [
        "proxy_logs.destination_domain", "proxy_logs.timestamp"
    ]
    assert sorted(result.results["username"]) == ["alice", "bob"]  # DuckDB still re-applies both


def test_to_json_handles_dates_and_results_only(search):
    import json

    import pandas as pd

    result = search.search("Which users accessed github in the last 24hrs?")
    assert result.status == "ok"
    result.results["seen_at"] = pd.Timestamp("2026-09-24T10:00:00")  # a date in the answer
    with pytest.raises(TypeError):
        json.dumps(result.to_dict())  # what to_json exists for
    full = json.loads(result.to_json())
    assert full["status"] == "ok" and full["results"]
    assert full["results"][0]["seen_at"] == "2026-09-24T10:00:00"
    rows = json.loads(result.to_json(results_only=True))
    assert rows == full["results"] and all(isinstance(r, dict) for r in rows)



# ---------------------------------------------------------------------------
# "how many": a count, not a list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("question, expected", [
    ("How many users accessed github in the last 24hrs?", 2),      # distinct entities
    ("How many failed authentication events?", 2),                 # records
    ("Count the denied connections", 3),
])
def test_how_many_answers_with_a_count(search, question, expected):
    result = search.search(question)
    assert result.status == "ok", result.report()
    assert list(result.results.columns) == ["count"] and result.results["count"].tolist() == [expected]
    assert result.query_plan.aggregate == "count"
    shape = next(d for d in result.decisions if d.kind == "answer_shape")
    assert shape.answer == "count" and shape.decided_by == "deterministic"


def test_the_count_matches_the_list(search):
    listed = search.search("Which users accessed github in the last 24hrs?").results
    counted = search.search("How many users accessed github in the last 24hrs?").results
    assert counted["count"].tolist() == [listed["username"].nunique()]


def test_a_count_counts_distinct_things_and_never_limits_at_the_source(search):
    result = search.search("How many users accessed github in the last 24hrs?")
    assert 'SELECT COUNT(*) AS "count" FROM (\n  SELECT DISTINCT' in result.sql and "LIMIT" not in result.sql
    assert not any(f.limit_pushed for f in result.fetches)


def test_a_list_question_stays_a_list(search):
    result = search.search("Which users accessed github in the last 24hrs?")
    assert result.query_plan.aggregate is None and "answer_shape" not in [d.kind for d in result.decisions]


def test_the_shape_can_be_pinned(search):
    result = search.search("Which users accessed github in the last 24hrs?", pinned={"answer_shape": "count"})
    assert result.results["count"].tolist() == [2]

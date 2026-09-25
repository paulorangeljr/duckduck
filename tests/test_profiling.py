"""Column profiles computed at catalog generation: stats, date range, category values, examples."""

import json

import pandas as pd
import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, CatalogGenerator  # noqa: E402
from duckduck.semantic.generation import GenField, GenSource, GenVocabulary  # noqa: E402
from duckduck.semantic.profiling import profile_frame  # noqa: E402

N = 200
ALERTS = pd.DataFrame({
    "ip": [f"10.0.{i // 250}.{i % 250}" for i in range(N)],
    "rule": ["brute_force", "port_scan", "malware", "dns_tunnel"] * (N // 4),
    "severity": ["low", "critical"] * (N // 2),
    "owner": [f"user{i % 40}" for i in range(N)],
    "ts": [f"2026-09-{1 + i % 20:02d}T10:00:00Z" for i in range(N)],
    "score": list(range(N)),
    "comment": [None] * (N - 10) + ["checked"] * 10,
})


def test_profile_frame():
    stats = profile_frame(ALERTS)
    assert stats["rule"]["values"] == ["brute_force", "dns_tunnel", "malware", "port_scan"]
    assert "values" not in stats["ip"]  # 200 distinct: not a category
    assert stats["ts"]["min"].startswith("2026-09-01") and stats["ts"]["max"].startswith("2026-09-20")
    assert stats["score"]["min"] == "0" and stats["score"]["max"] == str(N - 1) and "values" not in stats["score"]
    assert stats["comment"]["null_ratio"] == 0.95 and stats["comment"]["distinct"] == 1
    assert stats["rule"]["examples"][:2] == ["brute_force", "port_scan"]


def test_a_tiny_table_is_not_turned_into_value_lists():
    assert "values" not in profile_frame(ALERTS.head(5))["rule"]


class Describer:
    """Types fields by name, marks no values — like an LLM that missed the categories."""

    def __init__(self):
        self.prompts = []

    def generate(self, system, prompt, output_model):
        if output_model is GenVocabulary:
            return GenVocabulary()
        profile = json.loads(prompt.split("Table profile:\n", 1)[1])
        self.prompts.append(profile)
        types = {"ip": "ip_address", "owner": "user", "ts": "event_time"}
        return GenSource(description="Alerts.", time_field="ts", fields=[
            GenField(name=c, semantic_type=types.get(c), type="datetime" if c == "ts" else
                     ("integer" if c == "score" else "string")) for c in profile["columns"]
        ])


def _generate(**kw):
    duck = DuckAPI()
    duck.register_api_function("alerts", lambda limit=None: ALERTS.head(limit) if limit else ALERTS)
    llm = Describer()
    return CatalogGenerator(llm, duck, **kw).generate(), llm


def test_generation_stores_profiles_and_fills_category_values():
    result, llm = _generate()
    src = result.catalog.sources["alerts"]
    assert set(src.fields["rule"].values) == {"brute_force", "port_scan", "malware", "dns_tunnel"}
    assert set(src.fields["severity"].values) == {"low", "critical"}
    assert not src.fields["ip"].values and not src.fields["owner"].values  # identifiers never become lists
    assert src.fields["comment"].profile.null_ratio == 0.95
    assert src.fields["score"].profile.min == "0"
    assert src.profile.rows_sampled == N and src.profile.time_min.startswith("2026-09-01")
    assert all(not f.profile.examples for f in src.fields.values())  # no real values stored by default


def test_the_llm_sees_stats_but_value_lists_only_when_data_may_be_sent():
    _, llm = _generate()
    stats = llm.prompts[0]["column_stats"]
    assert stats["rule"]["distinct"] == 4 and stats["rule"]["values"] and "examples" not in stats["rule"]
    _, llm = _generate(sample_rows=0)
    assert "values" not in llm.prompts[0]["column_stats"]["rule"] and llm.prompts[0]["sample_rows"] == []


def test_example_values_are_opt_in_and_skip_personal_data():
    result, _ = _generate(sample_values=2)
    fields = result.catalog.sources["alerts"].fields
    assert fields["ip"].profile.examples == ["10.0.0.0", "10.0.0.1"]
    assert not fields["owner"].profile.examples
    result, _ = _generate(sample_values=2, sample_sensitive=True)
    assert result.catalog.sources["alerts"].fields["owner"].profile.examples == ["user0", "user1"]


def test_profile_rows_zero_disables_profiles():
    result, llm = _generate(profile_rows=0)
    src = result.catalog.sources["alerts"]
    assert src.profile is None and all(f.profile is None for f in src.fields.values())
    assert not src.fields["rule"].values and llm.prompts[0]["column_stats"] == {}


def test_profiles_reach_the_decision_engine(tmp_path):
    result, _ = _generate(sample_values=2)
    path = tmp_path / "catalog.yaml"
    result.write(str(path))
    catalog = Catalog.load(str(path))
    assert "E.g. '10.0.0.0', '10.0.0.1'." in catalog.describe_field("alerts.ip")
    assert "Ranges from 0 to 199." in catalog.describe_field("alerts.score")
    assert "Mostly empty (95% of sampled rows)." in catalog.describe_field("alerts.comment")
    assert "Known values: 'brute_force'" in catalog.describe_field("alerts.rule")
    assert "Sampled records span 2026-09-01" in catalog.describe_source("alerts")

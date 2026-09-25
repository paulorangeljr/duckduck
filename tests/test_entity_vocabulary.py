"""Catalog generation tidies the vocabulary from the data: value shapes, orphan entities, field-name keywords."""

import json

import pandas as pd
import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, CatalogGenerator  # noqa: E402
from duckduck.semantic.generation import GenEntity, GenField, GenSource, GenVocabulary  # noqa: E402
from duckduck.semantic.profiling import profile_frame  # noqa: E402

N = 40
ALERTS = pd.DataFrame({
    "src_ip": [f"10.0.0.{i}" for i in range(N)],
    "reporter": [f"user{i}@corp.example.com" for i in range(N)],
    "site": [f"https://portal{i}.example.com/login" for i in range(N)],
    "domain": [f"svc{i}.example.org" for i in range(N)],
    "name": [f"web-{i}" for i in range(N)],
    "score": list(range(N)),
})


def test_value_shapes():
    stats = profile_frame(ALERTS)
    assert {c: stats[c].get("shape") for c in ALERTS.columns} == {
        "src_ip": "ip_address", "reporter": "email", "site": "url", "domain": "domain", "name": None, "score": None,
    }
    assert profile_frame(pd.DataFrame({"ip": ["fe80::1", "2001:db8::/32"] * 10}))["ip"]["shape"] == "ip_address"
    mostly = pd.DataFrame({"ip": [f"10.0.0.{i}" for i in range(8)] + ["n/a", "unknown"]})
    assert "shape" not in profile_frame(mostly)["ip"]  # 80% isn't "every value"


class LLM:
    """Types only what it's told to; the vocabulary invents a 'host' entity no field holds."""

    def __init__(self, types=None, entities=None, claims=("host",)):
        self.types = types or {}
        self.entities = entities
        self.claims = list(claims)

    def generate(self, system, prompt, output_model):
        if output_model is GenVocabulary:
            return GenVocabulary(entities=self.entities if self.entities is not None else [
                GenEntity(name="ip_address", description="An IP address.", keywords=["ip", "ips"]),
                GenEntity(name="host", description="A machine on the network, identified by its IP address.",
                          keywords=["hosts", "machine", "machines"]),
            ])
        profile = json.loads(prompt.split("Table profile:\n", 1)[1])
        return GenSource(description="Alerts.", entities=self.claims, fields=[
            GenField(name=c, semantic_type=self.types.get(c)) for c in profile["columns"]
        ])


def _generate(llm, existing=None, **kw):
    duck = DuckAPI()
    duck.register_api_function("alerts", lambda limit=None: ALERTS.head(limit) if limit else ALERTS)
    return CatalogGenerator(llm, duck, **kw).generate(existing=existing, force=existing is not None)


def test_the_shape_types_a_field_the_llm_left_untyped():
    catalog = _generate(LLM(types={"reporter": "user"})).catalog
    fields = catalog.sources["alerts"].fields
    assert fields["src_ip"].semantic_type == "ip_address" and fields["src_ip"].profile.shape == "ip_address"
    assert fields["reporter"].semantic_type == "user"  # the LLM's reading wins
    assert fields["site"].semantic_type == "url" and fields["name"].semantic_type is None


def test_an_entity_no_field_holds_is_merged_into_the_one_its_description_names():
    result = _generate(LLM())
    catalog = result.catalog
    assert "host" not in catalog.entities
    keywords = catalog.entities["ip_address"].keywords
    assert {"ip", "ips", "host", "hosts", "machine", "machines"} <= set(keywords)
    assert catalog.sources["alerts"].entities == ["ip_address"]
    assert any("'host'" in w and "merged into 'ip_address'" in w for w in result.warnings)


def test_without_a_description_the_claiming_sources_pick_the_target():
    entities = [GenEntity(name="ip_address", description="An address."),
                GenEntity(name="user", description="A person."),
                GenEntity(name="asset", description="Something we own.")]
    catalog = _generate(LLM(entities=entities, claims=["asset"])).catalog
    # alerts claims 'asset' and holds ip_address (src_ip) — no user field typed
    assert "asset" not in catalog.entities and "asset" in catalog.entities["ip_address"].keywords


def test_an_orphan_with_nothing_to_merge_into_is_kept_and_reported():
    entities = [GenEntity(name="vendor", description="A supplier.")]
    result = _generate(LLM(entities=entities, claims=["vendor"]))
    assert "vendor" in result.catalog.entities
    assert any("'vendor': no field holds it — questions about it will be asked back" in w for w in result.warnings)


def test_field_names_become_keywords_of_their_entity():
    catalog = _generate(LLM()).catalog
    assert "src ip" in catalog.entities["ip_address"].keywords


def test_row_level_entities_are_never_merged():
    entities = [GenEntity(name="ip_address", description="An IP address."),
                GenEntity(name="alert", description="An alert raised about an IP address.", row_level=True)]
    catalog = _generate(LLM(entities=entities, claims=["alert"])).catalog
    assert "alert" in catalog.entities


def test_an_existing_orphan_is_merged_on_the_next_redraft():
    data = _generate(LLM()).catalog.model_dump(by_alias=True)
    data["entities"]["host"] = {"description": "A machine, i.e. an IP address.", "keywords": ["server", "servers"]}
    data["sources"]["alerts"]["entities"] = ["ip_address", "host"]
    existing = Catalog.model_validate(data)
    catalog = _generate(LLM(entities=[]), existing=existing).catalog
    assert "host" not in catalog.entities and {"host", "server", "servers"} <= set(catalog.entities["ip_address"].keywords)

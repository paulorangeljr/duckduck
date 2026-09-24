import copy

import pytest
import yaml
from pydantic import ValidationError

from duckduck.semantic import Catalog, LexicalRetriever
from semantic_helpers import CATALOG_PATH


def _raw():
    with open(CATALOG_PATH) as f:
        return yaml.safe_load(f)


def test_example_catalog_loads():
    catalog = Catalog.load(CATALOG_PATH)
    assert set(catalog.sources) == {"proxy_logs", "dns_logs", "firewall_logs", "auth_logs", "asset_inventory"}
    assert catalog.sources["proxy_logs"].resolved_time_field == "timestamp"
    assert catalog.sources["asset_inventory"].resolved_time_field is None


def test_relationship_from_alias_parses():
    catalog = Catalog.load(CATALOG_PATH)
    assert catalog.relationships[0].from_ == "proxy_logs.source_ip"


def test_entity_fields_combines_semantic_type_and_represents():
    catalog = Catalog.load(CATALOG_PATH)
    users = catalog.entity_fields("user")
    assert users["proxy_logs.username"] == 1.0
    assert users["asset_inventory.owner"] == 1.0  # semantic_type wins over the 0.99 relationship


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["relationships"].append({"from": "proxy_logs.nope", "to": "asset_inventory.ip_address", "type": "same_entity", "confidence": 0.9}),
     "not an existing source.field"),
    (lambda d: d["relationships"].append({"from": "proxy_logs.source_ip", "to": "user", "type": "same_entity", "confidence": 0.9}),
     "needs a source.field on both sides"),
    (lambda d: d["sources"]["proxy_logs"]["activities"].append("teleportation"), "unknown activity"),
    (lambda d: d["sources"]["proxy_logs"]["entities"].append("spaceship"), "unknown entity"),
    (lambda d: d["activities"]["web_access"].update(resource="mac_address"), "no field has semantic_type"),
])
def test_bad_cross_references_fail_at_load(mutate, message):
    data = copy.deepcopy(_raw())
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        Catalog.model_validate(data)


def test_identifier_injection_is_rejected():
    data = copy.deepcopy(_raw())
    data["sources"]["proxy_logs"]["fields"]['x" ; DROP TABLE t; --'] = {"type": "string"}
    with pytest.raises(ValidationError):
        Catalog.model_validate(data)


def test_source_needs_exactly_one_binding():
    data = copy.deepcopy(_raw())
    data["sources"]["proxy_logs"]["relation"] = "read_parquet('x.parquet')"
    with pytest.raises(ValidationError, match="exactly one of 'table' or 'relation'"):
        Catalog.model_validate(data)


def test_time_field_must_be_datetime():
    data = copy.deepcopy(_raw())
    data["sources"]["proxy_logs"]["time_field"] = "username"
    with pytest.raises(ValidationError, match="must be datetime"):
        Catalog.model_validate(data)


def test_unknown_keys_are_rejected():
    data = copy.deepcopy(_raw())
    data["sources"]["proxy_logs"]["fields"]["username"]["semantc_type"] = "user"  # typo
    with pytest.raises(ValidationError):
        Catalog.model_validate(data)


def test_json_catalog_loads(tmp_path):
    import json

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(_raw(), default=str))
    assert "proxy_logs" in Catalog.load(str(path)).sources


def test_describe_source_includes_keywords():
    text = Catalog.load(CATALOG_PATH).describe_source("dns_logs")
    assert "dns_resolution (query, queried" in text
    assert "query_name (domain: Name being resolved)" in text


def test_lexical_retriever_ranks_the_obvious_source_first():
    retriever = LexicalRetriever(Catalog.load(CATALOG_PATH))
    assert retriever.search("who logged in with a failed sign-in", top_k=2)[0][0] == "auth_logs"
    assert retriever.search("dns lookups for a domain", top_k=1)[0][0] == "dns_logs"
    assert len(retriever.search("anything", top_k=3)) == 3

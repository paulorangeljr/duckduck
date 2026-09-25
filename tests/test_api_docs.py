"""API documentation as catalog-generation context: OpenAPI specs, hand-written field docs, text."""

import json

import pandas as pd
import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import Catalog, CatalogGenerator, SemanticConfig  # noqa: E402
from duckduck.semantic.apidocs import ApiDocs, DocsRef, html_to_text  # noqa: E402
from duckduck.semantic.generation import GenField, GenSource, GenVocabulary  # noqa: E402

SPEC = {
    "openapi": "3.0.1",
    "info": {"title": "InsightVM API"},
    "paths": {
        "/api/3/assets": {"get": {
            "operationId": "getAssets", "summary": "Assets", "description": "Returns all assets the scanner knows.",
            "parameters": [{"$ref": "#/components/parameters/page"},
                           {"name": "sort", "in": "query", "description": "Sort order"}],
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/PageOfAsset"}}}}},
        }},
        "/api/3/assets/{id}": {"get": {"operationId": "getAsset", "summary": "Asset",
                                       "responses": {"200": {"content": {"application/json": {
                                           "schema": {"$ref": "#/components/schemas/Asset"}}}}}}},
        "/api/3/sites": {"get": {"operationId": "getSites", "summary": "Sites", "responses": {}}},
    },
    "components": {
        "parameters": {"page": {"name": "page", "in": "query", "description": "Page index"}},
        "schemas": {
            "PageOfAsset": {"type": "object", "properties": {
                "resources": {"type": "array", "items": {"$ref": "#/components/schemas/Asset"}},
                "page": {"type": "object", "properties": {"number": {"type": "integer"}}},
                "links": {"type": "array", "items": {"type": "object"}},
            }},
            "Asset": {"allOf": [{"$ref": "#/components/schemas/Base"}, {"type": "object", "properties": {
                "ip": {"type": "string", "description": "The primary IPv4 or IPv6 address of the asset."},
                "hostName": {"type": "string", "description": "The primary host name of the asset."},
                "riskScore": {"type": "number", "format": "double", "description": "The risk score, with criticality adjustments."},
                "status": {"type": "string", "enum": ["active", "retired"], "description": "Lifecycle state."},
                "os": {"$ref": "#/components/schemas/OS"},
            }}]},
            "Base": {"type": "object", "properties": {"id": {"type": "integer", "description": "The identifier of the asset."}}},
            "OS": {"type": "object", "properties": {"name": {"type": "string", "description": "Operating system name."},
                                                    "family": {"type": "string", "enum": ["Windows", "Linux"]}}},
        },
    },
}

ASSETS = pd.DataFrame({
    "id": [1, 2], "ip": ["10.0.0.1", "10.0.0.2"], "hostName": ["web", "db"], "riskScore": [100.0, 5.0],
    "status": ["active", "active"], "os_name": ["Ubuntu", "Windows 11"], "os_family": ["Linux", "Windows"],
    "tag": ["x", "y"],
})
COLUMNS = list(ASSETS.columns)


class InsightVM:
    def assets(self, limit=None):
        """Assets known to the scanner."""
        return ASSETS.head(limit) if limit else ASSETS

    def sites(self, limit=None):
        return pd.DataFrame({"id": [1], "name": ["HQ"]})


def test_the_operation_behind_the_table_is_found_and_only_it_is_sent():
    docs = ApiDocs.from_content(SPEC)
    ctx, note = docs.for_table(["assets"], COLUMNS)
    assert note is None and ctx["operation"] == "GET /api/3/assets"  # the listing, not /assets/{id}
    assert ctx["summary"] == "Assets" and ctx["api"] == "InsightVM API"
    assert [p["name"] for p in ctx["parameters"]] == ["page", "sort"]
    fields = ctx["fields"]
    assert set(fields) == {"id", "ip", "hostName", "riskScore", "status", "os_name", "os_family"}  # not 'tag'
    assert fields["status"]["enum"] == ["active", "retired"]
    assert fields["os_family"]["enum"] == ["Windows", "Linux"]  # nested objects flattened like json_normalize
    assert fields["id"]["description"] == "The identifier of the asset."  # allOf + $ref
    assert fields["riskScore"] == {"type": "number", "format": "double",
                                   "description": "The risk score, with criticality adjustments."}


def test_named_operation_and_what_can_t_be_matched():
    docs = ApiDocs.from_content(SPEC)
    assert docs.for_table(["whatever"], COLUMNS, operation="GET /api/3/assets/{id}")[0]["operation"] == "GET /api/3/assets/{id}"
    assert docs.for_table(["x"], COLUMNS, operation="getAssets")[0]["operation"] == "GET /api/3/assets"
    ctx, note = docs.for_table(["x"], COLUMNS, operation="GET /nope")
    assert ctx is None and "isn't in the spec" in note
    ctx, note = docs.for_table(["vulnerabilities"], COLUMNS)
    assert ctx is None and 'name one with "operation"' in note


def test_swagger_2_and_yaml(tmp_path):
    swagger = {"swagger": "2.0", "paths": {"/devices": {"get": {"responses": {"200": {"schema": {
        "type": "array", "items": {"$ref": "#/definitions/Device"}}}}}}},
        "definitions": {"Device": {"properties": {"hostname": {"type": "string", "description": "DNS name"}}}}}
    path = tmp_path / "axonius.yaml"
    path.write_text(yaml.safe_dump(swagger))
    ctx, _ = ApiDocs.load("axonius.yaml", str(tmp_path)).for_table(["devices"], ["hostname", "adapter_count"])
    assert ctx["operation"] == "GET /devices" and ctx["fields"] == {"hostname": {"type": "string", "description": "DNS name"}}


def test_hand_written_field_docs_for_an_internal_api():
    doc = {"tables": {
        "tickets": {"description": "Help-desk tickets.", "notes": "Closed tickets are purged after 90 days.",
                    "fields": {"st": {"description": "Ticket state", "values": {"O": ["open"], "C": ["closed", "done"]}},
                               "prio": "Priority, 1 (highest) to 4"}},
        "agents": {"fields": {"name": "Agent name"}},
    }}
    ctx, note = ApiDocs.from_content(doc).for_table(["helpdesk_tickets", "tickets"], ["st", "prio", "opened"])
    assert note is None and ctx["table"] == "tickets" and ctx["notes"].startswith("Closed tickets")
    assert ctx["fields"] == {"st": {"description": "Ticket state", "values": {"O": ["open"], "C": ["closed", "done"]}},
                             "prio": {"description": "Priority, 1 (highest) to 4"}}
    ctx, note = ApiDocs.from_content(doc).for_table(["billing"], ["x"])
    assert ctx is None and "no entry in 'tables'" in note
    single = ApiDocs.from_content({"description": "Tickets.", "fields": {"st": "State"}})
    assert single.for_table(["t"], ["st"])[0] == {"from": "inline", "description": "Tickets.", "fields": {"st": {"description": "State"}}}


def test_a_json_file_that_is_neither_a_spec_nor_field_docs_is_refused(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps({"hello": 1}))
    with pytest.raises(ValueError, match="'fields' or 'tables'"):
        ApiDocs.load("x.json", str(tmp_path))
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(ValueError, match="isn't valid JSON"):
        ApiDocs.load("bad.json", str(tmp_path))


def test_text_docs_send_the_relevant_sections():
    filler = "Lorem ipsum dolor sit amet. " * 40
    text = (f"# Introduction\n{filler}\n# Authentication\n{filler}\n"
            f"# Tickets\nEach ticket has a `prio` from 1 to 4 and a state.\n# Billing\n{filler}\n")
    ctx, note = ApiDocs("guide.md", text).for_table(["tickets"], ["prio"], max_chars=500)
    assert note is None and ctx["excerpt"].startswith("# Tickets") and "Lorem" not in ctx["excerpt"]
    short = ApiDocs("short.md", "All about tickets.")
    assert short.for_table(["x"], [])[0]["excerpt"] == "All about tickets."  # short: sent whole
    assert ApiDocs("guide.md", text).for_table(["payroll"], ["salary"], max_chars=500)[0] is None


def test_html_becomes_sectioned_text():
    text = html_to_text("<html><head><style>p{}</style></head><body><h2>Tickets</h2><p>A&amp;B</p>"
                        "<table><tr><td>prio</td><td>1-4</td></tr></table><script>x()</script></body></html>")
    assert text.splitlines()[0] == "## Tickets" and "A&B" in text and "prio" in text and "x()" not in text
    assert "p{}" not in text


def test_docs_ref_needs_exactly_one_source():
    with pytest.raises(ValueError):
        DocsRef()
    with pytest.raises(ValueError):
        DocsRef(location="a.json", content={})


# ---------------------------------------------------------------------------
# Generation


class LLM:
    def __init__(self):
        self.profiles = []

    def generate(self, system, prompt, output_model):
        if output_model is GenVocabulary:
            return GenVocabulary()
        profile = json.loads(prompt.split("Table profile:\n", 1)[1])
        self.profiles.append(profile)
        return GenSource(description="drafted", fields=[
            GenField(name=c, description="" if c == "riskScore" else "d",
                     type="integer" if c == "id" else "string") for c in profile["columns"]
        ])


def _duck():
    duck = DuckAPI()
    api = InsightVM()
    duck.register_api_function("insightvm_assets", api.assets)
    duck.register_api_function("insightvm_sites", api.sites)
    duck.service_of.update({"insightvm_assets": "insightvm", "insightvm_sites": "insightvm"})
    return duck


def test_generation_sends_the_docs_and_applies_what_they_settle(tmp_path):
    (tmp_path / "insightvm.json").write_text(json.dumps(SPEC))
    llm = LLM()
    result = CatalogGenerator(llm, _duck(), api_docs={"insightvm": "insightvm.json"},
                              docs_base_dir=str(tmp_path)).generate()
    sent = {p["source_name"]: p.get("api_docs") for p in llm.profiles}
    assert sent["insightvm_assets"]["operation"] == "GET /api/3/assets"
    assert sent["insightvm_sites"]["operation"] == "GET /api/3/sites"
    src = result.catalog.sources["insightvm_assets"]
    assert src.api_docs == "GET /api/3/assets (insightvm.json)"
    assert set(src.fields["status"].values) == {"active", "retired"}  # the sample only had 'active'
    assert src.fields["riskScore"].description == "The risk score, with criticality adjustments."
    assert src.fields["hostName"].description == "d"  # the LLM's own description stays
    assert "api_docs: GET /api/3/assets (insightvm.json)" in result.to_yaml()


def test_the_most_specific_selector_wins_and_inline_docs_work():
    llm = LLM()
    docs = {
        "insightvm": {"content": SPEC},
        "insightvm:sites": {"content": {"description": "Scan sites.", "fields": {"name": "Site name"}}},
    }
    result = CatalogGenerator(llm, _duck(), api_docs=docs).generate()
    sent = {p["source_name"]: p.get("api_docs") for p in llm.profiles}
    assert sent["insightvm_sites"] == {"from": "inline (insightvm:sites)", "description": "Scan sites.",
                                       "fields": {"name": {"description": "Site name"}}}
    assert sent["insightvm_assets"]["from"] == "inline (insightvm)"
    assert result.catalog.sources["insightvm_sites"].api_docs == "inline (insightvm:sites)"


def test_values_with_synonyms_from_field_docs_reach_the_catalog():
    docs = {"insightvm:assets": {"content": {"fields": {"status": {"values": {"active": ["in use", "live"]}}}}}}
    src = CatalogGenerator(LLM(), _duck(), api_docs=docs).generate().catalog.sources["insightvm_assets"]
    assert src.fields["status"].values == {"active": ["in use", "live"]}


def test_unreadable_docs_are_a_warning_not_a_failure():
    llm = LLM()
    result = CatalogGenerator(llm, _duck(), api_docs={"insightvm": "missing.json"}).generate()
    assert set(result.catalog.sources) == {"insightvm_assets", "insightvm_sites"}
    assert sum("couldn't read api docs 'missing.json'" in w for w in result.warnings) == 1  # once, not per table
    assert all("api_docs" not in p for p in llm.profiles)


def test_config(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "tickets.json").write_text(json.dumps({"fields": {"st": "State"}}))
    cfg_file = tmp_path / "duckduck.json"
    cfg_file.write_text(json.dumps({"services": {}, "semantic": {"catalog_generation": {"api_docs": {
        "helpdesk": "docs/tickets.json",
        "insightvm": {"location": "https://example.com/api.json", "operation": "GET /api/3/assets"},
        "billing": {"content": {"fields": {"amount": "Amount in cents"}}},
    }}}}))
    cfg = SemanticConfig.load(DuckAPI(), str(cfg_file))
    gen = cfg.catalog_generation
    assert gen.api_docs["helpdesk"] == "docs/tickets.json" and gen.api_docs["insightvm"].operation == "GET /api/3/assets"
    bad = {"services": {}, "semantic": {"catalog_generation": {"api_docs": {"x": {"operation": "GET /a"}}}}}
    cfg_file.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="exactly one of 'location'"):
        SemanticConfig.load(DuckAPI(), str(cfg_file))


def test_build_generator_resolves_paths_against_the_config_file(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "insightvm.json").write_text(json.dumps(SPEC))
    cfg_file = tmp_path / "duckduck.json"
    cfg_file.write_text(json.dumps({"services": {}, "semantic": {"catalog_generation": {
        "api_docs": {"insightvm": "docs/insightvm.json"}}}}))
    monkeypatch.setattr(SemanticConfig, "build_llm", lambda self, duck, stage=None: LLM())
    monkeypatch.chdir("/")
    duck = _duck()
    gen = SemanticConfig.load(duck, str(cfg_file)).build_generator(duck)
    result = gen.generate()
    assert result.catalog.sources["insightvm_assets"].api_docs == "GET /api/3/assets (docs/insightvm.json)"


def test_the_catalog_round_trips_the_docs_label():
    data = {"sources": {"a": {"table": "a", "api_docs": "GET /a (x.json)", "fields": {"x": {}}}}}
    assert Catalog.model_validate(data).sources["a"].api_docs == "GET /a (x.json)"

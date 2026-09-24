"""Jev client, LLM client, LLM extraction, catalog generation, config wiring."""

import json
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from duckduck import DuckAPI
from duckduck.semantic import (
    Catalog,
    CatalogGenerator,
    ClaudeLLM,
    DecisionEngineError,
    DecisionState,
    JEVAdapter,
    JevAPIError,
    JevClient,
    LLMError,
    LLMExtractor,
    SemanticSearch,
    TableSpec,
)
from duckduck.semantic.generation import (
    GenActivity,
    GenEntity,
    GenEnumValue,
    GenField,
    GenRelationship,
    GenSource,
    GenVocabulary,
)
from duckduck.semantic.llm_extraction import LLMEnumValue, LLMExtractionOutput, LLMTimeRange, LLMValue
from semantic_helpers import CATALOG_PATH, NOW, sample_sources


# ---------------------------------------------------------------------------
# Jev HTTP client
# ---------------------------------------------------------------------------


def _jev(responses):
    """JevClient whose session.post returns ``responses`` in order."""
    session = MagicMock()
    session.headers = {}
    replies = []
    for status, body in responses:
        r = MagicMock()
        r.status_code, r.ok = status, status < 400
        r.json.return_value = body
        r.text = json.dumps(body)
        replies.append(r)
    session.post.side_effect = replies
    return JevClient(api_key="k", session=session, model="typesafe-ai/jev"), session


def _ok(answers):
    return 200, {"code": 0, "message": "ok", "data": {"answers": answers}}


def _sent(session, i=0):
    call = session.post.call_args_list[i]
    return call.args[0], json.loads(call.kwargs["data"])


def test_jev_needs_a_key(monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        JevClient()
    monkeypatch.setenv("JEV_API_KEY", "from-env")
    assert JevClient().session.headers["Authorization"] == "Bearer from-env"


def test_jev_decide_is_a_noul_question():
    client, session = _jev([_ok({"answer": {"noul": 0.8}})])
    out = client.decide({"query": "q", "subject": "proxy_logs: ...", "terms": ["x"], "prior": 0.9}, "Relevant?", ["YES", "NO"])
    assert out == {"YES": 0.8, "NO": pytest.approx(0.2)}
    url, body = _sent(session)
    assert url == "https://www.jevai.org/api/v1/decisions"
    assert body["model"] == "typesafe-ai/jev"
    assert body["questions"] == {"answer": {"type": "noul", "instructions": "Relevant?"}}
    assert body["state"] == {"user_question": "q", "subject": "proxy_logs: ...", "catalog_prior_probability": 0.9}
    assert session.headers["Authorization"] == "Bearer k"


def test_jev_classify_sends_criteria_and_reads_probabilities():
    client, session = _jev([_ok({"answer": {"choice": "user", "probabilities": {"user": 0.9, "host": 0.1}, "confidence": 0.9}})])
    out = client.classify({"query": "q", "options": {"user": "a person", "host": "a machine"}}, "Which?", ["user", "host"])
    assert out == {"user": 0.9, "host": 0.1}
    question = _sent(session)[1]["questions"]["answer"]
    assert question == {"type": "choice", "instructions": "Which?", "criteria": {"user": "a person", "host": "a machine"}}


@pytest.mark.parametrize("answer, expected", [(0.7, 0.7), ({"probability": 0.3}, 0.3)])
def test_jev_noul_shapes(answer, expected):
    client, _ = _jev([_ok({"answer": answer})])
    assert client.decide({"query": "q"}, "?", ["YES", "NO"])["YES"] == expected


def test_jev_choice_with_only_confidence():
    client, _ = _jev([_ok({"answer": {"choice": "host", "confidence": 0.8}})])
    out = client.classify({"query": "q"}, "?", ["user", "host", "domain"])
    assert out == {"user": pytest.approx(0.1), "host": 0.8, "domain": pytest.approx(0.1)}


@pytest.mark.parametrize("response, retryable", [
    ((401, {"code": 401, "message": "bad key"}), False),
    ((429, {"code": 429, "message": "slow down"}), True),
    ((503, {}), True),
    ((200, {"code": 7, "message": "invalid question"}), False),
    ((200, {"code": 0, "data": {}}), False),
    (_ok({"answer": {"weird": True}}), False),
])
def test_jev_errors_say_whether_to_retry(response, retryable):
    client, _ = _jev([response])
    with pytest.raises(JevAPIError) as info:
        client.decide({"query": "q"}, "?", ["YES", "NO"])
    assert info.value.retryable is retryable


def test_jev_rejects_oversized_bodies_before_sending():
    client, session = _jev([])
    with pytest.raises(JevAPIError, match="32 KiB"):
        client.decide({"query": "x" * 40_000}, "?", ["YES", "NO"])
    session.post.assert_not_called()


def test_adapter_does_not_retry_a_bad_key():
    client, session = _jev([(401, {"message": "bad key"}), _ok({"answer": 0.9})])
    with pytest.raises(DecisionEngineError, match="1 attempt"):
        JEVAdapter(client, retries=3, backoff=0).decide(DecisionState(query="q"), "?", "s")
    assert session.post.call_count == 1


def test_adapter_retries_a_rate_limit():
    client, session = _jev([(429, {}), _ok({"answer": 0.9})])
    assert JEVAdapter(client, retries=2, backoff=0).decide(DecisionState(query="q"), "?", "s").probability == 0.9
    assert session.post.call_count == 2


def test_source_relevance_is_one_batched_jev_call():
    client, session = _jev([_ok({"q0": 0.9, "q1": {"noul": 0.2}})])
    out = JEVAdapter(client, backoff=0).decide_many(
        DecisionState(query="q"), "Relevant?", {"proxy_logs": "web proxy", "dns_logs": "dns"}
    )
    assert out["proxy_logs"].probability == 0.9 and out["dns_logs"].probability == 0.2
    body = _sent(session)[1]
    assert set(body["questions"]) == {"q0", "q1"}
    assert body["state"]["candidates"] == {"proxy_logs": "web proxy", "dns_logs": "dns"}
    assert session.post.call_count == 1


class RoutingJev:
    """Fake Jev server: answers each question by looking at its text."""

    def __init__(self):
        self.calls = 0

    def __call__(self, url, data, timeout):
        self.calls += 1
        body = json.loads(data)
        answers = {}
        for key, q in body["questions"].items():
            if q["type"] == "choice":
                wanted = "user" if "entity" in q["instructions"] else "authentication"
                answers[key] = {"probabilities": {c: (0.96 if c == wanted else 0.01) for c in q["criteria"]}}
            else:
                answers[key] = {"noul": 0.97 if "auth_logs" in q["instructions"] or "catalog_prior_probability" in body["state"] else 0.1}
        r = MagicMock(status_code=200, ok=True)
        r.json.return_value = {"code": 0, "message": "ok", "data": {"answers": answers}}
        return r


# ---------------------------------------------------------------------------
# ClaudeLLM (SDK mocked)
# ---------------------------------------------------------------------------


def _claude(stop_reason="end_turn", parsed="PARSED"):
    client = MagicMock()
    response = SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, stop_details=None)
    client.beta.messages.parse.return_value = response
    client.messages.parse.return_value = response
    return client


def test_claude_llm_uses_structured_output_with_refusal_fallback():
    client = _claude()
    assert ClaudeLLM(client=client, effort="high").generate("SYS", "PROMPT", GenSource) == "PARSED"
    kwargs = client.beta.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5" and kwargs["system"] == "SYS"
    assert kwargs["output_format"] is GenSource
    assert kwargs["fallbacks"] == "default" and kwargs["betas"] == [ClaudeLLM.FALLBACK_BETA]
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["messages"] == [{"role": "user", "content": "PROMPT"}]


def test_claude_llm_without_fallbacks_uses_the_stable_endpoint():
    client = _claude()
    ClaudeLLM(client=client, fallbacks=None).generate("s", "p", GenSource)
    client.messages.parse.assert_called_once()
    client.beta.messages.parse.assert_not_called()


@pytest.mark.parametrize("stop_reason, parsed, match", [
    ("refusal", None, "declined"), ("max_tokens", None, "truncated"), ("end_turn", None, "no parseable"),
])
def test_claude_llm_failures(stop_reason, parsed, match):
    with pytest.raises(LLMError, match=match):
        ClaudeLLM(client=_claude(stop_reason, parsed)).generate("s", "p", GenSource)


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def generate(self, system, prompt, output_model):
        self.calls.append((system, prompt, output_model))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def catalog():
    return Catalog.load(CATALOG_PATH)


def test_llm_extractor_types_values_and_validates_against_the_catalog(catalog):
    llm = FakeLLM(LLMExtractionOutput(
        values=[LLMValue(value="bob", semantic_type="user"), LLMValue(value="x", semantic_type="spaceship")],
        enum_values=[
            LLMEnumValue(field="auth_logs.outcome", value="failure", term="failed"),
            LLMEnumValue(field="auth_logs.outcome", value="exploded", term="boom"),
            LLMEnumValue(field="nope.field", value="failure", term="failed"),
        ],
        time_range=LLMTimeRange(start="2026-09-23T00:00:00Z", end="2026-09-24T00:00:00Z", text="yesterday"),
    ))
    ex = LLMExtractor(catalog, llm, system_prompt="MY PROMPT").extract("failed logins by bob yesterday", NOW)
    assert [(lit.value, lit.semantic_type) for lit in ex.literals] == [("bob", "user"), ("x", None)]
    assert [(m.field, m.value) for m in ex.enum_matches] == [("auth_logs.outcome", "failure")]
    assert (ex.time_range.start.day, ex.time_range.end.day) == (23, 24)
    system, prompt, _ = llm.calls[0]
    assert system == "MY PROMPT"
    assert "Current UTC time: 2026-09-24T12:00:00" in prompt and '"auth_logs.outcome"' in prompt


def test_llm_extractor_keeps_shape_literals_authoritative(catalog):
    llm = FakeLLM(LLMExtractionOutput(values=[LLMValue(value="10.0.0.5", semantic_type="domain")]))
    ex = LLMExtractor(catalog, llm).extract("Which IPs connected to 10.0.0.5?", NOW)
    assert [(lit.value, lit.kind) for lit in ex.literals] == [("10.0.0.5", "ip_address")]


def test_llm_extractor_falls_back_to_rules_with_a_warning(catalog):
    llm = FakeLLM(LLMError("boom"))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        ex = LLMExtractor(catalog, llm).extract("Which users accessed github in the last 24hrs?", NOW)
    assert any("rule-based" in str(w.message) for w in caught)
    assert ex.time_range.last_hours == 24 and ex.literals[0].value == "github"
    with pytest.raises(LLMError):
        LLMExtractor(catalog, FakeLLM(LLMError("boom")), on_error="raise").extract("x", NOW)


def test_llm_typed_value_needs_no_hint_in_the_question():
    duck = DuckAPI()
    sample_sources.register_sample_sources(duck, NOW)
    search = SemanticSearch(CATALOG_PATH, duck, clock=lambda: NOW)
    question = "Show me failed authentication attempts from alice"
    assert search.search(question).status == "needs_clarification"  # rules alone can't type 'alice'

    search.interpreter.extractor = LLMExtractor(search.catalog, FakeLLM(LLMExtractionOutput(
        values=[LLMValue(value="bob", semantic_type="user")],
        enum_values=[LLMEnumValue(field="auth_logs.outcome", value="failure", term="failed")],
    )))
    result = search.search(question.replace("alice", "bob"))
    assert result.status == "ok" and result.results["username"].tolist() == ["bob"]
    typed = next(d for d in result.decisions if d.kind == "resource_type")
    assert typed.decided_by == "extractor"


# ---------------------------------------------------------------------------
# Catalog generation
# ---------------------------------------------------------------------------


def _generation_llm():
    fw = GenSource(
        description="Firewall connections.",
        fields=[
            GenField(name="timestamp", type="datetime", semantic_type="event_time"),
            GenField(name="src_ip", semantic_type="ip_address", role="source"),
            GenField(name="dst_ip", semantic_type="ip_address", role="destination"),
            GenField(name="action", values=[GenEnumValue(stored="DENY", synonyms=["denied", "blocked"])]),
            GenField(name="made_up_column", semantic_type="user"),
        ],
        time_field="timestamp", entities=["ip_address", "host", "event", "ghost"], activities=["network_connection"],
    )
    assets = GenSource(
        description="Asset inventory.",
        fields=[GenField(name="ip_address", semantic_type="ip_address"), GenField(name="hostname", semantic_type="host")],
        entities=["host"],
    )
    vocab = GenVocabulary(
        entities=[GenEntity(name="ip_address", keywords=["ip", "ips"]), GenEntity(name="host", keywords=["machines"]),
                  GenEntity(name="event", keywords=["connections"], row_level=True)],
        activities=[GenActivity(name="network_connection", keywords=["connected"], resource="ip_address",
                                resource_role="destination", actor_role="source")],
        relationships=[
            GenRelationship(from_field="fw.src_ip", to_field="assets.ip_address", confidence=0.95),
            GenRelationship(from_field="fw.src_ip", to_field="assets.nope", confidence=0.9),
            GenRelationship(from_field="fw.src_ip", to_field="fw.dst_ip", confidence=0.9),
        ],
    )
    return FakeLLM(fw, assets, vocab)


def test_catalog_generation_drafts_sanitizes_and_validates(tmp_path):
    duck = DuckAPI()
    fns = sample_sources.build_sources(NOW)
    duck.register_api_function("firewall_logs", fns["firewall_logs"])
    duck.register_api_function("asset_inventory", fns["asset_inventory"])
    llm = _generation_llm()
    gen = CatalogGenerator(llm, duck, source_prompt="SRC PROMPT", link_prompt="LINK PROMPT", sample_rows=2)
    result = gen.generate([
        TableSpec(name="fw", table="firewall_logs", notes="perimeter firewall"),
        TableSpec(name="assets", table="asset_inventory"),
    ])

    cat = result.catalog
    assert set(cat.sources) == {"fw", "assets"}
    assert "made_up_column" not in cat.sources["fw"].fields
    assert cat.sources["fw"].fields["action"].values == {"DENY": ["denied", "blocked"]}
    assert cat.sources["fw"].entities == ["ip_address", "host", "event"]
    assert [(r.from_, r.to) for r in cat.relationships] == [("fw.src_ip", "assets.ip_address")]
    assert len(result.warnings) == 4  # made-up column, undefined entity, 2 bad relationships

    # prompts are the configured ones; samples and owner notes reach the LLM
    (s1, p1, _), (_, _, _), (s3, p3, m3) = llm.calls
    assert s1 == "SRC PROMPT" and s3 == "LINK PROMPT" and m3 is GenVocabulary
    assert "perimeter firewall" in p1 and "10.0.0.11" in p1 and '"dst_port": "int64"' in p1

    out = tmp_path / "catalog.yaml"
    result.write(str(out))
    text = out.read_text()
    assert text.startswith("# Semantic catalog drafted by an LLM")
    assert Catalog.load(str(out)).sources["fw"].time_field == "timestamp"


def test_generated_catalog_answers_questions():
    duck = DuckAPI()
    fns = sample_sources.build_sources(NOW)
    duck.register_api_function("firewall_logs", fns["firewall_logs"])
    duck.register_api_function("asset_inventory", fns["asset_inventory"])
    result = CatalogGenerator(_generation_llm(), duck).generate([
        TableSpec(name="fw", table="firewall_logs"), TableSpec(name="assets", table="asset_inventory"),
    ])
    search = SemanticSearch(result.catalog, duck, clock=lambda: NOW)
    answer = search.search("Which machines connected to 203.0.113.9?")
    assert answer.status == "ok", answer.clarification
    assert sorted(answer.results["hostname"]) == ["srv-build", "ws-dave"]


def test_sample_rows_zero_sends_no_values():
    duck = DuckAPI()
    duck.register_api_function("firewall_logs", sample_sources.build_sources(NOW)["firewall_logs"])
    profile = CatalogGenerator(FakeLLM(), duck, sample_rows=0).profile(TableSpec(name="fw", table="firewall_logs"))
    assert profile["sample_rows"] == [] and "src_ip" in profile["columns"]


def test_default_specs_skip_tables_needing_structural_args():
    duck = DuckAPI()
    duck.register_api_function("free", lambda limit=None: [])
    duck.register_api_function("needs_args", lambda table_name, limit=None: [])
    assert [s.table for s in CatalogGenerator(FakeLLM(), duck).default_specs()] == ["free"]


# ---------------------------------------------------------------------------
# Config file wiring
# ---------------------------------------------------------------------------


def _write_config(tmp_path, semantic):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "extract.md").write_text("PROMPT FROM FILE")
    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps({"services": {}, "semantic": semantic}))
    return str(path)


def test_from_config_builds_jev_with_key_from_authentication(tmp_path, monkeypatch):
    path = _write_config(tmp_path, {
        "catalog_path": CATALOG_PATH,
        "decision_engine": {"type": "jev", "model": "typesafe-ai/jev",
                            "authentication": {"type": "local", "api_key": "jev-secret"}},
        "thresholds": {"source": 0.9},
        "allowed_sources": ["auth_logs"],
    })
    fake = RoutingJev()
    monkeypatch.setattr("requests.Session.post", lambda self, url, data, timeout: fake(url, data, timeout))
    duck = DuckAPI()
    sample_sources.register_sample_sources(duck, NOW)
    search = SemanticSearch.from_config(duck, config_path=path, clock=lambda: NOW)

    assert isinstance(search.engine, JEVAdapter)
    assert search.engine.backend.session.headers["Authorization"] == "Bearer jev-secret"
    assert search.thresholds.source == 0.9
    result = search.search("Which users generated failed authentication events?")
    assert result.status == "ok", result.clarification
    assert sorted(result.results["username"]) == ["bob", "erin"]


def test_from_config_llm_extractor_reads_prompt_file(tmp_path, monkeypatch):
    path = _write_config(tmp_path, {
        "catalog_path": CATALOG_PATH,
        "llm": {"model": "claude-opus-5", "authentication": {"type": "local", "api_key": "sk-test"}},
        "extractor": {"type": "llm", "system_prompt_file": "prompts/extract.md"},
    })
    built = {}

    def fake_init(self, **kwargs):
        built.update(kwargs)

    monkeypatch.setattr(ClaudeLLM, "__init__", lambda self, **kw: fake_init(self, **kw))
    duck = DuckAPI()
    sample_sources.register_sample_sources(duck, NOW)
    search = SemanticSearch.from_config(duck, config_path=path)
    extractor = search.interpreter.extractor
    assert isinstance(extractor, LLMExtractor)
    assert extractor.system_prompt == "PROMPT FROM FILE"
    assert built["api_key"] == "sk-test" and built["model"] == "claude-opus-5"


def test_config_rejects_typos(tmp_path):
    from duckduck.semantic import SemanticConfig

    path = _write_config(tmp_path, {"decision_engin": {"type": "jev"}})
    with pytest.raises(Exception, match="decision_engin"):
        SemanticConfig.load(DuckAPI(), path)


def test_example_config_semantic_section_is_valid():
    from duckduck.semantic import SemanticConfig
    from semantic_helpers import REPO

    with open(f"{REPO}/duckduck.example.json") as f:
        cfg = SemanticConfig.model_validate(json.load(f)["semantic"])
    assert cfg.decision_engine.type == "jev" and cfg.extractor.type == "llm"
    assert cfg.catalog_generation.tables[0].args == {"database": "security", "table_name": "proxy_logs"}


# ---------------------------------------------------------------------------
# Catalog-driven discovery: the tables *behind* a connector
# ---------------------------------------------------------------------------

from typing import Optional as _Opt  # noqa: E402

import pandas as _pd  # noqa: E402

from duckduck.kinds import catalog as _catalog  # noqa: E402


class FakeLake:
    """A Glue-like connector: a catalog listing (database, table_name) and the table function it feeds."""

    DATA = {("security", "proxy_logs"): [{"user": "a", "url": "x"}],
            ("security", "dns_logs"): [{"client": "1.1.1.1"}],
            ("sales", "orders"): [{"id": 1}]}

    @_catalog(lists="table")
    def tables(self, database: _Opt[str] = None, limit: _Opt[int] = None):
        """Lists tables."""
        return _pd.DataFrame([{"database": d, "table_name": t, "format": "parquet"} for d, t in self.DATA])

    def table(self, database: str, table_name: str, where=None, limit: _Opt[int] = None):
        """Reads one table."""
        return self.DATA[(database, table_name)][: limit or None]

    @_catalog
    def databases(self, limit: _Opt[int] = None):
        """Lists databases (no `lists=`: nothing to discover from it)."""
        return [{"database": "security"}, {"database": "sales"}]


def _discovery_duck(tmp_path):
    import sqlalchemy as sa

    from duckduck.database import SQLDatabase

    duck = DuckAPI()
    lake = FakeLake()
    for n in ("tables", "table", "databases"):
        duck.register_api_function(f"glue_{n}", getattr(lake, n))
    db = SQLDatabase(f"sqlite:///{tmp_path}/crm.db")
    with db.engine.begin() as c:
        c.execute(sa.text("CREATE TABLE customers (id INTEGER, name TEXT)"))
        c.execute(sa.text("INSERT INTO customers VALUES (1, 'acme')"))
    for n in ("tables", "table", "query"):
        duck.register_api_function(f"crm_{n}", getattr(db, n))
    duck.register_api_function("vulns_of", lambda asset_id, limit=None: [])  # table function, no catalog
    duck.register_api_function("assets", lambda limit=None: [{"hostname": "h"}])  # plain table
    return duck


def test_plan_specs_discovers_tables_through_catalogs(tmp_path):
    duck = _discovery_duck(tmp_path)
    specs, notes = CatalogGenerator(FakeLLM(), duck).plan_specs()
    by_name = {s.name: (s.table, s.args) for s in specs}
    assert by_name == {
        "assets": ("assets", {}),
        "security_proxy_logs": ("glue_table", {"database": "security", "table_name": "proxy_logs"}),
        "security_dns_logs": ("glue_table", {"database": "security", "table_name": "dns_logs"}),
        "sales_orders": ("glue_table", {"database": "sales", "table_name": "orders"}),
        "customers": ("crm_table", {"table_name": "customers"}),
    }
    # catalogs / raw queries are never described as data
    assert not {"glue_tables", "glue_databases", "crm_tables", "crm_query"} & set(by_name)
    assert any("vulns_of" in n and "catalog_generation.tables" in n for n in notes)


def test_include_exclude_and_max_tables(tmp_path):
    duck = _discovery_duck(tmp_path)
    gen = CatalogGenerator(FakeLLM(), duck, include=["security.*"], exclude=["*dns*"])
    assert [s.name for s in gen.plan_specs()[0]] == ["security_proxy_logs"]
    specs, notes = CatalogGenerator(FakeLLM(), duck, max_tables=2).plan_specs()
    assert len(specs) == 2 and any("5 tables matched; drafting only the first 2" in n for n in notes)
    assert [s.name for s in CatalogGenerator(FakeLLM(), duck, discover=False).plan_specs()[0]] == ["assets"]


def test_generate_profiles_discovered_tables_with_their_args(tmp_path):
    duck = _discovery_duck(tmp_path)
    prompts = []

    class Recorder:
        def generate(self, system, prompt, output_model):
            if output_model is GenVocabulary:
                return GenVocabulary()
            prompts.append(prompt)
            profile = json.loads(prompt.split("Table profile:\n", 1)[1])
            return GenSource(description="d", fields=[GenField(name=next(iter(profile["columns"])))])

    result = CatalogGenerator(Recorder(), duck, include=["security.proxy_logs", "customers"]).generate()
    source = result.catalog.sources["security_proxy_logs"]
    assert source.table == "glue_table" and source.args == {"database": "security", "table_name": "proxy_logs"}
    assert result.catalog.sources["customers"].table == "crm_table"
    assert any('"user"' in p for p in prompts)  # the real table's columns were sampled
    # and the drafted binding is usable as-is by SemanticSearch
    assert set(SemanticSearch(result.catalog, duck).catalog.sources) == {"security_proxy_logs", "customers"}


def test_failing_catalog_is_a_note_not_a_crash(tmp_path):
    class DeniedLake(FakeLake):
        @_catalog(lists="table")
        def tables(self, database=None, limit=None):
            raise PermissionError("no glue:GetTables")

    duck = _discovery_duck(tmp_path)
    lake = DeniedLake()
    duck.register_api_function("glue_tables", lake.tables)  # same instance as its table function
    duck.register_api_function("glue_table", lake.table)
    specs, notes = CatalogGenerator(FakeLLM(), duck).plan_specs()
    assert any("catalog 'glue_tables' failed (PermissionError: no glue:GetTables)" in n for n in notes)
    assert "customers" in {s.name for s in specs}  # other sources still discovered
    assert not any(s.table == "glue_table" for s in specs)

"""LLM providers beyond the Claude API: Claude on Microsoft Foundry, Azure OpenAI, gateways."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pydantic")

from duckduck import DuckAPI  # noqa: E402
from duckduck.semantic import AzureOpenAILLM, ClaudeLLM, LLMError, SemanticConfig  # noqa: E402
from duckduck.semantic import llm as llm_module  # noqa: E402
from duckduck.semantic.generation import GenSource  # noqa: E402

from semantic_helpers import CATALOG_PATH  # noqa: E402

ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL",
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "OPENAI_API_VERSION",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture
def entra(monkeypatch):
    """Stands in for DefaultAzureCredential; records how it was asked for."""
    calls = []

    def provider(tenant_id=None, scope=llm_module.AZURE_AI_SCOPE):
        calls.append((tenant_id, scope))
        return lambda: "entra-token"

    monkeypatch.setattr(llm_module, "azure_token_provider", provider)
    return calls


# ---------------------------------------------------------------------------
# Azure OpenAI
# ---------------------------------------------------------------------------


def _azure_client(message=None, finish_reason="stop"):
    client = MagicMock()
    message = message or SimpleNamespace(parsed="PARSED", refusal=None)
    client.chat.completions.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)]
    )
    return client


def test_azure_openai_uses_the_deployment_and_structured_output():
    client = _azure_client()
    llm = AzureOpenAILLM("gpt-prod", client=client, max_tokens=900, reasoning_effort="low")
    assert llm.generate("SYS", "PROMPT", GenSource) == "PARSED"
    kwargs = client.chat.completions.parse.call_args.kwargs
    assert kwargs["model"] == "gpt-prod" and kwargs["response_format"] is GenSource
    assert kwargs["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "PROMPT"}]
    assert kwargs["max_completion_tokens"] == 900 and kwargs["reasoning_effort"] == "low"


@pytest.mark.parametrize("message, finish_reason, match", [
    (SimpleNamespace(parsed=None, refusal="no"), "stop", "declined"),
    (SimpleNamespace(parsed=None, refusal=None), "length", "truncated"),
    (SimpleNamespace(parsed=None, refusal=None), "content_filter", "content filter"),
    (SimpleNamespace(parsed=None, refusal=None), "stop", "no parseable"),
])
def test_azure_openai_failures(message, finish_reason, match):
    with pytest.raises(LLMError, match=match):
        AzureOpenAILLM("d", client=_azure_client(message, finish_reason)).generate("s", "p", GenSource)


def test_azure_openai_sdk_finish_reason_exceptions_become_llm_errors():
    openai = pytest.importorskip("openai")
    client = MagicMock()
    client.chat.completions.parse.side_effect = openai.LengthFinishReasonError(completion=MagicMock())
    with pytest.raises(LLMError, match="truncated"):
        AzureOpenAILLM("d", client=client).generate("s", "p", GenSource)


def test_azure_openai_real_client_with_key_endpoint_and_headers():
    pytest.importorskip("openai")
    llm = AzureOpenAILLM("d", endpoint="https://res.openai.azure.com", api_key="k",
                         default_headers={"Ocp-Apim-Subscription-Key": "sub"})
    assert str(llm.client.base_url).startswith("https://res.openai.azure.com/openai")
    assert llm.client.api_key == "k" and llm.client._custom_query["api-version"] == AzureOpenAILLM.DEFAULT_API_VERSION
    assert llm.client._custom_headers["Ocp-Apim-Subscription-Key"] == "sub"


def test_azure_openai_reads_the_standard_env_vars(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://env.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("OPENAI_API_VERSION", "2025-01-01-preview")
    llm = AzureOpenAILLM("d")
    assert "env.openai.azure.com" in str(llm.client.base_url) and llm.client.api_key == "env-key"
    assert llm.client._custom_query["api-version"] == "2025-01-01-preview"


def test_azure_openai_without_a_key_uses_entra_id(entra):
    pytest.importorskip("openai")
    llm = AzureOpenAILLM("d", endpoint="https://res.openai.azure.com", tenant_id="t1")
    assert entra == [("t1", llm_module.AZURE_AI_SCOPE)]
    assert llm.client._azure_ad_token_provider() == "entra-token"


def test_azure_openai_without_an_endpoint_says_where_to_put_it():
    pytest.importorskip("openai")
    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
        AzureOpenAILLM("d", api_key="k")


# ---------------------------------------------------------------------------
# Claude on Microsoft Foundry, and gateways
# ---------------------------------------------------------------------------


def test_foundry_with_resource_and_key():
    pytest.importorskip("anthropic")
    llm = ClaudeLLM.on_foundry(model="claude-dep", resource="myres", api_key="k")
    assert str(llm.client.base_url) == "https://myres.services.ai.azure.com/anthropic/"
    assert llm.client.api_key == "k" and llm.model == "claude-dep"
    assert llm.fallbacks is None  # the refusal fallback is a Claude API feature


def test_foundry_endpoint_and_key_from_env(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "https://gw.example.com/anthropic/")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "env-key")
    llm = ClaudeLLM.on_foundry()
    assert str(llm.client.base_url) == "https://gw.example.com/anthropic/" and llm.client.api_key == "env-key"


def test_foundry_without_a_key_uses_entra_id(entra):
    pytest.importorskip("anthropic")
    ClaudeLLM.on_foundry(resource="myres", tenant_id="t1")
    assert entra == [("t1", llm_module.AZURE_AI_SCOPE)]


def test_foundry_without_an_endpoint_says_where_to_put_it():
    pytest.importorskip("anthropic")
    with pytest.raises(ValueError, match="ANTHROPIC_FOUNDRY_RESOURCE"):
        ClaudeLLM.on_foundry(api_key="k")


def test_claude_api_gateway_authenticating_by_header_needs_no_api_key():
    pytest.importorskip("anthropic")
    llm = ClaudeLLM(base_url="https://gw.example.com", default_headers={"Authorization": "Bearer gw"})
    assert llm.client._custom_headers["Authorization"] == "Bearer gw"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _semantic(tmp_path, llms=None, **semantic):
    """A duckduck.json with ``ai_providers`` at the top level (next to services) and ``semantic``."""
    data = {"services": {}, "semantic": {"catalog_path": CATALOG_PATH, **semantic}}
    if llms is not None:
        data["ai_providers"] = llms
    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps(data))
    return SemanticConfig.load(DuckAPI(), str(path))


def _config(tmp_path, llm):
    """One LLM, declared at the top level as "main" and named as the default."""
    return _semantic(tmp_path, llms={"main": llm}, default_llm="main")


def test_config_azure_openai_settings_and_header_from_the_secret(tmp_path):
    pytest.importorskip("openai")
    cfg = _config(tmp_path, {
        "provider": "azure_openai",
        "api_version": "2025-01-01-preview",
        "headers": {"Ocp-Apim-Subscription-Key": "$secret.apim_key", "X-Team": "sec"},
        "authentication": {"type": "local", "endpoint": "https://res.openai.azure.com",
                           "deployment": "gpt-prod", "api_key": "k", "apim_key": "sub"},
    })
    llm = cfg.build_llm(DuckAPI())
    assert isinstance(llm, AzureOpenAILLM) and llm.deployment == "gpt-prod"
    assert llm.client.api_key == "k" and llm.client._custom_query["api-version"] == "2025-01-01-preview"
    assert llm.client._custom_headers["Ocp-Apim-Subscription-Key"] == "sub"
    assert llm.client._custom_headers["X-Team"] == "sec"


def test_config_azure_openai_without_authentication_uses_entra_id(tmp_path, entra):
    pytest.importorskip("openai")
    cfg = _config(tmp_path, {"provider": "azure_openai", "endpoint": "https://res.openai.azure.com",
                             "deployment": "gpt-prod", "tenant_id": "t1"})
    cfg.build_llm(DuckAPI())
    assert entra == [("t1", llm_module.AZURE_AI_SCOPE)]


def test_config_azure_openai_needs_a_deployment(tmp_path):
    cfg = _config(tmp_path, {"provider": "azure_openai", "endpoint": "https://res.openai.azure.com"})
    with pytest.raises(ValueError, match="deployment"):
        cfg.build_llm(DuckAPI())


def test_config_foundry(tmp_path):
    pytest.importorskip("anthropic")
    cfg = _config(tmp_path, {"provider": "foundry", "model": "claude-dep", "resource": "myres",
                             "authentication": {"type": "local", "api_key": "k"}})
    llm = cfg.build_llm(DuckAPI())
    assert isinstance(llm, ClaudeLLM) and llm.model == "claude-dep" and llm.fallbacks is None
    assert str(llm.client.base_url) == "https://myres.services.ai.azure.com/anthropic/"


def test_config_rejects_a_setting_of_another_provider(tmp_path):
    with pytest.raises(ValueError, match="'deployment' applies to provider 'azure_openai'"):
        _config(tmp_path, {"provider": "foundry", "deployment": "x"})


def test_config_header_referring_to_a_missing_secret_key(tmp_path):
    cfg = _config(tmp_path, {"provider": "azure_openai", "deployment": "d",
                             "headers": {"Ocp-Apim-Subscription-Key": "$secret.nope"},
                             "authentication": {"type": "local", "api_key": "k"}})
    with pytest.raises(ValueError, match=r"\$secret\.nope.*\['api_key'\]"):
        cfg.build_llm(DuckAPI())


# ---------------------------------------------------------------------------
# one LLM per stage
# ---------------------------------------------------------------------------




LLMS = {
    "strong": {"model": "claude-opus-5"},
    "fast": {"model": "claude-haiku-4-5", "max_tokens": 4000},
    "azure": {"provider": "azure_openai", "endpoint": "https://res.openai.azure.com", "deployment": "gpt-mini"},
}


def test_stages_reference_declared_llms_by_name(tmp_path):
    cfg = _semantic(
        tmp_path, llms=LLMS, default_llm="strong",
        extractor={"type": "llm", "llm": "azure"},
        catalog_generation={"llm": "fast", "link_llm": "strong"},
    )
    assert cfg.llm_config()[0] == "strong"
    name, extractor = cfg.llm_config("extractor")
    assert name == "azure" and extractor.deployment == "gpt-mini"
    assert cfg.llm_config("catalog_generation")[0] == "fast"
    assert cfg.llm_config("catalog_link")[0] == "strong"


def test_unset_stages_fall_back(tmp_path):
    cfg = _semantic(tmp_path, llms=LLMS, default_llm="strong", catalog_generation={"llm": "fast"})
    assert cfg.llm_config("extractor")[0] == "strong"      # → default_llm
    assert cfg.llm_config("catalog_link")[0] == "fast"     # → catalog_generation.llm, then default_llm


@pytest.mark.parametrize("semantic", [
    {"default_llm": {"model": "claude-opus-5"}},
    {"extractor": {"type": "llm", "llm": {"model": "claude-haiku-4-5"}}},
    {"catalog_generation": {"link_llm": {"model": "claude-opus-5"}}},
])
def test_a_block_where_a_name_belongs_says_to_declare_it_at_the_top_level(tmp_path, semantic):
    with pytest.raises(ValueError, match=r"(?s)is the name of an ai_providers entry, not a block.*\"ai_providers\": \{\"my_llm\""):
        _semantic(tmp_path, **semantic)


def test_ai_providers_inside_semantic_is_rejected_pointing_to_the_top_level(tmp_path):
    data = {"services": {}, "semantic": {"catalog_path": CATALOG_PATH, "ai_providers": LLMS, "default_llm": "strong"}}
    with pytest.raises(ValueError, match="'ai_providers' goes at the top level of the file"):
        SemanticConfig.from_file_data(data)


@pytest.mark.parametrize("where", ["root", "semantic"])
def test_the_old_llms_section_says_it_is_now_ai_providers(where):
    data = {"services": {}, "semantic": {"catalog_path": CATALOG_PATH, "default_llm": "strong"}}
    (data if where == "root" else data["semantic"])["llms"] = LLMS
    with pytest.raises(ValueError, match="'llms' is now 'ai_providers'"):
        SemanticConfig.from_file_data(data)


def test_file_level_ai_providers_sit_next_to_services(tmp_path):
    data = {"services": {}, "ai_providers": LLMS, "semantic": {"catalog_path": CATALOG_PATH, "default_llm": "fast"}}
    cfg = SemanticConfig.from_file_data(data)
    assert set(cfg.ai_providers) == set(LLMS) and cfg.llm_config()[1].model == "claude-haiku-4-5"


def test_unknown_llm_name_fails_at_load_listing_the_declared_ones(tmp_path):
    with pytest.raises(ValueError, match=r"semantic.extractor.llm refers to 'fsat'.*top-level 'ai_providers'.*declared: azure, fast, strong"):
        _semantic(tmp_path, llms=LLMS, extractor={"type": "llm", "llm": "fsat"})


def test_invalid_declared_llm_fails_at_load(tmp_path):
    with pytest.raises(ValueError, match="deployment"):
        _semantic(tmp_path, llms={"bad": {"deployment": "x"}})


def test_no_llm_named_explains_where_to_declare_it(tmp_path):
    cfg = _semantic(tmp_path, llms=LLMS)
    with pytest.raises(ValueError, match=r"(?s)'extractor.llm'.*top-level \"ai_providers\""):
        cfg.build_llm(DuckAPI(), "extractor")


def test_each_stage_gets_its_own_client(tmp_path, monkeypatch):
    cfg = _semantic(
        tmp_path, llms=LLMS, default_llm="strong",
        extractor={"type": "llm", "llm": "fast"},
        catalog_generation={"llm": "fast", "link_llm": "strong"},
    )
    monkeypatch.setattr(SemanticConfig, "build_llm", lambda self, duck, stage=None: self.llm_config(stage)[0])
    gen = cfg.build_generator(DuckAPI())
    assert (gen.llm, gen.link_llm) == ("fast", "strong")
    from duckduck.semantic import Catalog
    assert cfg.build_extractor(Catalog.load(CATALOG_PATH), DuckAPI()).llm == "fast"


def test_generator_uses_the_link_llm_for_the_final_call():
    from duckduck.semantic import CatalogGenerator
    from duckduck.semantic.generation import GenEntity, GenField, GenVocabulary

    calls = []

    class Fake:
        def __init__(self, name):
            self.name = name

        def generate(self, system, prompt, output_model):
            calls.append((self.name, output_model.__name__))
            if output_model is GenVocabulary:
                return GenVocabulary(entities=[GenEntity(name="host", keywords=["hosts"])])
            return GenSource(description="Hosts.", fields=[GenField(name="hostname")])

    import pandas as pd

    duck = DuckAPI()
    duck.register_api_function("hosts", lambda: pd.DataFrame({"hostname": ["a"]}))
    CatalogGenerator(Fake("cheap"), duck, link_llm=Fake("strong")).generate()
    assert calls == [("cheap", "GenSource"), ("strong", "GenVocabulary")]


def test_the_old_semantic_llm_key_says_it_was_renamed(tmp_path):
    with pytest.raises(ValueError, match="'semantic.llm' is now 'semantic.default_llm'"):
        _semantic(tmp_path, llms=LLMS, llm="strong")


# ---------------------------------------------------------------------------
# OpenRouter: chat models, and Jev through its Decisions API
# ---------------------------------------------------------------------------


def test_openrouter_chat_client_defaults():
    pytest.importorskip("openai")
    from duckduck.semantic.llm import OpenRouterLLM

    llm = OpenRouterLLM("anthropic/claude-x", api_key="or-key", default_headers={"X-Title": "duckduck"})
    assert str(llm.client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"
    assert llm.client.api_key == "or-key" and llm.client._custom_headers["X-Title"] == "duckduck"


def test_openrouter_request_shape():
    from duckduck.semantic.llm import OpenRouterLLM

    client = _azure_client()
    OpenRouterLLM("vendor/model", client=client, max_tokens=500, reasoning_effort="high").generate("S", "P", GenSource)
    kwargs = client.chat.completions.parse.call_args.kwargs
    assert kwargs["model"] == "vendor/model" and kwargs["max_tokens"] == 500 and kwargs["response_format"] is GenSource
    assert kwargs["extra_body"] == {"provider": {"require_parameters": True}, "reasoning": {"effort": "high"}}


def test_openrouter_key_from_env_or_a_clear_error(monkeypatch):
    pytest.importorskip("openai")
    from duckduck.semantic.llm import OpenRouterLLM

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterLLM("vendor/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    assert OpenRouterLLM("vendor/model").client.api_key == "from-env"


def test_openrouter_entry_needs_a_model(tmp_path):
    with pytest.raises(ValueError, match="needs 'model'"):
        _semantic(tmp_path, llms={"or": {"provider": "openrouter"}})


def test_config_builds_an_openrouter_llm(tmp_path):
    pytest.importorskip("openai")
    from duckduck.semantic.llm import OpenRouterLLM

    cfg = _config(tmp_path, {"provider": "openrouter", "model": "vendor/model", "require_parameters": False,
                             "authentication": {"type": "local", "api_key": "k"}})
    llm = cfg.build_llm(DuckAPI())
    assert isinstance(llm, OpenRouterLLM) and llm.model_id == "vendor/model" and llm._extra() == {}


# The response below is the one OpenRouter's Jev tutorial shows from the live API.
OPENROUTER_JEV_ANSWER = {
    "id": "gen-dec-1", "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
    "answers": {"answer": {"type": "choice", "choice": "user", "confidence": 0.67,
                           "probabilities": {"user": 0.78, "host": 0.22, "domain": 0}}},
    "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.00002},
}


def test_jev_through_openrouter_decisions_api(tmp_path, monkeypatch):
    from duckduck.semantic.decisions import DecisionState, JEVAdapter

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    sent = {}

    def fake_post(self, url, data, timeout):
        sent.update(url=url, auth=self.headers["Authorization"], body=json.loads(data))
        r = MagicMock(status_code=200, ok=True)
        r.json.return_value = OPENROUTER_JEV_ANSWER
        return r

    monkeypatch.setattr("requests.Session.post", fake_post)
    cfg = _semantic(tmp_path, llms={"jev_or": {"provider": "openrouter", "api": "decisions", "model": "typesafe/jev-1.13"}},
                    decision_engine={"ai_provider": "jev_or"})
    engine = cfg.build_engine(DuckAPI())
    assert isinstance(engine, JEVAdapter)
    result = engine.classify(DecisionState(query="who?"), "Which entity?", {"user": "a person", "host": "a machine", "domain": "a site"})
    assert result.choice == "user" and abs(result.probability - 0.78) < 1e-9
    assert sent["url"] == "https://openrouter.ai/api/alpha/decisions" and sent["auth"] == "Bearer or-key"
    assert sent["body"]["model"] == "typesafe/jev-1.13" and sent["body"]["questions"]["answer"]["type"] == "choice"


def test_decisions_url_points_any_entry_at_another_host(tmp_path, monkeypatch):
    urls = []

    def fake_post(self, url, data, timeout):
        urls.append(url)
        r = MagicMock(status_code=200, ok=True)
        r.json.return_value = {"answers": {"answer": {"type": "noul", "noul": 0.9}}}
        return r

    monkeypatch.setattr("requests.Session.post", fake_post)
    from duckduck.semantic.decisions import DecisionState

    cfg = _semantic(tmp_path, llms={"j": {"provider": "jev", "decisions_url": "https://gw.corp/decisions",
                                          "authentication": {"type": "local", "api_key": "k"}}},
                    decision_engine={"ai_provider": "j"})
    assert cfg.build_engine(DuckAPI()).decide(DecisionState(query="q"), "yes?", "s").probability == 0.9
    assert urls == ["https://gw.corp/decisions"]


@pytest.mark.parametrize("entry, match", [
    ({"provider": "anthropic", "api": "decisions"}, "has no Decisions API"),
    ({"provider": "jev", "api": "chat"}, "only serves the Decisions API"),
    ({"provider": "openrouter", "api": "decisions", "model": "m", "max_tokens": 5}, "only apply to api 'chat'"),
])
def test_api_and_provider_must_agree(tmp_path, entry, match):
    with pytest.raises(ValueError, match=match):
        _semantic(tmp_path, llms={"x": entry})


def test_a_decisions_entry_cannot_be_an_llm(tmp_path):
    with pytest.raises(ValueError, match="Decisions API entry — that can only be a decision_engine"):
        _semantic(tmp_path, llms={"jev": {"provider": "jev"}}, default_llm="jev")


def test_unknown_decision_engine_provider(tmp_path):
    with pytest.raises(ValueError, match="decision_engine.ai_provider refers to 'nope'"):
        _semantic(tmp_path, llms=LLMS, decision_engine={"ai_provider": "nope"})


@pytest.mark.parametrize("old", [
    {"type": "jev", "model": "typesafe-ai/jev"},
    {"authentication": {"type": "local", "api_key": "k"}},
])
def test_the_old_inline_decision_engine_says_how_to_move_it(tmp_path, old):
    with pytest.raises(ValueError, match=r'(?s)now an .ai_providers. entry.*"provider": "jev"'):
        _semantic(tmp_path, decision_engine=old)


# ---------------------------------------------------------------------------
# a chat model as the decision engine
# ---------------------------------------------------------------------------


class ScriptedJudge:
    def __init__(self):
        self.calls = []

    def generate(self, system, prompt, output_model):
        from duckduck.semantic.llm_decisions import _Scored, _Scores, _YesNo

        body = json.loads(prompt)
        self.calls.append((system, body, output_model))
        if output_model is _YesNo:
            return _YesNo(probability=0.8)
        keys = list(body.get("options") or body.get("judgments"))
        return _Scores(scores=[_Scored(key=k, probability=0.9 if i == 0 else 0.1) for i, k in enumerate(keys)])


def test_chat_model_decision_backend():
    from duckduck.semantic.decisions import DecisionState, JEVAdapter
    from duckduck.semantic.llm_decisions import DEFAULT_DECISION_PROMPT, LLMDecisionBackend

    judge = ScriptedJudge()
    engine = JEVAdapter(LLMDecisionBackend(judge))
    state = DecisionState(query="Which users hit github?", terms=["user"], prior=0.7)
    assert engine.decide(state, "Is it relevant?", "proxy logs").probability == pytest.approx(0.8)
    assert engine.classify(state, "Which entity?", {"user": "person", "host": "machine"}).choice == "user"
    many = engine.decide_many(state, "Relevant?", {"proxy": "web", "dns": "names"})
    assert many["proxy"].probability == pytest.approx(0.9) and many["dns"].probability == pytest.approx(0.1)
    assert len(judge.calls) == 3  # the batch is one call
    system, body, _ = judge.calls[0]
    assert system == DEFAULT_DECISION_PROMPT
    assert body["state"]["catalog_prior_probability"] == 0.7 and "terms" not in body["state"]


def test_config_builds_a_chat_decision_engine_with_its_own_prompt(tmp_path, monkeypatch):
    from duckduck.semantic.decisions import JEVAdapter
    from duckduck.semantic.llm_decisions import LLMDecisionBackend

    monkeypatch.setattr(SemanticConfig, "_build_client", lambda self, name, cfg, duck: f"client:{name}")
    cfg = _semantic(tmp_path, llms=LLMS, decision_engine={"ai_provider": "fast", "system_prompt": "JUDGE", "timeout": 30})
    engine = cfg.build_engine(DuckAPI())
    assert isinstance(engine, JEVAdapter) and isinstance(engine.backend, LLMDecisionBackend)
    assert engine.backend.llm == "client:fast" and engine.backend.system_prompt == "JUDGE" and engine.timeout == 30

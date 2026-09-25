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
    """A duckduck.json with ``llms`` at the top level (next to services) and ``semantic``."""
    data = {"services": {}, "semantic": {"catalog_path": CATALOG_PATH, **semantic}}
    if llms is not None:
        data["llms"] = llms
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
    with pytest.raises(ValueError, match="llm.deployment applies to provider 'azure_openai'"):
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
    with pytest.raises(ValueError, match=r"(?s)is the name of an LLM, not a block.*\"llms\": \{\"my_llm\""):
        _semantic(tmp_path, **semantic)


def test_llms_inside_semantic_is_rejected_pointing_to_the_top_level(tmp_path):
    data = {"services": {}, "semantic": {"catalog_path": CATALOG_PATH, "llms": LLMS, "default_llm": "strong"}}
    with pytest.raises(ValueError, match="'llms' goes at the top level of the file"):
        SemanticConfig.from_file_data(data)


def test_file_level_llms_sit_next_to_services(tmp_path):
    data = {"services": {}, "llms": LLMS, "semantic": {"catalog_path": CATALOG_PATH, "default_llm": "fast"}}
    cfg = SemanticConfig.from_file_data(data)
    assert set(cfg.llms) == set(LLMS) and cfg.llm_config()[1].model == "claude-haiku-4-5"


def test_unknown_llm_name_fails_at_load_listing_the_declared_ones(tmp_path):
    with pytest.raises(ValueError, match=r"semantic.extractor.llm refers to LLM 'fsat'.*top-level 'llms'.*declared: azure, fast, strong"):
        _semantic(tmp_path, llms=LLMS, extractor={"type": "llm", "llm": "fsat"})


def test_invalid_declared_llm_fails_at_load(tmp_path):
    with pytest.raises(ValueError, match="deployment"):
        _semantic(tmp_path, llms={"bad": {"deployment": "x"}})


def test_no_llm_named_explains_where_to_declare_it(tmp_path):
    cfg = _semantic(tmp_path, llms=LLMS)
    with pytest.raises(ValueError, match=r"(?s)'extractor.llm'.*top-level \"llms\""):
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

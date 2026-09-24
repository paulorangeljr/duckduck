"""
LLM access for the two jobs the architecture gives a generative model:
drafting the semantic catalog, and extracting values from questions.
Never for writing SQL, and never for the judgments JEV makes.

``LLMClient`` is the protocol (one method: system prompt + user content →
a validated Pydantic object). ``ClaudeLLM`` implements it with the
Anthropic SDK's structured outputs (``messages.parse``), so the response
is schema-constrained JSON already parsed into the requested model —
against the Claude API (or a gateway, via ``base_url``) or Claude on
Microsoft Foundry (``ClaudeLLM.on_foundry``). ``AzureOpenAILLM`` does the
same over an Azure OpenAI deployment. Another provider plugs in by
implementing ``generate``.

Both Azure flavours authenticate with an API key or, without one, with
Entra ID (``DefaultAzureCredential``: managed identity, ``az login``,
``AZURE_*`` env vars — needs ``azure-identity``).
"""

import os
from typing import Any, Callable, Mapping, Optional, Protocol, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


MISSING_KEY_HELP = (
    "No Anthropic API key found for the LLM. Either:\n"
    "  - set ANTHROPIC_API_KEY in the environment of *this* process (a notebook kernel only sees "
    "variables that existed when it started: restart it, or set os.environ[\"ANTHROPIC_API_KEY\"] "
    "before building the LLM), or\n"
    "  - add an \"authentication\" block to the LLM's declaration (top-level \"llms\" section) that yields "
    "an api_key, e.g. {\"type\": \"aws\", \"secret_id\": \"prod/anthropic\", \"api_key\": \"$secret.key\"}, or\n"
    "  - pass ClaudeLLM(api_key=...) when building it in code.\n"
    "Using a model on Azure instead? Set \"provider\": \"foundry\" (Claude on Microsoft Foundry) or "
    "\"azure_openai\" in the LLM's declaration."
)


#: Entra ID scope for Azure AI services (Azure OpenAI; Claude on Foundry).
AZURE_AI_SCOPE = "https://cognitiveservices.azure.com/.default"


def _require_credentials(client: Any, headers: Optional[Mapping[str, str]] = None) -> None:
    """Fails at construction, not at the first request (after tables were already profiled)."""
    if any(h.lower() in ("x-api-key", "authorization") for h in (headers or {})):
        return  # a gateway credential sent as a header
    if not (getattr(client, "api_key", None) or getattr(client, "auth_token", None)
            or getattr(client, "credentials", None)):
        raise ValueError(MISSING_KEY_HELP)


def azure_token_provider(tenant_id: Optional[str] = None, scope: str = AZURE_AI_SCOPE) -> Callable[[], str]:
    """Bearer-token callable from ``DefaultAzureCredential`` (optionally pinned to a tenant)."""
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError as exc:
        raise ImportError(
            "No API key was given, so Entra ID (DefaultAzureCredential) would be used, which needs "
            'azure-identity: pip install "duckduck[azure]" — or pass an api_key.'
        ) from exc
    credential = (  # same tenant pinning as the ADX connector and AzureKeyVaultSecrets
        DefaultAzureCredential(interactive_browser_tenant_id=tenant_id, additionally_allowed_tenants=[tenant_id])
        if tenant_id else DefaultAzureCredential()
    )
    return get_bearer_token_provider(credential, scope)


class LLMClient(Protocol):
    def generate(self, system: str, prompt: str, output_model: Type[T]) -> T:
        """Returns ``output_model`` parsed from the model's answer."""


class ClaudeLLM:
    """
    Parameters
    ----------
    model : str
        Claude model id. Default ``claude-opus-5``.
    api_key : str, optional
        Omitted → the SDK's own resolution (``ANTHROPIC_API_KEY``, an
        ``ant auth login`` profile, ...).
    effort : str, optional
        ``low`` / ``medium`` / ``high`` / ``xhigh`` / ``max``. ``None`` →
        the model's default.
    fallbacks : str or None
        Server-side refusal fallback (``"default"`` routes a declined
        request to a fallback model inside the same call). Claude API
        only — set ``None`` when going through a gateway/cloud platform
        that rejects it.
    """

    DEFAULT_MODEL = "claude-opus-5"
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        max_tokens: int = 16000,
        effort: Optional[str] = None,
        fallbacks: Optional[str] = "default",
        base_url: Optional[str] = None,
        default_headers: Optional[Mapping[str, str]] = None,
        client: Any = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.fallbacks = fallbacks
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ImportError('ClaudeLLM needs the Anthropic SDK: pip install "duckduck[llm]"') from exc
            kwargs = {}
            if api_key:
                kwargs["api_key"] = api_key
            if base_url:
                kwargs["base_url"] = base_url
            if default_headers:
                kwargs["default_headers"] = dict(default_headers)
            client = anthropic.Anthropic(**kwargs)
            _require_credentials(client, default_headers)
        self.client = client

    @classmethod
    def on_foundry(
        cls,
        model: str = DEFAULT_MODEL,
        resource: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        tenant_id: Optional[str] = None,
        token_scope: str = AZURE_AI_SCOPE,
        default_headers: Optional[Mapping[str, str]] = None,
        fallbacks: Optional[str] = None,
        **kwargs,
    ) -> "ClaudeLLM":
        """
        Claude deployed on Microsoft Foundry (Azure). ``model`` is the
        deployment name. The endpoint is ``resource`` (→
        ``https://{resource}.services.ai.azure.com/anthropic/``) or a full
        ``base_url`` (``ANTHROPIC_FOUNDRY_RESOURCE`` /
        ``ANTHROPIC_FOUNDRY_BASE_URL`` when omitted). Auth: ``api_key``
        (or ``ANTHROPIC_FOUNDRY_API_KEY``), else Entra ID. ``fallbacks``
        defaults to off — the server-side refusal fallback is a Claude API
        feature.
        """
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError('ClaudeLLM needs the Anthropic SDK: pip install "duckduck[llm]"') from exc
        resource = resource or os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE")
        base_url = base_url or os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL")
        if not (resource or base_url):
            raise ValueError(
                "Claude on Foundry needs its endpoint: 'resource' (the Foundry resource name, as in "
                "https://<resource>.services.ai.azure.com) or a full 'endpoint'/base_url — in the LLM's "
                "declaration, its authentication secret, or ANTHROPIC_FOUNDRY_RESOURCE / "
                "ANTHROPIC_FOUNDRY_BASE_URL."
            )
        api_key = api_key or os.environ.get("ANTHROPIC_FOUNDRY_API_KEY")
        client_kwargs = {"base_url": base_url} if base_url else {"resource": resource}
        if api_key:
            client_kwargs["api_key"] = api_key
        else:
            client_kwargs["azure_ad_token_provider"] = azure_token_provider(tenant_id, token_scope)
        if default_headers:
            client_kwargs["default_headers"] = dict(default_headers)
        client = anthropic.AnthropicFoundry(**client_kwargs)
        return cls(model=model, fallbacks=fallbacks, client=client, **kwargs)

    @classmethod
    def from_secret(cls, secret: Mapping[str, Any], **overrides) -> "ClaudeLLM":
        """Builds from a resolved ``authentication`` block (``api_key`` optional)."""
        return cls(api_key=secret.get("api_key"), **overrides)

    def generate(self, system: str, prompt: str, output_model: Type[T]) -> T:
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=output_model,
        )
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            response = self.client.beta.messages.parse(
                betas=[self.FALLBACK_BETA], fallbacks=self.fallbacks, **kwargs
            )
        else:
            response = self.client.messages.parse(**kwargs)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(f"the model declined the request ({getattr(details, 'category', None)})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"answer truncated at max_tokens={self.max_tokens}; raise it")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LLMError("the model returned no parseable output")
        return parsed


class AzureOpenAILLM:
    """
    An Azure OpenAI deployment, with structured outputs
    (``chat.completions.parse(response_format=Model)``).

    Parameters
    ----------
    deployment : str
        The deployment name (what Azure calls the model you deployed).
    endpoint : str, optional
        ``https://<resource>.openai.azure.com`` (or a gateway in front of
        it). Default: ``AZURE_OPENAI_ENDPOINT``.
    api_version : str, optional
        Default: ``OPENAI_API_VERSION``, else ``DEFAULT_API_VERSION``
        (structured outputs need 2024-08-01-preview or later).
    api_key : str, optional
        Default: ``AZURE_OPENAI_API_KEY``; neither → Entra ID
        (``DefaultAzureCredential``, optionally pinned by ``tenant_id``).
    reasoning_effort : str, optional
        For reasoning deployments (``low`` / ``medium`` / ``high``).
    default_headers : dict, optional
        Extra headers on every request (e.g. an API-management gateway's
        subscription key).
    """

    DEFAULT_API_VERSION = "2024-10-21"

    def __init__(
        self,
        deployment: str,
        endpoint: Optional[str] = None,
        api_version: Optional[str] = None,
        api_key: Optional[str] = None,
        tenant_id: Optional[str] = None,
        token_scope: str = AZURE_AI_SCOPE,
        max_tokens: int = 16000,
        reasoning_effort: Optional[str] = None,
        default_headers: Optional[Mapping[str, str]] = None,
        client: Any = None,
    ):
        self.deployment = deployment
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        if client is None:
            try:
                import openai
            except ImportError as exc:
                raise ImportError('AzureOpenAILLM needs the OpenAI SDK: pip install "duckduck[azure-openai]"') from exc
            endpoint = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
            if not endpoint:
                raise ValueError(
                    "Azure OpenAI needs its endpoint (https://<resource>.openai.azure.com): 'endpoint' in "
                    "the LLM's declaration, in its authentication secret, or AZURE_OPENAI_ENDPOINT."
                )
            kwargs = {
                "azure_endpoint": endpoint,
                "api_version": api_version or os.environ.get("OPENAI_API_VERSION") or self.DEFAULT_API_VERSION,
            }
            api_key = api_key or os.environ.get("AZURE_OPENAI_API_KEY")
            if api_key:
                kwargs["api_key"] = api_key
            else:
                kwargs["azure_ad_token_provider"] = azure_token_provider(tenant_id, token_scope)
            if default_headers:
                kwargs["default_headers"] = dict(default_headers)
            client = openai.AzureOpenAI(**kwargs)
        self.client = client

    def generate(self, system: str, prompt: str, output_model: Type[T]) -> T:
        kwargs = dict(
            model=self.deployment,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            response_format=output_model,
            max_completion_tokens=self.max_tokens,
        )
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        completions = self.client.chat.completions
        if not hasattr(completions, "parse"):  # openai < 1.92 only has it under beta
            completions = self.client.beta.chat.completions
        try:
            response = completions.parse(**kwargs)
        except Exception as exc:
            # the SDK raises these itself instead of returning the finish reason
            name = type(exc).__name__
            if name == "LengthFinishReasonError":
                raise LLMError(f"answer truncated at max_tokens={self.max_tokens}; raise it") from exc
            if name == "ContentFilterFinishReasonError":
                raise LLMError("the answer was blocked by the Azure content filter") from exc
            raise
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise LLMError(f"the model declined the request ({choice.message.refusal})")
        if choice.finish_reason == "length":
            raise LLMError(f"answer truncated at max_tokens={self.max_tokens}; raise it")
        if choice.finish_reason == "content_filter":
            raise LLMError("the answer was blocked by the Azure content filter")
        parsed = getattr(choice.message, "parsed", None)
        if parsed is None:
            raise LLMError("the model returned no parseable output")
        return parsed

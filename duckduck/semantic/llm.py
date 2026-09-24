"""
LLM access for the two jobs the architecture gives a generative model:
drafting the semantic catalog, and extracting values from questions.
Never for writing SQL, and never for the judgments JEV makes.

``LLMClient`` is the protocol (one method: system prompt + user content →
a validated Pydantic object). ``ClaudeLLM`` implements it with the
Anthropic SDK's structured outputs (``messages.parse``), so the response
is schema-constrained JSON already parsed into the requested model.
Another provider plugs in by implementing ``generate``.
"""

from typing import Any, Mapping, Optional, Protocol, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


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
            client = anthropic.Anthropic(**kwargs)
        self.client = client

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

"""
Configuration for ``duckduck.semantic`` — the ``"semantic"`` section of
the same ``duckduck.json`` that ``auto_register()`` reads (or its own
file). Credentials use the exact ``authentication`` block shapes the
connectors use (``local`` / ``aws`` / ``azure``, ``$secret.<key>``), so a
Jev or LLM key lives in the same secret store as everything else.

Every AI model is declared once, by a name you choose, in the file's
top-level ``"ai_providers"`` section (next to ``"services"``): where it
runs (``provider``), which model, credentials. ``semantic`` only refers to
entries by name — ``default_llm`` / each stage's ``llm`` for text
generation, ``decision_engine.ai_provider`` for the judgments. An entry
with ``provider: "jev"`` is Jev's native decisions API; any other entry
(OpenRouter, Claude, Azure) can serve either role.

::

    "ai_providers": {
      "claude": {
        "provider": "anthropic", "model": "claude-opus-5",
        "authentication": {"type": "azure", "vault_url": "https://kv.vault.azure.net/", "secret_id": "anthropic"}
      },
      "jev": {
        "provider": "jev", "model": "typesafe-ai/jev",
        "authentication": {"type": "aws", "secret_id": "prod/jev", "api_key": "$secret.key"}
      }
    },
    "semantic": {
      "catalog_path": "semantic_catalog.yaml",
      "decision_engine": {"ai_provider": "jev"},
      "default_llm": "claude",
      "extractor": {"type": "llm", "system_prompt_file": "prompts/extraction.md"},
      "catalog_generation": {"max_age": "7d", "source_prompt_file": "prompts/catalog_source.md"}
    }

Every ``*_prompt`` has a ``*_prompt_file`` twin (path relative to the
config file); omitted → the built-in default prompt.
"""

import json
import logging
import os
from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .generation import TableSpec
from .intent import Thresholds

logger = logging.getLogger("duckduck.semantic.llm")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionEngineConfig(_Strict):
    """
    ``{}`` / ``{"type": "lexical"}`` → the offline lexical baseline.
    ``{"ai_provider": "<name>"}`` → an ``ai_providers`` entry: Jev's native
    API (``provider: "jev"``) or any chat model (OpenRouter, Claude, Azure),
    asked for probabilities through structured output.
    """

    type: Literal["lexical"] = "lexical"
    ai_provider: Optional[str] = None
    #: Per-decision timeout (seconds) and retries on top of it.
    timeout: Optional[float] = None
    retries: int = 2
    #: System prompt for a chat-model decision engine (not used by Jev's native API).
    system_prompt: Optional[str] = None
    system_prompt_file: Optional[str] = None


class LiveEvidenceConfig(_Strict):
    """Probe the question's values in candidate sources before deciding — see ``semantic.evidence``."""

    enabled: bool = False
    #: Probes per question at most (each is one tiny query at a source).
    max_probes: int = Field(default=8, ge=0)
    #: Seconds per probe before it counts as "couldn't check".
    timeout: float = Field(default=5.0, gt=0)


class AIProviderConfig(_Strict):
    """
    One entry of the top-level ``ai_providers`` section. ``provider``:

    - ``anthropic`` — the Claude API (or a gateway in front of it: ``base_url`` + ``headers``).
    - ``foundry`` — Claude deployed on Microsoft Foundry (Azure): ``resource`` or ``endpoint``;
      ``model`` is the deployment name.
    - ``azure_openai`` — an Azure OpenAI deployment: ``endpoint``, ``deployment``, ``api_version``.
    - ``openrouter`` — any model on OpenRouter (``model`` = its ``vendor/model`` id). With
      ``api: "decisions"``, OpenRouter's Decisions API instead of chat — how Jev is served there
      (``model: "typesafe/jev-1.13"``).
    - ``jev`` — TypeSafe's own Decisions API (``api`` is always ``decisions``).

    ``api``: ``chat`` (text generation; usable as an LLM, or as a decision engine asked for
    probabilities through structured output) or ``decisions`` (a typed Decisions API —
    ``noul``/``choice`` questions, probabilities back; decision engine only). ``decisions_url``
    points a ``decisions`` entry at any other host serving that API.

    Credentials: the ``authentication`` block's ``api_key``, else the provider's env var
    (``ANTHROPIC_API_KEY`` / ``ANTHROPIC_FOUNDRY_API_KEY`` / ``AZURE_OPENAI_API_KEY`` /
    ``OPENROUTER_API_KEY`` / ``JEV_API_KEY``), else — Azure providers only — Entra ID
    (``DefaultAzureCredential``, pinned by ``tenant_id``). Every connection setting
    (``endpoint``, ``deployment``, ``api_version``, ``resource``, ``tenant_id``, ``base_url``) may
    also come from the authentication secret instead of the file; ``headers`` values written
    ``"$secret.<key>"`` do too.
    """

    provider: Literal["anthropic", "foundry", "azure_openai", "openrouter", "jev"] = "anthropic"
    #: Omitted → ``decisions`` for ``jev``, ``chat`` for everything else.
    api: Optional[Literal["chat", "decisions"]] = None
    #: Full Decisions API endpoint, overriding the provider's default.
    decisions_url: Optional[str] = None
    #: anthropic / foundry: default ``claude-opus-5``; openrouter: required; jev: the server's default.
    model: Optional[str] = None
    max_tokens: int = 16000
    #: Claude: ``output_config.effort``; Azure OpenAI: ``reasoning_effort``; OpenRouter: ``reasoning.effort``.
    effort: Optional[Literal["minimal", "low", "medium", "high", "xhigh", "max"]] = None
    #: Claude API only (server-side refusal fallback); off by default on Foundry.
    fallbacks: Optional[str] = "default"
    base_url: Optional[str] = None
    endpoint: Optional[str] = None
    resource: Optional[str] = None
    deployment: Optional[str] = None
    api_version: Optional[str] = None
    tenant_id: Optional[str] = None
    #: OpenRouter: route only to providers that honour structured output.
    require_parameters: bool = True
    #: Decisions API HTTP timeout (seconds).
    timeout: float = 15.0
    headers: Dict[str, str] = Field(default_factory=dict)
    #: Omitted → the provider's env var (Entra ID on Azure).
    authentication: Optional[Dict[str, Any]] = None

    CLAUDE_DEFAULT_MODEL: ClassVar[str] = "claude-opus-5"
    CHAT_PROVIDERS: ClassVar[Tuple[str, ...]] = ("anthropic", "foundry", "azure_openai", "openrouter")
    DECISIONS_PROVIDERS: ClassVar[Tuple[str, ...]] = ("jev", "openrouter")
    _ONLY_FOR: ClassVar[Dict[str, tuple]] = {
        "resource": ("foundry",),
        "deployment": ("azure_openai",),
        "api_version": ("azure_openai",),
        "endpoint": ("foundry", "azure_openai"),
        "tenant_id": ("foundry", "azure_openai"),
        "fallbacks": ("anthropic", "foundry"),
        "base_url": ("anthropic", "openrouter", "jev"),
        "require_parameters": ("openrouter",),
        "max_tokens": ("anthropic", "foundry", "azure_openai", "openrouter"),
        "effort": ("anthropic", "foundry", "azure_openai", "openrouter"),
        "headers": ("anthropic", "foundry", "azure_openai", "openrouter"),
    }

    @model_validator(mode="after")
    def _fields_match_provider(self) -> "AIProviderConfig":
        for name, providers in self._ONLY_FOR.items():
            if name in self.model_fields_set and self.provider not in providers:
                raise ValueError(
                    f"'{name}' applies to provider {' / '.join(repr(p) for p in providers)}, "
                    f"not {self.provider!r}"
                )
        if self.provider == "openrouter" and not self.model:
            raise ValueError("provider 'openrouter' needs 'model' (the vendor/model id from openrouter.ai/models)")
        if self.effective_api == "decisions" and self.provider not in self.DECISIONS_PROVIDERS and not self.decisions_url:
            raise ValueError(
                f"provider {self.provider!r} has no Decisions API — use api 'chat', or set decisions_url"
            )
        if self.effective_api == "chat" and self.provider == "jev":
            raise ValueError("provider 'jev' only serves the Decisions API (api 'decisions')")
        chat_only = [k for k in ("max_tokens", "effort", "headers", "require_parameters") if k in self.model_fields_set]
        if self.effective_api == "decisions" and chat_only:
            raise ValueError(f"{chat_only} only apply to api 'chat'")
        if self.effective_api == "chat" and {"decisions_url", "timeout"} & self.model_fields_set:
            raise ValueError("'decisions_url' / 'timeout' only apply to api 'decisions'")
        return self

    @property
    def effective_api(self) -> str:
        return self.api or ("decisions" if self.provider == "jev" else "chat")

    @property
    def is_chat(self) -> bool:
        return self.effective_api == "chat"

    @property
    def model_name(self) -> Optional[str]:
        if self.provider == "azure_openai":
            return self.deployment
        if self.provider in ("anthropic", "foundry"):
            return self.model or self.CLAUDE_DEFAULT_MODEL
        return self.model


#: The previous name, kept for code that imports it.
LLMConfig = AIProviderConfig


#: Where an LLM is wanted: the name of a (chat) entry in the file's top-level ``ai_providers``.
LLMRef = str


class ExtractorConfig(_Strict):
    type: Literal["rules", "llm"] = "rules"
    #: This stage's LLM (an ``ai_providers`` name). Omitted → ``default_llm``.
    llm: Optional[LLMRef] = None
    system_prompt: Optional[str] = None
    system_prompt_file: Optional[str] = None
    on_error: Literal["fallback", "raise"] = "fallback"
    #: Read questions asked in other languages in English, in the same LLM call (``llm`` only).
    translate: bool = True


class ApiDocsConfig(_Strict):
    """Where one table's (or service's) API docs are: a file/URL, or the docs inline."""

    #: File path (relative to this config file's folder) or http(s) URL:
    #: an OpenAPI/Swagger spec (JSON/YAML), field docs (JSON/YAML), or text (Markdown/HTML).
    location: Optional[str] = None
    #: The docs inline — a spec or field-docs object, or text.
    content: Optional[Union[Dict[str, Any], str]] = None
    #: The spec operation behind the table (``"GET /api/3/assets"``, a path, or an operationId).
    operation: Optional[str] = None

    @model_validator(mode="after")
    def _one_of(self) -> "ApiDocsConfig":
        if (self.location is None) == (self.content is None):
            raise ValueError("api_docs entries need exactly one of 'location' (file or URL) or 'content' (inline)")
        return self


class FeedbackConfig(_Strict):
    """What users say about answers, and what's learned from it (see ``duckduck.semantic.feedback``)."""

    #: Record every search and accept feedback on it.
    enabled: bool = False
    #: The DuckDB file holding searches and feedback (relative to this config file's folder).
    path: str = "feedback.duckdb"
    #: Give the decision engine similar questions users confirmed before, as evidence.
    memory: bool = True
    #: At most this many similar cases per question, each at least this similar (0–1).
    max_cases: int = Field(default=3, ge=1, le=10)
    #: How alike (0–1, over the question's words with its values set aside) a past question must be to count.
    min_similarity: float = Field(default=0.6, ge=0.0, le=1.0)
    #: Where accepted answer-wording suggestions are written — loaded on top of ``answer_shapes``.
    learned_answer_shapes: str = "answer_shapes.learned.yaml"
    #: A suggestion needs at least this many failed questions behind it (1: every correction counts).
    min_support: int = Field(default=2, ge=1)


class CatalogGenerationConfig(_Strict):
    #: The file generation maintains. Omitted → ``catalog_path`` itself —
    #: the catalog ``ask`` reads, updated in place (drafted sources
    #: replaced/added, hand-written ones and your notes kept). Set it to
    #: draft into a separate file for review instead.
    output_path: Optional[str] = None
    #: A generated source older than this is redrafted on the next run:
    #: ``"7d"``, ``"12h"``, ``"30m"``, or seconds. Omitted → never expires
    #: (only new tables, and what ``force`` names, get drafted).
    max_age: Optional[Union[int, float, str]] = None
    #: Refresh what's missing or expired every time a ``SemanticSearch`` is
    #: built from this config (``ask``, ``from_config``) — before answering.
    #: Only when generation maintains ``catalog_path`` itself.
    auto_refresh: bool = False
    sample_rows: int = Field(default=5, ge=0)
    source_prompt: Optional[str] = None
    source_prompt_file: Optional[str] = None
    link_prompt: Optional[str] = None
    link_prompt_file: Optional[str] = None
    #: Explicit list of tables to draft. Omitted → every plain table plus
    #: every table discovered through connector catalogs (Glue, ADX, SQL
    #: databases), filtered by include/exclude and capped by max_tables.
    tables: Optional[List[TableSpec]] = None
    #: Walk connector catalogs to find the tables behind table functions.
    discover: bool = True
    #: fnmatch patterns (case-insensitive) on the registered name, or the
    #: joined arguments of a discovered table ("security.proxy_*").
    include: List[str] = Field(default_factory=list)
    exclude: List[str] = Field(default_factory=list)
    #: Cap on sources drafted per run — each one is an LLM call. The rest
    #: are drafted on later runs.
    max_tables: int = Field(default=50, ge=1)
    #: Rows read per table to compute each field's profile (distinct values,
    #: empty share, min/max, the source's date range, category values). 0: none.
    profile_rows: int = Field(default=1000, ge=0)
    #: Real example values stored per field in the catalog (0: none — they
    #: write real data into the file).
    sample_values: int = Field(default=0, ge=0)
    #: Store example values for user/email fields too.
    sample_sensitive: bool = False
    #: A text column with at most this many distinct (repeating) values
    #: becomes a value list automatically.
    max_enum_values: int = Field(default=20, ge=0)
    #: API documentation per table: ``{selector: location | {location | content, operation}}``.
    #: Selectors as in ``force``/``only`` (``"insightvm"``, ``"servicenow:incidents"``,
    #: ``"myapi:*"``); a ``service:table`` entry beats a whole-service one.
    api_docs: Dict[str, Union[str, ApiDocsConfig]] = Field(default_factory=dict)
    #: Budget per table for an excerpt of a text document (Markdown/HTML).
    api_docs_max_chars: int = Field(default=6000, ge=200)

    @model_validator(mode="after")
    def _check(self) -> "CatalogGenerationConfig":
        from .generation import parse_age

        parse_age(self.max_age)  # fails at load on a malformed duration
        return self
    #: LLM for drafting each table (one call per table) — an
    #: ``ai_providers`` name. Omitted → ``default_llm``.
    llm: Optional[LLMRef] = None
    #: LLM for the final call (entities, activities, joins across every
    #: table). Omitted → this section's ``llm``, then ``default_llm``.
    link_llm: Optional[LLMRef] = None


class SemanticConfig(_Strict):
    catalog_path: str = "semantic_catalog.yaml"
    decision_engine: DecisionEngineConfig = Field(default_factory=DecisionEngineConfig)
    #: The named AI models — filled by ``load``/``from_file_data`` from the
    #: file's top-level ``"ai_providers"`` section, never written inside ``"semantic"``.
    ai_providers: Dict[str, AIProviderConfig] = Field(default_factory=dict)
    #: The LLM every stage uses unless it names its own (an ``ai_providers`` name).
    default_llm: Optional[LLMRef] = None
    extractor: ExtractorConfig = Field(default_factory=ExtractorConfig)
    catalog_generation: CatalogGenerationConfig = Field(default_factory=CatalogGenerationConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    #: Override the questions asked back to the user (``clarify.DEFAULT_TEXTS`` keys) — e.g. in Portuguese.
    clarification_texts: Dict[str, str] = Field(default_factory=dict)
    #: Wording of each kind of answer, added to the defaults (``shapes.DEFAULT_WORDING``):
    #: ``{shape: {wording: [...], maybe_wording: [...], description, replace}}``, or the path of a
    #: JSON/YAML file holding that — e.g. ``"answer_shapes.yaml"``.
    answer_shapes: Optional[Union[str, Dict[str, Any]]] = None
    #: Record searches and feedback; case memory; catalog suggestions.
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    #: Check the question's values live in candidate sources and tell the decision engine.
    live_evidence: LiveEvidenceConfig = Field(default_factory=LiveEvidenceConfig)
    allowed_sources: Optional[List[str]] = None
    default_limit: int = 1000
    strict: bool = False
    #: Directory relative paths resolve against (the config file's own).
    base_dir: str = "."
    #: The file this was loaded from (for error messages).
    config_file: Optional[str] = None

    #: stage → where to look for its LLM, first match wins (then ``default_llm``).
    LLM_STAGES: ClassVar[Dict[str, Tuple[str, ...]]] = {
        "extractor": ("extractor.llm",),
        "catalog_generation": ("catalog_generation.llm",),
        "catalog_link": ("catalog_generation.link_llm", "catalog_generation.llm"),
    }

    @model_validator(mode="after")
    def _references_exist(self) -> "SemanticConfig":
        declared = ", ".join(sorted(self.ai_providers)) or "none"
        refs = {"default_llm": self.default_llm, **{path: self._at(path) for paths in self.LLM_STAGES.values() for path in paths}}
        for where, ref in refs.items():
            if ref is None:
                continue
            if ref not in self.ai_providers:
                raise ValueError(
                    f"semantic.{where} refers to {ref!r}, which the top-level 'ai_providers' section doesn't "
                    f"declare (declared: {declared})"
                )
            if not self.ai_providers[ref].is_chat:
                raise ValueError(
                    f"semantic.{where} refers to {ref!r}, a Decisions API entry — that can only be a "
                    f"decision_engine, not an LLM"
                )
        engine_ref = self.decision_engine.ai_provider
        if engine_ref is not None and engine_ref not in self.ai_providers:
            raise ValueError(
                f"semantic.decision_engine.ai_provider refers to {engine_ref!r}, which the top-level "
                f"'ai_providers' section doesn't declare (declared: {declared})"
            )
        from .clarify import ClarificationTexts

        ClarificationTexts(self.clarification_texts)  # unknown keys/placeholders fail at load
        from .shapes import AnswerShapes, load_answer_shapes

        AnswerShapes(self.answer_shape_overrides())  # unknown shapes / bad regexes too
        cg = self.catalog_generation
        if cg.auto_refresh and cg.output_path and self.path(cg.output_path) != self.path(self.catalog_path):
            raise ValueError(
                "catalog_generation.auto_refresh only applies when generation maintains catalog_path itself "
                "— remove output_path (or point it at catalog_path)"
            )
        return self

    def _at(self, path: str) -> Optional[LLMRef]:
        section, key = path.split(".")
        return getattr(getattr(self, section), key)

    def llm_config(self, stage: Optional[str] = None) -> Tuple[Optional[str], Optional[AIProviderConfig]]:
        """
        ``(name, settings)`` of the LLM a stage uses: its own ``llm`` (for
        ``catalog_link``: ``link_llm``, then ``catalog_generation.llm``),
        else ``default_llm``; ``(None, None)`` when none is set.
        """
        paths = self.LLM_STAGES.get(stage, ()) if stage else ()
        for ref in [*(self._at(p) for p in paths), self.default_llm]:
            if ref is not None:
                return ref, self.ai_providers[ref]
        return None, None

    @classmethod
    def load(cls, duck: Any, config_path: Optional[str] = None, section: str = "semantic") -> "SemanticConfig":
        """
        Same lookup as ``auto_register()``: ``config_path`` →
        ``DUCKDUCK_CONFIG`` → ``duckduck.json`` from the current directory
        upward.
        """
        path = config_path or os.environ.get("DUCKDUCK_CONFIG") or duck._find_default_config_file()
        if not path or not os.path.isfile(path):
            raise ValueError(f"no config file found{f' at {path!r}' if path else ''}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_file_data(data, path, section)

    @classmethod
    def from_file_data(cls, data: Dict[str, Any], path: str = "", section: str = "semantic") -> "SemanticConfig":
        """From a parsed config file: its ``section`` plus the top-level ``ai_providers``."""
        where = f"config file '{path}'" if path else "the config"
        if section not in data:
            raise ValueError(f"{where} has no '{section}' section")
        semantic = data[section]
        if "llms" in data or "llms" in semantic:
            raise ValueError(
                f"{where}: 'llms' is now 'ai_providers', at the top level of the file (next to 'services') — "
                f"it declares every AI model by name: LLMs and the decision engine (Jev) alike."
            )
        if "ai_providers" in semantic:
            raise ValueError(
                f"{where}: 'ai_providers' goes at the top level of the file (next to 'services'), not inside "
                f"'{section}' — '{section}' only refers to its entries by name."
            )
        if "llm" in semantic:
            raise ValueError(
                f"{where}: '{section}.llm' is now '{section}.default_llm' (the LLM every stage uses unless "
                f"it names its own) — rename the key."
            )
        for ref_path in ("default_llm", "extractor.llm", "catalog_generation.llm", "catalog_generation.link_llm"):
            *parent, key = ref_path.split(".")
            holder = semantic.get(parent[0], {}) if parent else semantic
            value = holder.get(key) if isinstance(holder, dict) else None
            if isinstance(value, dict):
                raise ValueError(
                    f"{where}: '{section}.{ref_path}' is the name of an ai_providers entry, not a block. "
                    f"Declare it in the top-level 'ai_providers' section and refer to it by name, e.g.\n"
                    f'    "ai_providers": {{"my_llm": {json.dumps(value)}}},\n'
                    f"    ... \"{ref_path.split('.')[-1]}\": \"my_llm\""
                )
        engine = semantic.get("decision_engine") or {}
        legacy = {k: engine[k] for k in ("model", "base_url", "authentication") if k in engine}
        if engine.get("type") == "jev" or legacy:
            entry = {"provider": "jev", **legacy}
            raise ValueError(
                f"{where}: the decision engine is now an 'ai_providers' entry named by "
                f"'{section}.decision_engine.ai_provider', e.g.\n"
                f'    "ai_providers": {{"jev": {json.dumps(entry)}}},\n'
                f'    "{section}": {{"decision_engine": {{"ai_provider": "jev"}}, ...}}'
            )
        base = os.path.dirname(os.path.abspath(path)) if path else "."
        return cls.model_validate({
            **semantic,
            "ai_providers": data.get("ai_providers", {}),
            "base_dir": base,
            "config_file": os.path.abspath(path) if path else None,
        })

    def answer_shape_overrides(self) -> Dict[str, Any]:
        """``answer_shapes`` plus the wording accepted from feedback suggestions (``feedback.learned_answer_shapes``)."""
        from .shapes import load_answer_shapes, merge_answer_shapes

        overrides = load_answer_shapes(self.answer_shapes, self.base_dir)
        learned = self.path(self.feedback.learned_answer_shapes)
        if os.path.exists(learned):
            overrides = merge_answer_shapes(overrides, load_answer_shapes(learned, self.base_dir))
        return overrides

    def build_feedback(self):
        """The ``FeedbackStore`` (``None`` unless ``feedback.enabled``)."""
        if not self.feedback.enabled:
            return None
        from .feedback import FeedbackStore

        return FeedbackStore(self.path(self.feedback.path))

    def path(self, relative: str) -> str:
        return relative if os.path.isabs(relative) else os.path.join(self.base_dir, relative)

    def prompt(self, inline: Optional[str], file: Optional[str], default: str) -> str:
        if inline:
            return inline
        if file:
            with open(self.path(file), "r", encoding="utf-8") as f:
                return f.read()
        return default

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def build_engine(self, duck: Any):
        from .decisions import JEVAdapter, LexicalDecisionEngine

        cfg = self.decision_engine
        if cfg.ai_provider is None:
            return LexicalDecisionEngine()
        entry = self.ai_providers[cfg.ai_provider]
        logger.info("decision engine: %s (%s %s)", cfg.ai_provider, entry.provider, entry.model_name)
        if entry.effective_api == "decisions":
            from .jev import JevClient

            creds = duck.resolve_credentials(entry.authentication, f"ai_providers.{cfg.ai_provider}") \
                if entry.authentication else {}
            on_openrouter = entry.provider == "openrouter"
            url = entry.decisions_url or creds.get("decisions_url") or (
                JevClient.OPENROUTER_DECISIONS_URL if on_openrouter else None
            )
            client = JevClient(
                api_key=creds.get("api_key"), url=url, model=entry.model, timeout=entry.timeout,
                base_url=entry.base_url or creds.get("base_url") or JevClient.DEFAULT_BASE_URL,
                api_key_env="OPENROUTER_API_KEY" if on_openrouter else "JEV_API_KEY",
            )
            # the adapter's timeout is a backstop above the HTTP client's own
            return JEVAdapter(client, retries=cfg.retries, timeout=cfg.timeout or entry.timeout + 5)
        from .llm_decisions import DEFAULT_DECISION_PROMPT, LLMDecisionBackend

        backend = LLMDecisionBackend(
            self._build_client(cfg.ai_provider, entry, duck),
            system_prompt=self.prompt(cfg.system_prompt, cfg.system_prompt_file, DEFAULT_DECISION_PROMPT),
        )
        return JEVAdapter(backend, retries=cfg.retries, timeout=cfg.timeout or 60.0)

    def build_llm(self, duck: Any, stage: Optional[str] = None):
        """The LLM client for ``stage`` (``extractor`` / ``catalog_generation`` / ``catalog_link``)."""
        name, cfg = self.llm_config(stage)
        if cfg is None:
            where = f"the 'semantic' section of {self.config_file}" if self.config_file else "the semantic config"
            own = self.LLM_STAGES.get(stage, ())
            raise ValueError(
                f"This needs an LLM (catalog generation / extractor type 'llm'), but {where} names none "
                f"('default_llm'{f' or {own[0]!r}' if own else ''}). Declare one in the file's top-level "
                f"\"ai_providers\" section and name it in \"semantic\", e.g.\n"
                f'    "ai_providers": {{"claude": {{"provider": "anthropic", "model": "claude-opus-5"}}}},\n'
                f'    "semantic": {{"default_llm": "claude", ...}}\n'
                f"(API key from the ANTHROPIC_API_KEY environment variable, or an \"authentication\" "
                f"block like the connectors'), and `pip install -e \".[llm]\"`. "
                f"See examples/semantic/duckduck.online.json."
            )
        logger.info("llm for %s: %s (%s %s)", stage or "default", name, cfg.provider, cfg.model_name)
        return self._build_client(name, cfg, duck)

    def _build_client(self, name: str, cfg: AIProviderConfig, duck: Any):
        from .llm import AzureOpenAILLM, ClaudeLLM, OpenRouterLLM

        creds = duck.resolve_credentials(cfg.authentication, f"ai_providers.{name}") if cfg.authentication else {}
        headers = _resolve_headers(cfg.headers, creds) or None

        def setting(key: str) -> Optional[str]:
            return getattr(cfg, key) or creds.get(key)

        api_key = creds.get("api_key")
        if cfg.provider == "azure_openai":
            if not setting("deployment"):
                raise ValueError(
                    f"ai_providers.{name}: provider 'azure_openai' needs 'deployment' (the name you gave the "
                    f"model deployment in Azure), in the entry or its authentication secret."
                )
            return AzureOpenAILLM(
                deployment=setting("deployment"), endpoint=setting("endpoint"), api_version=setting("api_version"),
                api_key=api_key, tenant_id=setting("tenant_id"), max_tokens=cfg.max_tokens,
                reasoning_effort=cfg.effort, default_headers=headers,
            )
        if cfg.provider == "openrouter":
            return OpenRouterLLM(
                model=cfg.model, api_key=api_key, base_url=setting("base_url"), max_tokens=cfg.max_tokens,
                reasoning_effort=cfg.effort, require_parameters=cfg.require_parameters, default_headers=headers,
            )
        if cfg.provider == "foundry":
            return ClaudeLLM.on_foundry(
                model=cfg.model_name, resource=setting("resource"), base_url=setting("endpoint") or setting("base_url"),
                api_key=api_key, tenant_id=setting("tenant_id"), default_headers=headers,
                fallbacks=cfg.fallbacks if "fallbacks" in cfg.model_fields_set else None,
                max_tokens=cfg.max_tokens, effort=cfg.effort,
            )
        gateway_auth = any(h.lower() in ("x-api-key", "authorization") for h in cfg.headers)
        if cfg.authentication and not api_key and not gateway_auth:
            raise ValueError(
                f"ai_providers.{name}: the authentication block resolved to keys {sorted(creds)} but no "
                f"'api_key'. If the secret stores it under another name, map it: \"api_key\": \"$secret.<its key>\"."
            )
        return ClaudeLLM(
            model=cfg.model_name, api_key=api_key, max_tokens=cfg.max_tokens,
            effort=cfg.effort, fallbacks=cfg.fallbacks, base_url=setting("base_url"), default_headers=headers,
        )

    def llm_label(self, stage: Optional[str] = None) -> Optional[str]:
        name, cfg = self.llm_config(stage)
        return f"{name} ({cfg.provider} {cfg.model_name})" if cfg else None

    def catalog_target(self, out: Optional[str] = None) -> str:
        """The file catalog generation reads and updates."""
        return out or self.path(self.catalog_generation.output_path or self.catalog_path)

    def build_extractor(self, catalog, duck: Any):
        from .llm_extraction import DEFAULT_EXTRACTION_PROMPT, LLMExtractor

        cfg = self.extractor
        if cfg.type == "rules":
            return None  # SemanticSearch's default
        prompt = self.prompt(cfg.system_prompt, cfg.system_prompt_file, DEFAULT_EXTRACTION_PROMPT)
        return LLMExtractor(catalog, self.build_llm(duck, "extractor"), system_prompt=prompt, on_error=cfg.on_error,
                            translate=cfg.translate)

    def build_generator(self, duck: Any):
        from .generation import DEFAULT_LINK_PROMPT, DEFAULT_SOURCE_PROMPT, CatalogGenerator

        cfg = self.catalog_generation
        source_llm = self.build_llm(duck, "catalog_generation")
        link_llm = self.build_llm(duck, "catalog_link") if cfg.link_llm is not None else None  # else same client
        return CatalogGenerator(
            source_llm, duck,
            link_llm=link_llm,
            source_prompt=self.prompt(cfg.source_prompt, cfg.source_prompt_file, DEFAULT_SOURCE_PROMPT),
            link_prompt=self.prompt(cfg.link_prompt, cfg.link_prompt_file, DEFAULT_LINK_PROMPT),
            sample_rows=cfg.sample_rows,
            discover=cfg.discover,
            include=cfg.include,
            exclude=cfg.exclude,
            max_tables=cfg.max_tables,
            max_age=cfg.max_age,
            llm_label=self.llm_label("catalog_generation"),
            link_llm_label=self.llm_label("catalog_link"),
            profile_rows=cfg.profile_rows,
            sample_values=cfg.sample_values,
            sample_sensitive=cfg.sample_sensitive,
            max_enum_values=cfg.max_enum_values,
            api_docs={sel: (ref if isinstance(ref, str) else ref.model_dump(exclude_none=True))
                      for sel, ref in cfg.api_docs.items()},
            docs_base_dir=self.base_dir,
            docs_max_chars=cfg.api_docs_max_chars,
        )


def _resolve_headers(headers: Dict[str, str], creds: Dict[str, Any]) -> Dict[str, str]:
    """``"$secret.<key>"`` header values come from the resolved authentication block."""
    resolved = {}
    for name, value in headers.items():
        if isinstance(value, str) and value.startswith("$secret."):
            key = value[len("$secret."):]
            if key not in creds:
                raise ValueError(
                    f"headers[{name!r}] refers to {value!r}, but the entry's authentication block "
                    f"resolved to keys {sorted(creds)}."
                )
            value = creds[key]
        resolved[name] = str(value)
    return resolved

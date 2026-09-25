"""
Configuration for ``duckduck.semantic`` — the ``"semantic"`` section of
the same ``duckduck.json`` that ``auto_register()`` reads (or its own
file). Credentials use the exact ``authentication`` block shapes the
connectors use (``local`` / ``aws`` / ``azure``, ``$secret.<key>``), so a
Jev or LLM key lives in the same secret store as everything else.

LLMs are declared once, by name, in the file's top-level ``"llms"``
section (next to ``"services"``); ``semantic`` only refers to them by
name — ``default_llm`` for every stage, and each stage may name its own.

::

    "llms": {
      "claude": {
        "model": "claude-opus-5",
        "authentication": {"type": "azure", "vault_url": "https://kv.vault.azure.net/", "secret_id": "anthropic"}
      }
    },
    "semantic": {
      "catalog_path": "semantic_catalog.yaml",
      "decision_engine": {
        "type": "jev",
        "model": "typesafe-ai/jev",
        "authentication": {"type": "aws", "secret_id": "prod/jev", "api_key": "$secret.key"}
      },
      "default_llm": "claude",
      "extractor": {"type": "llm", "system_prompt_file": "prompts/extraction.md"},
      "catalog_generation": {
        "output_path": "semantic_catalog.yaml",
        "sample_rows": 5,
        "source_prompt_file": "prompts/catalog_source.md",
        "tables": [{"name": "proxy_logs", "table": "glue_table",
                    "args": {"database": "sec", "table_name": "proxy"}, "notes": "..."}]
      }
    }

Every ``*_prompt`` has a ``*_prompt_file`` twin (path relative to the
config file); omitted → the built-in default prompt.
"""

import json
import logging
import os
from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .generation import TableSpec
from .intent import Thresholds

logger = logging.getLogger("duckduck.semantic.llm")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionEngineConfig(_Strict):
    type: Literal["lexical", "jev"] = "lexical"
    base_url: str = "https://www.jevai.org"
    model: Optional[str] = None
    timeout: float = 15.0
    retries: int = 2
    authentication: Optional[Dict[str, Any]] = None


class LLMConfig(_Strict):
    """
    ``provider``:

    - ``anthropic`` — the Claude API (or a gateway in front of it: ``base_url`` + ``headers``).
    - ``foundry`` — Claude deployed on Microsoft Foundry (Azure): ``resource`` or ``endpoint``;
      ``model`` is the deployment name.
    - ``azure_openai`` — an Azure OpenAI deployment: ``endpoint``, ``deployment``, ``api_version``.

    Credentials: the ``authentication`` block's ``api_key``, else the provider's env var
    (``ANTHROPIC_API_KEY`` / ``ANTHROPIC_FOUNDRY_API_KEY`` / ``AZURE_OPENAI_API_KEY``), else — Azure
    providers only — Entra ID (``DefaultAzureCredential``, pinned by ``tenant_id``). Every
    connection setting (``endpoint``, ``deployment``, ``api_version``, ``resource``, ``tenant_id``,
    ``base_url``) may also come from the authentication secret instead of the file; ``headers``
    values written ``"$secret.<key>"`` do too.
    """

    provider: Literal["anthropic", "foundry", "azure_openai"] = "anthropic"
    model: str = "claude-opus-5"
    max_tokens: int = 16000
    #: Claude: ``output_config.effort``; Azure OpenAI: ``reasoning_effort``.
    effort: Optional[Literal["minimal", "low", "medium", "high", "xhigh", "max"]] = None
    #: Claude API only (server-side refusal fallback); off by default on Foundry.
    fallbacks: Optional[str] = "default"
    base_url: Optional[str] = None
    endpoint: Optional[str] = None
    resource: Optional[str] = None
    deployment: Optional[str] = None
    api_version: Optional[str] = None
    tenant_id: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    #: Omitted → the SDK's own credential resolution (env vars; Entra ID on Azure).
    authentication: Optional[Dict[str, Any]] = None

    _ONLY_FOR: ClassVar[Dict[str, tuple]] = {
        "resource": ("foundry",),
        "deployment": ("azure_openai",),
        "api_version": ("azure_openai",),
        "endpoint": ("foundry", "azure_openai"),
        "tenant_id": ("foundry", "azure_openai"),
        "fallbacks": ("anthropic", "foundry"),
    }

    @model_validator(mode="after")
    def _fields_match_provider(self) -> "LLMConfig":
        for name, providers in self._ONLY_FOR.items():
            if name in self.model_fields_set and self.provider not in providers:
                raise ValueError(
                    f"llm.{name} applies to provider {' / '.join(repr(p) for p in providers)}, "
                    f"not {self.provider!r}"
                )
        return self


#: Where an LLM is wanted: the name of one declared in the file's top-level ``llms`` section.
LLMRef = str


class ExtractorConfig(_Strict):
    type: Literal["rules", "llm"] = "rules"
    #: This stage's LLM (a name from ``llms``). Omitted → the default ``llm``.
    llm: Optional[LLMRef] = None
    system_prompt: Optional[str] = None
    system_prompt_file: Optional[str] = None
    on_error: Literal["fallback", "raise"] = "fallback"


class CatalogGenerationConfig(_Strict):
    output_path: str = "semantic_catalog.yaml"
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
    #: Cap on tables drafted per run — each one is an LLM call.
    max_tables: int = Field(default=50, ge=1)
    #: LLM for drafting each table (one call per table) — a name from
    #: ``llms``. Omitted → the default ``llm``.
    llm: Optional[LLMRef] = None
    #: LLM for the final call (entities, activities, joins across every
    #: table). Omitted → this section's ``llm``, then ``default_llm``.
    link_llm: Optional[LLMRef] = None


class SemanticConfig(_Strict):
    catalog_path: str = "semantic_catalog.yaml"
    decision_engine: DecisionEngineConfig = Field(default_factory=DecisionEngineConfig)
    #: The named LLMs — filled by ``load``/``from_file_data`` from the file's
    #: top-level ``"llms"`` section, never written inside ``"semantic"``.
    llms: Dict[str, LLMConfig] = Field(default_factory=dict)
    #: The LLM every stage uses unless it names its own (a name from ``llms``).
    default_llm: Optional[LLMRef] = None
    extractor: ExtractorConfig = Field(default_factory=ExtractorConfig)
    catalog_generation: CatalogGenerationConfig = Field(default_factory=CatalogGenerationConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
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
    def _llm_references_exist(self) -> "SemanticConfig":
        refs = {"default_llm": self.default_llm, **{path: self._at(path) for paths in self.LLM_STAGES.values() for path in paths}}
        for where, ref in refs.items():
            if isinstance(ref, str) and ref not in self.llms:
                declared = ", ".join(sorted(self.llms)) or "none"
                raise ValueError(
                    f"semantic.{where} refers to LLM {ref!r}, which the top-level 'llms' section doesn't "
                    f"declare (declared: {declared})"
                )
        return self

    def _at(self, path: str) -> Optional[LLMRef]:
        section, key = path.split(".")
        return getattr(getattr(self, section), key)

    def llm_config(self, stage: Optional[str] = None) -> Tuple[Optional[str], Optional[LLMConfig]]:
        """
        ``(name, settings)`` of the LLM a stage uses: its own ``llm`` (for
        ``catalog_link``: ``link_llm``, then ``catalog_generation.llm``),
        else ``default_llm``; ``(None, None)`` when none is set.
        """
        paths = self.LLM_STAGES.get(stage, ()) if stage else ()
        for ref in [*(self._at(p) for p in paths), self.default_llm]:
            if ref is not None:
                return ref, self.llms[ref]
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
        """From a parsed config file: its ``section`` plus the top-level ``llms``."""
        where = f"config file '{path}'" if path else "the config"
        if section not in data:
            raise ValueError(f"{where} has no '{section}' section")
        if "llms" in data[section]:
            raise ValueError(
                f"{where}: 'llms' goes at the top level of the file (next to 'services'), not inside "
                f"'{section}' — '{section}' only refers to LLMs by name."
            )
        semantic = data[section]
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
                    f"{where}: '{section}.{ref_path}' is the name of an LLM, not a block. Declare the LLM "
                    f"in the top-level 'llms' section and refer to it by name, e.g.\n"
                    f'    "llms": {{"my_llm": {json.dumps(value)}}},\n'
                    f"    ... \"{ref_path.split('.')[-1]}\": \"my_llm\""
                )
        base = os.path.dirname(os.path.abspath(path)) if path else "."
        return cls.model_validate({
            **semantic,
            "llms": data.get("llms", {}),
            "base_dir": base,
            "config_file": os.path.abspath(path) if path else None,
        })

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
        from .jev import JevClient

        cfg = self.decision_engine
        if cfg.type == "lexical":
            return LexicalDecisionEngine()
        creds = duck.resolve_credentials(cfg.authentication, "semantic.decision_engine") if cfg.authentication else {}
        client = JevClient(
            api_key=creds.get("api_key"), base_url=cfg.base_url, model=cfg.model, timeout=cfg.timeout,
        )
        # the adapter's timeout is a backstop above the HTTP client's own
        return JEVAdapter(client, retries=cfg.retries, timeout=cfg.timeout + 5)

    def build_llm(self, duck: Any, stage: Optional[str] = None):
        """The LLM client for ``stage`` (``extractor`` / ``catalog_generation`` / ``catalog_link``)."""
        from .llm import AzureOpenAILLM, ClaudeLLM

        name, cfg = self.llm_config(stage)
        if cfg is None:
            where = f"the 'semantic' section of {self.config_file}" if self.config_file else "the semantic config"
            own = self.LLM_STAGES.get(stage, ())
            raise ValueError(
                f"This needs an LLM (catalog generation / extractor type 'llm'), but {where} names none "
                f"('default_llm'{f' or {own[0]!r}' if own else ''}). Declare one in the file's top-level \"llms\" "
                f"section and name it in \"semantic\", e.g.\n"
                f'    "llms": {{"claude": {{"model": "claude-opus-5"}}}},\n'
                f'    "semantic": {{"default_llm": "claude", ...}}\n'
                f"(API key from the ANTHROPIC_API_KEY environment variable, or an \"authentication\" "
                f"block like the connectors'), and `pip install -e \".[llm]\"`. "
                f"See examples/semantic/duckduck.online.json."
            )
        logger.info(
            "llm for %s: %s (%s %s)", stage or "default", name, cfg.provider,
            cfg.deployment if cfg.provider == "azure_openai" else cfg.model,
        )
        creds = duck.resolve_credentials(cfg.authentication, "semantic.llm") if cfg.authentication else {}
        headers = _resolve_headers(cfg.headers, creds) or None

        def setting(name: str) -> Optional[str]:
            return getattr(cfg, name) or creds.get(name)

        api_key = creds.get("api_key")
        if cfg.provider == "azure_openai":
            if not setting("deployment"):
                raise ValueError(
                    "llm.provider 'azure_openai' needs 'deployment' (the name you gave the model "
                    "deployment in Azure), in the LLM's declaration or its authentication secret."
                )
            return AzureOpenAILLM(
                deployment=setting("deployment"), endpoint=setting("endpoint"), api_version=setting("api_version"),
                api_key=api_key, tenant_id=setting("tenant_id"), max_tokens=cfg.max_tokens,
                reasoning_effort=cfg.effort, default_headers=headers,
            )
        if cfg.provider == "foundry":
            return ClaudeLLM.on_foundry(
                model=cfg.model, resource=setting("resource"), base_url=setting("endpoint") or setting("base_url"),
                api_key=api_key, tenant_id=setting("tenant_id"), default_headers=headers,
                fallbacks=cfg.fallbacks if "fallbacks" in cfg.model_fields_set else None,
                max_tokens=cfg.max_tokens, effort=cfg.effort,
            )
        gateway_auth = any(h.lower() in ("x-api-key", "authorization") for h in cfg.headers)
        if cfg.authentication and not api_key and not gateway_auth:
            raise ValueError(
                f"The LLM's authentication block resolved to keys {sorted(creds)} but no 'api_key'. "
                f"If the secret stores it under another name, map it: \"api_key\": \"$secret.<its key>\"."
            )
        return ClaudeLLM(
            model=cfg.model, api_key=api_key, max_tokens=cfg.max_tokens,
            effort=cfg.effort, fallbacks=cfg.fallbacks, base_url=setting("base_url"), default_headers=headers,
        )

    def build_extractor(self, catalog, duck: Any):
        from .llm_extraction import DEFAULT_EXTRACTION_PROMPT, LLMExtractor

        cfg = self.extractor
        if cfg.type == "rules":
            return None  # SemanticSearch's default
        prompt = self.prompt(cfg.system_prompt, cfg.system_prompt_file, DEFAULT_EXTRACTION_PROMPT)
        return LLMExtractor(catalog, self.build_llm(duck, "extractor"), system_prompt=prompt, on_error=cfg.on_error)

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
        )


def _resolve_headers(headers: Dict[str, str], creds: Dict[str, Any]) -> Dict[str, str]:
    """``"$secret.<key>"`` header values come from the resolved authentication block."""
    resolved = {}
    for name, value in headers.items():
        if isinstance(value, str) and value.startswith("$secret."):
            key = value[len("$secret."):]
            if key not in creds:
                raise ValueError(
                    f"llm.headers[{name!r}] refers to {value!r}, but the llm authentication block "
                    f"resolved to keys {sorted(creds)}."
                )
            value = creds[key]
        resolved[name] = str(value)
    return resolved

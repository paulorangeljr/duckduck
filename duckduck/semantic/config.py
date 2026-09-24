"""
Configuration for ``duckduck.semantic`` — the ``"semantic"`` section of
the same ``duckduck.json`` that ``auto_register()`` reads (or its own
file). Credentials use the exact ``authentication`` block shapes the
connectors use (``local`` / ``aws`` / ``azure``, ``$secret.<key>``), so a
Jev or LLM key lives in the same secret store as everything else.

::

    "semantic": {
      "catalog_path": "semantic_catalog.yaml",
      "decision_engine": {
        "type": "jev",
        "model": "typesafe-ai/jev",
        "authentication": {"type": "aws", "secret_id": "prod/jev", "api_key": "$secret.key"}
      },
      "llm": {
        "model": "claude-opus-5",
        "authentication": {"type": "azure", "vault_url": "https://kv.vault.azure.net/", "secret_id": "anthropic"}
      },
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
import os
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .generation import TableSpec
from .intent import Thresholds


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
    provider: Literal["anthropic"] = "anthropic"
    model: str = "claude-opus-5"
    max_tokens: int = 16000
    effort: Optional[Literal["low", "medium", "high", "xhigh", "max"]] = None
    fallbacks: Optional[str] = "default"
    base_url: Optional[str] = None
    #: Omitted → the SDK's own credential resolution (ANTHROPIC_API_KEY, ...).
    authentication: Optional[Dict[str, Any]] = None


class ExtractorConfig(_Strict):
    type: Literal["rules", "llm"] = "rules"
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


class SemanticConfig(_Strict):
    catalog_path: str = "semantic_catalog.yaml"
    decision_engine: DecisionEngineConfig = Field(default_factory=DecisionEngineConfig)
    llm: Optional[LLMConfig] = None
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
        if section not in data:
            raise ValueError(f"config file '{path}' has no '{section}' section")
        return cls.model_validate({
            **data[section],
            "base_dir": os.path.dirname(os.path.abspath(path)),
            "config_file": os.path.abspath(path),
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

    def build_llm(self, duck: Any):
        from .llm import ClaudeLLM

        if self.llm is None:
            where = f"the 'semantic' section of {self.config_file}" if self.config_file else "the semantic config"
            raise ValueError(
                f"This needs an LLM (catalog generation / extractor type 'llm'), but {where} has no "
                f"'llm' block. Add one, e.g.\n"
                f'    "llm": {{"model": "claude-opus-5"}}\n'
                f"(API key from the ANTHROPIC_API_KEY environment variable, or an \"authentication\" "
                f"block like the connectors'), and `pip install -e \".[llm]\"`. "
                f"See examples/semantic/duckduck.online.json."
            )
        cfg = self.llm
        creds = duck.resolve_credentials(cfg.authentication, "semantic.llm") if cfg.authentication else {}
        return ClaudeLLM(
            model=cfg.model, api_key=creds.get("api_key"), max_tokens=cfg.max_tokens,
            effort=cfg.effort, fallbacks=cfg.fallbacks, base_url=cfg.base_url,
        )

    def build_extractor(self, catalog, duck: Any):
        from .llm_extraction import DEFAULT_EXTRACTION_PROMPT, LLMExtractor

        cfg = self.extractor
        if cfg.type == "rules":
            return None  # SemanticSearch's default
        prompt = self.prompt(cfg.system_prompt, cfg.system_prompt_file, DEFAULT_EXTRACTION_PROMPT)
        return LLMExtractor(catalog, self.build_llm(duck), system_prompt=prompt, on_error=cfg.on_error)

    def build_generator(self, duck: Any):
        from .generation import DEFAULT_LINK_PROMPT, DEFAULT_SOURCE_PROMPT, CatalogGenerator

        cfg = self.catalog_generation
        return CatalogGenerator(
            self.build_llm(duck), duck,
            source_prompt=self.prompt(cfg.source_prompt, cfg.source_prompt_file, DEFAULT_SOURCE_PROMPT),
            link_prompt=self.prompt(cfg.link_prompt, cfg.link_prompt_file, DEFAULT_LINK_PROMPT),
            sample_rows=cfg.sample_rows,
            discover=cfg.discover,
            include=cfg.include,
            exclude=cfg.exclude,
            max_tables=cfg.max_tables,
        )

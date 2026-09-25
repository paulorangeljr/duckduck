"""
duckduck.semantic — natural-language search planned over a semantic
catalog and executed on the DuckAPI virtualization layer.

Requires ``pip install "duckduck[semantic]"`` (pydantic, PyYAML).

    from duckduck import DuckAPI
    from duckduck.semantic import SemanticSearch

    duck = DuckAPI()
    duck.auto_register()
    search = SemanticSearch("catalog.yaml", duck)
    result = search.search("Which users accessed github in the last 24hrs?")
    result.sql, result.results
"""

from .catalog import Catalog
from .commands import ask, calibrate, connect, generate_catalog, jev_check
from .config import SemanticConfig
from .decisions import (
    Ask,
    ask_all,
    BinaryDecision,
    Classification,
    DecisionEngine,
    DecisionEngineError,
    DecisionState,
    JEVAdapter,
    JEVBackend,
    LexicalDecisionEngine,
)
from .engine import SearchResult, SemanticSearch
from .evaluation import CalibrationReport, calibrate_thresholds, evaluate, load_dataset
from .extraction import RuleBasedExtractor
from .generation import CatalogGenerator, GenerationResult, TableSpec
from .graph import RelationshipGraph
from .intent import ClarificationNeeded, DecisionRecord, SemanticIntent, Thresholds
from .interpreter import SemanticInterpreter
from .jev import JevAPIError, JevClient
from .llm import AzureOpenAILLM, ClaudeLLM, LLMClient, LLMError, OpenRouterLLM
from .llm_extraction import LLMExtractor
from .plan import Filter, Join, LogicalQueryPlan, TimeRangeFilter
from .planner import QueryPlanner
from .retrieval import LexicalRetriever
from .validator import PlanValidationError, QueryValidator

__all__ = [
    "Ask",
    "BinaryDecision",
    "Catalog",
    "CatalogGenerator",
    "ClarificationNeeded",
    "Classification",
    "AzureOpenAILLM",
    "ClaudeLLM",
    "DecisionEngine",
    "DecisionEngineError",
    "DecisionRecord",
    "DecisionState",
    "Filter",
    "GenerationResult",
    "JEVAdapter",
    "JEVBackend",
    "JevAPIError",
    "JevClient",
    "Join",
    "LLMClient",
    "LLMError",
    "OpenRouterLLM",
    "LLMExtractor",
    "LexicalDecisionEngine",
    "LexicalRetriever",
    "LogicalQueryPlan",
    "PlanValidationError",
    "QueryPlanner",
    "QueryValidator",
    "RelationshipGraph",
    "RuleBasedExtractor",
    "SearchResult",
    "SemanticConfig",
    "SemanticIntent",
    "SemanticInterpreter",
    "SemanticSearch",
    "TableSpec",
    "Thresholds",
    "TimeRangeFilter",
    "ask",
    "ask_all",
    "calibrate",
    "calibrate_thresholds",
    "CalibrationReport",
    "connect",
    "evaluate",
    "generate_catalog",
    "jev_check",
    "load_dataset",
]

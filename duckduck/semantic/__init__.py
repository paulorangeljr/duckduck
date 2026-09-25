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
from .commands import (ask, calibrate, connect, feedback_export, feedback_report, feedback_suggest,
                       feedback_to_eval, generate_catalog, jev_check, serve)
from .config import SemanticConfig
from .feedback import FeedbackStore
from .memory import CaseMemory
from .suggest import CatalogSuggester
from .takeover import TakenOver
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
from .engine import Conversation, SearchResult, SemanticSearch
from .evaluation import CalibrationReport, calibrate_thresholds, evaluate, load_dataset
from .extraction import RuleBasedExtractor
from .generation import CatalogGenerator, GenerationResult, TableSpec
from .graph import RelationshipGraph
from .intent import Clarification, ClarificationNeeded, ClarificationOption, DecisionRecord, SemanticIntent, Thresholds
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
    "CaseMemory",
    "CatalogSuggester",
    "FeedbackStore",
    "feedback_export",
    "feedback_report",
    "feedback_suggest",
    "feedback_to_eval",
    "serve",
    "TakenOver",
    "BinaryDecision",
    "Catalog",
    "Clarification",
    "ClarificationOption",
    "Conversation",
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

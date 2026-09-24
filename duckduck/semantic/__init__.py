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
from .decisions import (
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
from .evaluation import evaluate, load_dataset
from .extraction import RuleBasedExtractor
from .graph import RelationshipGraph
from .intent import ClarificationNeeded, DecisionRecord, SemanticIntent, Thresholds
from .interpreter import SemanticInterpreter
from .plan import Filter, Join, LogicalQueryPlan, TimeRangeFilter
from .planner import QueryPlanner
from .retrieval import LexicalRetriever
from .validator import PlanValidationError, QueryValidator

__all__ = [
    "BinaryDecision",
    "Catalog",
    "Classification",
    "ClarificationNeeded",
    "DecisionEngine",
    "DecisionEngineError",
    "DecisionRecord",
    "DecisionState",
    "Filter",
    "JEVAdapter",
    "JEVBackend",
    "Join",
    "LexicalDecisionEngine",
    "LexicalRetriever",
    "LogicalQueryPlan",
    "PlanValidationError",
    "QueryPlanner",
    "QueryValidator",
    "RelationshipGraph",
    "RuleBasedExtractor",
    "SearchResult",
    "SemanticIntent",
    "SemanticInterpreter",
    "SemanticSearch",
    "Thresholds",
    "TimeRangeFilter",
    "evaluate",
    "load_dataset",
]

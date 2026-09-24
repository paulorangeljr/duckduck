"""
The semantic layer's top-level operations as plain Python functions.

``python -m duckduck.semantic <command>`` is a thin wrapper over these —
the CLI and a script/notebook run exactly the same code:

==================================================  ==============================================
CLI                                                 Python
==================================================  ==============================================
``python -m duckduck.semantic ask "..."``           ``print(ask("...").report())``
``... ask "..." --plan-only``                       ``ask("...", execute=False)``
``... ask "..." --json``                            ``ask("...").to_dict()``
``python -m duckduck.semantic generate-catalog``    ``generate_catalog()``
``... generate-catalog --out x.yaml``               ``generate_catalog(out="x.yaml")``
``python -m duckduck.semantic jev-check``           ``jev_check()``
``--config path``                                   ``config_path="path"``
``-v`` / ``-v debug``                               ``verbose="info"`` / ``verbose="debug"``
==================================================  ==============================================

Every function builds a ``DuckAPI`` from the config's ``services`` (via
``auto_register(on_error="warn")``) unless you pass one in ``duck=`` —
in a notebook, build it once and reuse it::

    duck = connect()                  # or your own DuckAPI
    ask("Which users accessed github in the last 24hrs?", duck=duck)
"""

from typing import Any, Optional

from .engine import SearchResult, SemanticSearch
from .generation import GenerationResult


def connect(config_path: Optional[str] = None, verbose: Any = None, on_error: str = "warn"):
    """A ``DuckAPI`` with every service from the config registered (what the CLI starts from)."""
    from duckduck import DuckAPI

    duck = DuckAPI(verbose=verbose)
    duck.auto_register(config_path=config_path, on_error=on_error)
    return duck


def ask(
    question: str,
    config_path: Optional[str] = None,
    duck: Any = None,
    verbose: Any = None,
    execute: bool = True,
) -> SearchResult:
    """
    Answers ``question`` with the config's catalog, decision engine and
    extractor. ``execute=False`` plans without fetching anything.
    ``result.report()`` is the CLI's printed output; ``result.to_dict()``
    its ``--json``.
    """
    duck = duck if duck is not None else connect(config_path, verbose)
    search = SemanticSearch.from_config(duck, config_path)
    return search.search(question, execute=execute)


def generate_catalog(
    config_path: Optional[str] = None,
    out: Optional[str] = None,
    duck: Any = None,
    verbose: Any = None,
    write: bool = True,
) -> GenerationResult:
    """
    Drafts the semantic catalog with the configured LLM from the tables
    in ``semantic.catalog_generation`` (or every registered table callable
    without structural args). Writes the YAML to ``out`` (default:
    ``catalog_generation.output_path``) unless ``write=False``; the
    result's ``path`` says where, ``warnings`` what was dropped.
    """
    from .config import SemanticConfig

    duck = duck if duck is not None else connect(config_path, verbose)
    cfg = SemanticConfig.load(duck, config_path)
    result = cfg.build_generator(duck).generate(cfg.catalog_generation.tables)
    if write:
        result.path = out or cfg.path(cfg.catalog_generation.output_path)
        result.write(result.path)
    return result


def jev_check(config_path: Optional[str] = None, verbose: Any = None):
    """
    One real Jev call (key, network access, response parsing) using the
    config's ``semantic.decision_engine``. Returns the ``Classification``;
    raises if the engine isn't Jev or the call fails.
    """
    from duckduck import DuckAPI

    from .config import SemanticConfig
    from .decisions import DecisionState

    duck = DuckAPI(verbose=verbose)  # no connectors needed, only credential resolution
    cfg = SemanticConfig.load(duck, config_path)
    if cfg.decision_engine.type != "jev":
        raise ValueError("semantic.decision_engine.type isn't 'jev' in the config")
    return cfg.build_engine(duck).classify(
        DecisionState(query="Which users accessed github in the last 24hrs?"),
        "What entity is the user asking for?",
        {"user": "A person or account", "host": "A machine", "domain": "A website"},
    )

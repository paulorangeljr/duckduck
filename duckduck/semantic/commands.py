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

import logging
import os
from typing import Any, List, Optional, Union

from .engine import SearchResult, SemanticSearch
from .generation import GenerationResult


logger = logging.getLogger("duckduck.semantic.generation")

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
    force: Union[bool, str, List[str], None] = False,
) -> GenerationResult:
    """
    Brings the semantic catalog up to date with the configured LLM. It
    reads and updates ``catalog_generation.output_path`` — by default
    ``catalog_path``, the catalog ``ask`` reads — drafting only what's
    missing, expired (``max_age``) or forced; hand-written sources and
    your ``notes`` are kept. ``force=True`` redrafts every generated
    source; a name / list of names or fnmatch patterns redrafts those
    (hand-written ones included). ``out`` overrides the file;
    ``write=False`` keeps the result in memory. Nothing to redraft → no
    LLM call and the file is left untouched.
    """
    from .config import SemanticConfig

    duck = duck if duck is not None else connect(config_path, verbose)
    cfg = SemanticConfig.load(duck, config_path)
    return refresh_catalog(cfg, duck, out=out, write=write, force=force)


def refresh_catalog(cfg: Any, duck: Any, out: Optional[str] = None, write: bool = True,
                    force: Union[bool, str, List[str], None] = False) -> GenerationResult:
    """``generate_catalog`` for an already-loaded ``SemanticConfig``."""
    from .catalog import Catalog

    target = cfg.catalog_target(out)
    existing = None
    if os.path.isfile(target):
        try:
            existing = Catalog.load(target)
        except Exception as exc:
            raise ValueError(
                f"{target} isn't a valid catalog, so it can't be updated (fix it, or move it away to draft "
                f"a new one): {exc}"
            ) from exc
    logger.info("catalog: %s %s", "updating" if existing else "creating", target)
    result = cfg.build_generator(duck).generate(cfg.catalog_generation.tables, existing=existing, force=force)
    if write and result.changed:
        result.write(target)
        logger.info("catalog: wrote %s", target)
    elif write:
        logger.info("catalog: %s left untouched", target)
    return result


def jev_check(config_path: Optional[str] = None, verbose: Any = None):
    """
    One real decision call (key, network access, response parsing) through
    the config's ``semantic.decision_engine`` — whichever ``ai_providers``
    entry it names: Jev on TypeSafe's API, Jev on OpenRouter, or a chat
    model. Returns the ``Classification``; raises if no AI decision engine
    is configured or the call fails.
    """
    from duckduck import DuckAPI

    from .config import SemanticConfig
    from .decisions import DecisionState

    duck = DuckAPI(verbose=verbose)  # no connectors needed, only credential resolution
    cfg = SemanticConfig.load(duck, config_path)
    if cfg.decision_engine.ai_provider is None:
        raise ValueError(
            "semantic.decision_engine names no ai_provider (it's the offline lexical engine) — nothing to check"
        )
    return cfg.build_engine(duck).classify(
        DecisionState(query="Which users accessed github in the last 24hrs?"),
        "What entity is the user asking for?",
        {"user": "A person or account", "host": "A machine", "domain": "A website"},
    )

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

def _apply_verbose(verbose: Any) -> None:
    """Same values as ``DuckAPI(verbose=...)`` — also when the caller passes its own ``duck``."""
    if verbose is not None:
        from duckduck.logs import set_verbose

        set_verbose(verbose)


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
    _apply_verbose(verbose)
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
    only: Union[str, List[str], None] = None,
) -> GenerationResult:
    """
    Brings the semantic catalog up to date with the configured LLM. It
    reads and updates ``catalog_generation.output_path`` — by default
    ``catalog_path``, the catalog ``ask`` reads — drafting only what's
    missing, expired (``max_age``) or forced; hand-written sources and
    your ``notes`` are kept. Nothing to redraft → no LLM call and the file
    is left untouched.

    Selectors — a name or list of fnmatch patterns: a table (catalog
    source name, registered table, ``database.table`` args, any single
    arg), a whole service (``"glue"``), or service + table
    (``"glue:security.proxy_logs"``, ``"adx:Proxy*"``).

    - ``force=True`` redrafts every generated source; ``force=<selectors>``
      redrafts exactly those (hand-written ones included) and nothing else.
    - ``only=<selectors>`` limits the run to those tables (new/expired
      among them, or what ``force`` picks among them).

    ``out`` overrides the file; ``write=False`` keeps the result in memory.
    ``verbose`` takes the same values as ``DuckAPI(verbose=...)``
    (``True``/``"info"``/``"debug"``), even with your own ``duck``.
    """
    from .config import SemanticConfig

    _apply_verbose(verbose)
    duck = duck if duck is not None else connect(config_path, verbose)
    cfg = SemanticConfig.load(duck, config_path)
    return refresh_catalog(cfg, duck, out=out, write=write, force=force, only=only)


def refresh_catalog(cfg: Any, duck: Any, out: Optional[str] = None, write: bool = True,
                    force: Union[bool, str, List[str], None] = False,
                    only: Union[str, List[str], None] = None) -> GenerationResult:
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
    result = cfg.build_generator(duck).generate(cfg.catalog_generation.tables, existing=existing, force=force, only=only)
    if write and result.changed:
        result.write(target)
        logger.info("catalog: wrote %s", target)
    elif write:
        logger.info("catalog: %s left untouched", target)
    return result


def calibrate(
    dataset: str,
    config_path: Optional[str] = None,
    duck: Any = None,
    verbose: Any = None,
    cost_wrong: float = 5.0,
    cost_ask: float = 1.0,
):
    """
    Suggests ``semantic.thresholds`` (entity / activity / source) for the
    configured decision engine from labeled questions — a JSON list like
    ``examples/semantic/evaluation.json`` (``question`` +
    ``expected_entity`` / ``expected_activity`` / ``expected_sources``).
    Plans every question (no data fetched) and picks, per kind, the
    threshold with the lowest cost: ``cost_wrong`` per wrong decision
    accepted, ``cost_ask`` per right one sent back to the user. Returns a
    ``CalibrationReport`` (``.summary()``, ``.thresholds``).
    """
    from .evaluation import calibrate_thresholds, load_dataset

    _apply_verbose(verbose)
    duck = duck if duck is not None else connect(config_path, verbose)
    search = SemanticSearch.from_config(duck, config_path)
    return calibrate_thresholds(search, load_dataset(dataset), cost_wrong=cost_wrong, cost_ask=cost_ask)


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

    _apply_verbose(verbose)
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


# ---------------------------------------------------------------------------
# Feedback: the web app, and what's learned from what users said
# ---------------------------------------------------------------------------


def _feedback_setup(config_path: Optional[str], duck: Any, verbose: Any):
    """(duck, config, store): the feedback file from ``semantic.feedback.path`` — enabled or not."""
    from .config import SemanticConfig
    from .feedback import FeedbackStore

    _apply_verbose(verbose)
    duck = duck if duck is not None else connect(config_path, verbose)
    cfg = SemanticConfig.load(duck, config_path)
    return duck, cfg, FeedbackStore(cfg.path(cfg.feedback.path))


def serve(
    config_path: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    duck: Any = None,
    verbose: Any = None,
    token: Optional[str] = None,
    run: bool = True,
    allow_sql: bool = False,
    allow_config_edit: bool = False,
):
    """
    The web app: ask questions, answer its clarifications, rate the answers,
    see the dashboard and review catalog suggestions. Records into
    ``semantic.feedback.path`` (whether or not ``feedback.enabled``: serving
    is collecting feedback) and uses the case memory when ``feedback.memory``.
    ``token`` (default ``DUCKDUCK_SERVER_TOKEN``) is required by every API
    call when set. ``run=False`` returns the FastAPI app instead of serving it.

    ``allow_sql``: the SQL tab — ``duck.sql`` on the registered tables, read
    queries only, on a connection with no file or network access.
    ``allow_config_edit``: the Config tab may save ``duckduck.json`` (secrets
    stay masked in the page; a ``.bak`` is kept) and reconnect everything.
    Reading the config (masked) and its options reference is always there.
    """
    from .admin import SQLConsole
    from .server import create_app, run as run_app

    duck, cfg, store = _feedback_setup(config_path, duck, verbose)

    def factory_for(d: Any, c: Any):
        memory = {"max_cases": c.feedback.max_cases, "min_similarity": c.feedback.min_similarity} \
            if c.feedback.memory else None
        return lambda: SemanticSearch.from_config(d, config_path, feedback=store, memory=memory)

    def rebuild():
        from .config import SemanticConfig

        d = connect(config_path, verbose)
        return factory_for(d, SemanticConfig.load(d, config_path)), (SQLConsole(d) if allow_sql else None)

    app = create_app(
        factory_for(duck, cfg), store, catalog_path=cfg.path(cfg.catalog_path),
        learned_shapes_path=cfg.path(cfg.feedback.learned_answer_shapes), min_support=cfg.feedback.min_support,
        token=token if token is not None else os.environ.get("DUCKDUCK_SERVER_TOKEN") or None,
        console=SQLConsole(duck) if allow_sql else None, config_path=cfg.config_file,
        allow_config_edit=allow_config_edit, rebuild=rebuild,
    )
    if not run:
        return app
    print(f"duckduck: http://{host}:{port}  (feedback in {store.path}"
          f"{'; SQL tab on' if allow_sql else ''}{'; config editing on' if allow_config_edit else ''})")
    run_app(app, host=host, port=port)
    return app


class FeedbackReport:
    """``feedback_report()``'s result: ``stats`` (the dashboard numbers) and ``summary()``."""

    def __init__(self, stats: dict, path: str):
        self.stats, self.path = stats, path

    def summary(self) -> str:
        o = self.stats["overall"]
        rate = lambda x: "—" if x is None else f"{x:.0%}"  # noqa: E731
        lines = [
            f"feedback: {self.path}",
            f"  {o['searches']} searches in {o['conversations']} conversations · {o['rated']} rated · "
            f"answered {rate(o['answer_rate'])} of rated · asked back {rate(o['asked_back_rate'])}",
        ]
        for title, key in (("by kind of answer", "by_answer_shape"), ("by table", "by_source")):
            df = self.stats[key]
            if not df.empty:
                lines.append(f"  {title}:")
                lines += [f"    {r['key']:<20} {r['answered']}/{r['rated']} answered ({rate(r['answer_rate'])})"
                          for r in df.to_dict(orient="records")]
        df = self.stats["by_category"]
        if not df.empty:
            lines.append("  what went wrong:")
            lines += [f"    {r['count']:>4}  {r['label']}" for r in df.to_dict(orient="records")]
        return "\n".join(lines)


def feedback_report(config_path: Optional[str] = None, duck: Any = None, verbose: Any = None) -> FeedbackReport:
    """Answer rates overall, per kind of answer, per table, and what went wrong — from ``semantic.feedback.path``."""
    _, _, store = _feedback_setup(config_path, duck, verbose)
    return FeedbackReport(store.stats(), store.path)


class FeedbackEvaluation:
    """``feedback_to_eval()``'s result: the dataset written, its metrics, the suggested thresholds."""

    def __init__(self, path: str, dataset: list, report: Any, calibration: Any):
        self.path, self.dataset, self.report, self.calibration = path, dataset, report, calibration

    def summary(self) -> str:
        if not self.dataset:
            return "no rated questions yet — nothing to evaluate"
        metrics = ", ".join(f"{k.replace('_accuracy', '')} {v:.0%}" for k, v in self.report.metrics.items()
                            if k.endswith("accuracy") and v is not None)
        return (f"wrote {len(self.dataset)} rated questions to {self.path}\n"
                f"  now: {metrics or 'no labels to score'}\n{self.calibration.summary()}")


def feedback_to_eval(
    config_path: Optional[str] = None,
    out: Optional[str] = None,
    duck: Any = None,
    verbose: Any = None,
    cost_wrong: float = 5.0,
    cost_ask: float = 1.0,
) -> FeedbackEvaluation:
    """
    Rated questions → an evaluation set (``evaluation.json`` shape, written
    to ``out``, default ``feedback_evaluation.json`` next to the config),
    scored against the current catalog and engine (planning only), plus the
    thresholds ``calibrate`` suggests from it.
    """
    import json

    from .evaluation import calibrate_thresholds, evaluate

    duck, cfg, store = _feedback_setup(config_path, duck, verbose)
    dataset = store.to_evaluation()
    path = out or cfg.path("feedback_evaluation.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2, ensure_ascii=False)
    if not dataset:
        return FeedbackEvaluation(path, dataset, None, None)
    search = SemanticSearch.from_config(duck, config_path, feedback=None, memory=None)
    return FeedbackEvaluation(path, dataset, evaluate(search, dataset, execute=False),
                              calibrate_thresholds(search, dataset, cost_wrong=cost_wrong, cost_ask=cost_ask))


class SuggestionReport:
    """``feedback_suggest()``'s result: the open suggestions, and what was accepted/dismissed this call."""

    def __init__(self, suggestions: list, applied: List[str], dismissed: List[str]):
        self.suggestions, self.applied, self.dismissed = suggestions, applied, dismissed

    def summary(self) -> str:
        lines = [f"accepted: {a}" for a in self.applied] + [f"dismissed: {d}" for d in self.dismissed]
        if not self.suggestions:
            lines.append("no open suggestions")
        for s in self.suggestions:
            lines.append(f"[{s.id}] {s.kind} {s.target or ''}: {s.change!r} — {s.reason} "
                         f"({len(s.evidence)} question{'s' if len(s.evidence) != 1 else ''})")
        return "\n".join(lines)


def feedback_suggest(
    config_path: Optional[str] = None,
    accept: Optional[List[str]] = None,
    dismiss: Optional[List[str]] = None,
    duck: Any = None,
    verbose: Any = None,
) -> SuggestionReport:
    """
    Catalog suggestions from what users said (``duckduck.semantic.suggest``).
    ``accept`` / ``dismiss``: suggestion ids — accepted ones are written to
    the catalog (``.bak`` kept) or to ``feedback.learned_answer_shapes``.
    """
    from .suggest import CatalogSuggester, apply_suggestion

    duck, cfg, store = _feedback_setup(config_path, duck, verbose)
    search = SemanticSearch.from_config(duck, config_path, feedback=None, memory=None)
    suggester = CatalogSuggester(store, search.catalog, search.shapes, min_support=cfg.feedback.min_support)
    by_id = {s.id: s for s in suggester.suggest()}
    applied, dismissed = [], []
    for sid in accept or []:
        if sid not in by_id:
            raise ValueError(f"no open suggestion {sid!r}")
        what = apply_suggestion(by_id[sid], cfg.path(cfg.catalog_path), cfg.path(cfg.feedback.learned_answer_shapes))
        store.record_review(sid, "accepted", detail=what)
        applied.append(what)
    for sid in dismiss or []:
        if sid not in by_id:
            raise ValueError(f"no open suggestion {sid!r}")
        store.record_review(sid, "dismissed")
        dismissed.append(sid)
    if applied or dismissed:
        search = SemanticSearch.from_config(duck, config_path, feedback=None, memory=None)
        suggester = CatalogSuggester(store, search.catalog, search.shapes, min_support=cfg.feedback.min_support)
    return SuggestionReport(suggester.suggest(), applied, dismissed)


class FeedbackExport:
    """``feedback_export()``'s result: the Markdown brief, where it was written, and the gaps in it."""

    def __init__(self, markdown: str, path: Optional[str], gaps: list, counts: dict):
        self.markdown, self.path, self.gaps, self.counts = markdown, path, gaps, counts

    def summary(self) -> str:
        where = f" to {self.path}" if self.path else ""
        shown = self.counts["shown"]
        more = f" ({self.counts['gaps']} in all)" if self.counts["gaps"] > shown else ""
        return f"wrote {shown} still-failing question pattern(s){more}{where}"


def feedback_export(
    config_path: Optional[str] = None,
    out: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
    include_partial: bool = True,
    redact: bool = False,
    duck: Any = None,
    verbose: Any = None,
    write: bool = True,
) -> FeedbackExport:
    """
    The questions users still rate as not answered, as a Markdown brief for
    whoever changes the code (``duckduck.semantic.export``): grouped by
    pattern, with what the system did, every decision, the SQL, what users
    said and would have expected, related suggestions, and how to reproduce.
    ``since``: ``"2026-09-01"`` or an age like ``"7d"``. ``redact``: question
    templates instead of questions, no SQL. Written to ``out`` (default
    ``feedback_export.md`` next to the config) unless ``write=False``.
    """
    import datetime as dt

    from .export import build_export
    from .generation import parse_age
    from .suggest import CatalogSuggester

    duck, cfg, store = _feedback_setup(config_path, duck, verbose)
    start = None
    if since:
        try:
            start = dt.datetime.fromisoformat(since)
        except ValueError:
            start = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - parse_age(since)
    search = SemanticSearch.from_config(duck, config_path, feedback=None, memory=None)
    suggestions = CatalogSuggester(store, search.catalog, search.shapes,
                                   min_support=cfg.feedback.min_support).suggest()
    built = build_export(store, suggestions, since=start, limit=limit, include_partial=include_partial, redact=redact,
                         config_path=config_path)
    path = None
    if write:
        path = out or cfg.path("feedback_export.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(built["markdown"])
    return FeedbackExport(built["markdown"], path, built["gaps"], built["counts"])

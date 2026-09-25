"""
A small web app for asking questions and collecting feedback on the answers.

``python -m duckduck.semantic serve`` (or ``serve()`` / ``create_app()``)
runs it with FastAPI + uvicorn (``pip install "duckduck[server]"``). One
HTML page (``webpage.PAGE``) over a JSON API:

==========================================  ==============================================
``POST /api/preview {question}``            while typing: entity, answer kind, relevant tables by system
``POST /api/ask {question, user, only_sources, entity, blocked_joins, in_scope}``  starts a conversation → its first result
``POST /api/answer {conversation_id, reply}`` answers the open clarification → next result
``POST /api/feedback {search_id, verdict, categories, reason, expected, user}``
``GET  /api/searches``, ``/api/searches/{id}``  history / one search's decision trail
``GET  /api/stats``                          the dashboard numbers
``GET  /api/suggestions``                    catalog suggestions (``?all=1``: reviewed too)
``POST /api/suggestions/{id}/accept|dismiss``  apply (and reload) / set aside
``POST /api/evaluate``                       feedback → evaluation set → metrics + thresholds
``GET  /api/evaluation.json``                that evaluation set, to keep
``GET  /api/export.md``                      the still-failing questions, as a brief for a developer
``GET  /api/meta``                           categories, answer kinds, tables (+ icon kind), entities, values
``POST /api/sql {sql}`` · ``GET /api/tables``  the SQL console (``allow_sql``, on by default; read-only, no files/network)
``GET  /api/config``                         duckduck.json, secrets masked, + every option documented
``POST /api/config/validate {config}``       check an edited config without saving
``PUT  /api/config {config}``                save it (``allow_config_edit``; ``.bak`` kept) and reload
==========================================  ==============================================

**It has no user accounts.** It answers from your data with your
credentials, so it listens on 127.0.0.1 by default. Set a ``token``
(``DUCKDUCK_SERVER_TOKEN``) before exposing it to anyone else — every API
call then needs it (``X-Duckduck-Token`` header); the page asks for it once.
"""

import json
import logging
import os
import secrets
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("duckduck.semantic.server")

#: Rows sent to the page per table (the full result stays available through the Python API).
MAX_ROWS = 500
MAX_CONVERSATIONS = 500


def create_app(
    factory: Callable[[], Any],
    store: Any,
    catalog_path: Optional[str] = None,
    learned_shapes_path: Optional[str] = None,
    min_support: int = 2,
    token: Optional[str] = None,
    console: Any = None,
    config_path: Optional[str] = None,
    allow_config_edit: bool = False,
    rebuild: Optional[Callable[[], Any]] = None,
):
    """
    ``factory`` builds the ``SemanticSearch`` (called again after an accepted
    suggestion changes the catalog or the wording); it must use ``store``.
    ``catalog_path`` / ``learned_shapes_path``: where accepted suggestions
    are written (without them, suggestions can be listed but not accepted).
    ``console``: an ``admin.SQLConsole`` for the SQL tab (``None``: no SQL
    tab). ``config_path``: the ``duckduck.json`` the Config tab shows;
    saving it needs ``allow_config_edit``, and then ``rebuild()`` →
    ``(factory, console)`` reconnects everything from the saved file.
    """
    try:
        from fastapi import Body, FastAPI, HTTPException, Request
        from fastapi.responses import HTMLResponse, JSONResponse, Response
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError('the web app needs FastAPI and uvicorn: pip install "duckduck[server]"') from exc

    from .engine import _json_default
    from .feedback import CATEGORIES, VERDICTS
    from .suggest import CatalogSuggester, apply_suggestion
    from .webpage import PAGE

    app = FastAPI(title="duckduck semantic search", docs_url=None, redoc_url=None)
    state: Dict[str, Any] = {"search": factory(), "factory": factory, "console": console,
                             "conversations": OrderedDict()}
    lock = threading.RLock()  # conversations + reloads; searches themselves run concurrently

    def dump(payload: Any, status: int = 200) -> Response:
        # strict JSON: browsers reject NaN / Infinity (a NULL count from pandas arrives as NaN)
        return Response(json.dumps(_finite(payload), default=_json_default, allow_nan=False),
                        status_code=status, media_type="application/json")

    @app.middleware("http")
    async def check_token(request: Request, call_next):
        if token and request.url.path.startswith("/api/"):
            given = request.headers.get("x-duckduck-token") or ""
            if not secrets.compare_digest(given, token):
                return JSONResponse({"error": "missing or wrong token"}, status_code=401)
        return await call_next(request)

    def payload(conv: Any) -> Dict[str, Any]:
        data = conv.result.to_dict()
        data["results"], truncated = data["results"][:MAX_ROWS], len(data["results"]) > MAX_ROWS
        for sec in data.get("sections") or []:
            if sec.get("results"):
                truncated |= len(sec["results"]) > MAX_ROWS
                sec["results"] = sec["results"][:MAX_ROWS]
        return {
            "conversation_id": conv.id, "done": conv.done, "rounds": conv.rounds,
            "transcript": conv.transcript, "history": conv.history, "truncated": truncated,
            "result": data,
        }

    def conversation(conversation_id: str):
        with lock:
            conv = state["conversations"].get(conversation_id)
        if conv is None:
            raise HTTPException(404, "conversation not found (the server restarted?) — ask again")
        return conv

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return PAGE

    @app.get("/api/meta")
    def meta():
        search = state["search"]
        cat = search.catalog
        return dump({
            "features": {"sql": state["console"] is not None, "config": bool(config_path),
                         "config_edit": bool(config_path and allow_config_edit)},
            "source_icons": {n: search.source_icon(n) for n in cat.sources},
            "texts": {"ask_anyway": search.texts.t("reply.ask_anyway")},
            "verdicts": list(VERDICTS),
            "categories": CATEGORIES,
            "answer_shapes": {s: search.texts.t(f"answer_shape.{s}") if f"answer_shape.{s}" in search.texts.texts
                              else "a list of what matches" for s in search.shapes.descriptions},
            "sources": {n: s.description.strip() for n, s in cat.sources.items()},
            "entities": {n: e.description.strip() for n, e in cat.entities.items()},
            "activities": {n: a.description.strip() for n, a in cat.activities.items()},
            "values": {f"{s}.{f}": sorted(d.values) for s, src in cat.sources.items()
                       for f, d in src.fields.items() if d.values},
            "can_accept": bool(catalog_path and learned_shapes_path),
            "token_required": bool(token),
        })

    @app.post("/api/ask")
    def ask(body: Dict[str, Any] = Body(...)):
        question = str(body.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "empty question")
        only = body.get("only_sources")
        entity = body.get("entity")
        search = state["search"]
        if entity is not None and entity not in search.catalog.entities:
            raise HTTPException(400, f"unknown entity {entity!r}")
        try:
            pins = {"entity": entity} if entity else {}
            if body.get("in_scope"):  # "it is about the data — try anyway"
                pins["in_scope"] = True
            for pair in body.get("blocked_joins") or []:  # joins the user unticked: routed around, never used
                if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                        or not all(isinstance(r, str) and search.catalog.has_field(r) for r in pair)):
                    raise ValueError(f"blocked_joins: {pair!r} isn't a pair of source.field names")
                pins[f"join:{pair[0]}={pair[1]}"] = False
            conv = search.conversation(question, user=body.get("user") or None,
                                       only_sources=list(only) if only is not None else None,
                                       pinned=pins or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        with lock:
            convs = state["conversations"]
            convs[conv.id] = conv
            while len(convs) > MAX_CONVERSATIONS:
                convs.popitem(last=False)
        return dump(payload(conv))

    @app.post("/api/preview")
    def preview(body: Dict[str, Any] = Body(...)):
        question = str(body.get("question") or "").strip()
        if len(question) < 3:
            return dump({"question": question, "systems": [], "sources": [], "entity": None})
        try:
            return dump(state["search"].preview(question))
        except Exception as exc:  # a preview is a hint: never an error on screen while typing
            logger.info("preview failed: %s", exc)
            return dump({"question": question, "error": str(exc), "systems": [], "sources": [], "entity": None})

    @app.post("/api/answer")
    def answer(body: Dict[str, Any] = Body(...)):
        conv = conversation(str(body.get("conversation_id")))
        if conv.done:
            raise HTTPException(409, "nothing to answer: this conversation has no open question")
        conv.answer(str(body.get("reply") or ""))
        return dump(payload(conv))

    @app.post("/api/feedback")
    def feedback(body: Dict[str, Any] = Body(...)):
        try:
            feedback_id = store.record_feedback(
                str(body.get("search_id")), str(body.get("verdict")), body.get("categories") or [],
                str(body.get("reason") or ""), body.get("expected") or None, body.get("user") or None)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return dump({"id": feedback_id})

    @app.get("/api/searches")
    def searches(limit: int = 100):
        return dump(store.searches(limit=min(max(limit, 1), 1000)).to_dict(orient="records"))

    @app.get("/api/searches/{search_id}")
    def one_search(search_id: str):
        row = store.search(search_id)
        if row is None:
            raise HTTPException(404, "no such search")
        return dump(row)

    @app.get("/api/stats")
    def stats():
        s = store.stats()
        return dump({k: (v.to_dict(orient="records") if hasattr(v, "to_dict") else v) for k, v in s.items()})

    def suggester():
        search = state["search"]
        return CatalogSuggester(store, search.catalog, search.shapes, min_support=min_support)

    @app.get("/api/suggestions")
    def suggestions(all: int = 0):
        items = suggester().suggest(include_reviewed=bool(all))
        questions = {}
        for s in items:
            for sid in s.evidence:
                if sid not in questions:
                    row = store.search(sid)
                    questions[sid] = row["question"] if row else sid
        return dump([{**s.to_dict(), "questions": [questions[i] for i in s.evidence]} for s in items])

    def find(suggestion_id: str):
        match = next((s for s in suggester().suggest(include_reviewed=True) if s.id == suggestion_id), None)
        if match is None:
            raise HTTPException(404, "no such suggestion (already applied, or its feedback changed)")
        return match

    @app.post("/api/suggestions/{suggestion_id}/accept")
    def accept(suggestion_id: str, body: Dict[str, Any] = Body(default={})):
        if not (catalog_path and learned_shapes_path):
            raise HTTPException(409, "this server wasn't given a catalog path to write to")
        s = find(suggestion_id)
        with lock:
            try:
                what = apply_suggestion(s, catalog_path, learned_shapes_path)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            store.record_review(s.id, "accepted", body.get("user") or None, what)
            state["search"] = state["factory"]()  # the new catalog / wording, from now on
        logger.info("suggestion %s accepted: %s", s.id, what)
        return dump({"applied": what})

    @app.post("/api/suggestions/{suggestion_id}/dismiss")
    def dismiss(suggestion_id: str, body: Dict[str, Any] = Body(default={})):
        s = find(suggestion_id)
        store.record_review(s.id, "dismissed", body.get("user") or None, str(body.get("reason") or ""))
        return dump({"dismissed": s.id})

    @app.post("/api/evaluate")
    def evaluate_feedback():
        from .evaluation import calibrate_thresholds, evaluate

        dataset = store.to_evaluation()
        if not dataset:
            return dump({"size": 0, "note": "no rated questions yet"})
        search = state["factory"]()  # its own instance: calibration sets thresholds to 0 for a while
        report = evaluate(search, dataset, execute=False)
        calibration = calibrate_thresholds(search, dataset)
        return dump({
            "size": len(dataset),
            "metrics": report.metrics,
            "misses": [{"question": c.question, "status": c.status, "error": c.error}
                       for c in report.cases
                       if False in (c.source_ok, c.entity_ok, c.activity_ok, c.shape_ok) or c.error],
            "calibration": calibration.summary(),
            "thresholds": calibration.thresholds,
        })

    @app.get("/api/export.md")
    def export_md(redact: int = 0, since_days: int = 0):
        import datetime as dt

        from .export import build_export

        since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=since_days) \
            if since_days else None
        built = build_export(store, suggester().suggest(), since=since, redact=bool(redact))
        return Response(built["markdown"], media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="feedback_export.md"'})

    @app.get("/api/evaluation.json")
    def evaluation_set():
        return Response(json.dumps(store.to_evaluation(), indent=2, ensure_ascii=False),
                        media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="feedback_evaluation.json"'})

    # -- SQL console ---------------------------------------------------------------------------

    def the_console():
        if state["console"] is None:
            raise HTTPException(403, "the SQL tab is off — the server was started with --no-sql (serve(allow_sql=False))")
        return state["console"]

    @app.post("/api/sql")
    def run_sql(body: Dict[str, Any] = Body(...)):
        return dump(the_console().run(str(body.get("sql") or "")))

    @app.get("/api/tables")
    def tables():
        return dump(the_console().tables())

    # -- duckduck.json -----------------------------------------------------------------------

    from .admin import config_reference, mask, save_config, unmask, validate_config

    def read_config() -> Dict[str, Any]:
        if not config_path:
            raise HTTPException(404, "this server wasn't started from a config file")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise HTTPException(500, f"{config_path} isn't valid JSON: {exc}")

    @app.get("/api/config")
    def get_config():
        return dump({"path": config_path, "config": mask(read_config()), "editable": allow_config_edit,
                     "reference": config_reference()})

    @app.post("/api/config/validate")
    def check_config(body: Dict[str, Any] = Body(...)):
        try:
            data = unmask(body.get("config"), read_config())
        except ValueError as exc:
            return dump({"errors": [str(exc)], "warnings": []})
        return dump(validate_config(data, config_path or ""))

    @app.put("/api/config")
    def put_config(body: Dict[str, Any] = Body(...)):
        if not allow_config_edit:
            raise HTTPException(403, "editing is off — start the server with --edit-config "
                                     "(serve(allow_config_edit=True))")
        read_config()
        try:
            saved = save_config(config_path, body.get("config"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        saved["reloaded"] = False
        if rebuild is not None:
            with lock:
                try:
                    new_factory, new_console = rebuild()
                    state["search"], state["factory"] = new_factory(), new_factory
                    state["console"] = new_console
                    state["conversations"].clear()
                    saved["reloaded"] = True
                except Exception as exc:  # the file is saved (and .bak kept); say why it didn't take
                    saved["reload_error"] = f"{type(exc).__name__}: {exc}"
        logger.info("config saved to %s (reloaded: %s)", config_path, saved["reloaded"])
        return dump(saved)

    app.state.duckduck = state
    return app


def _finite(value: Any) -> Any:
    """NaN / ±Infinity → ``None``, anywhere in a JSON-bound structure (numpy floats included)."""
    import math

    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    if isinstance(value, float) or type(value).__name__.startswith("float"):
        try:
            return None if not math.isfinite(float(value)) else value
        except (TypeError, ValueError):
            return value
    return value


def run(app: Any, host: str = "127.0.0.1", port: int = 8765) -> None:  # pragma: no cover - blocks
    try:
        import uvicorn
    except ImportError as exc:
        raise ImportError('the web app needs FastAPI and uvicorn: pip install "duckduck[server]"') from exc
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("DUCKDUCK_SERVER_TOKEN"):
        logger.warning("serving on %s without a token: anyone who can reach it can query your data "
                       "(set DUCKDUCK_SERVER_TOKEN)", host)
    uvicorn.run(app, host=host, port=port)

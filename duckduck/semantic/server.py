"""
A small web app for asking questions and collecting feedback on the answers.

``python -m duckduck.semantic serve`` (or ``serve()`` / ``create_app()``)
runs it with FastAPI + uvicorn (``pip install "duckduck[server]"``). One
HTML page (``webpage.PAGE``) over a JSON API:

==========================================  ==============================================
``POST /api/ask {question, user}``          starts a conversation → its first result
``POST /api/answer {conversation_id, reply}`` answers the open clarification → next result
``POST /api/feedback {search_id, verdict, categories, reason, expected, user}``
``GET  /api/searches``, ``/api/searches/{id}``  history / one search's decision trail
``GET  /api/stats``                          the dashboard numbers
``GET  /api/suggestions``                    catalog suggestions (``?all=1``: reviewed too)
``POST /api/suggestions/{id}/accept|dismiss``  apply (and reload) / set aside
``POST /api/evaluate``                       feedback → evaluation set → metrics + thresholds
``GET  /api/evaluation.json``                that evaluation set, to keep
``GET  /api/meta``                           categories, answer kinds, tables, entities, values
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
):
    """
    ``factory`` builds the ``SemanticSearch`` (called again after an accepted
    suggestion changes the catalog or the wording); it must use ``store``.
    ``catalog_path`` / ``learned_shapes_path``: where accepted suggestions
    are written (without them, suggestions can be listed but not accepted).
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
    state: Dict[str, Any] = {"search": factory(), "conversations": OrderedDict()}
    lock = threading.RLock()  # conversations + reloads; searches themselves run concurrently

    def dump(payload: Any, status: int = 200) -> Response:
        return Response(json.dumps(payload, default=_json_default), status_code=status,
                        media_type="application/json")

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
        conv = state["search"].conversation(question, user=body.get("user") or None)
        with lock:
            convs = state["conversations"]
            convs[conv.id] = conv
            while len(convs) > MAX_CONVERSATIONS:
                convs.popitem(last=False)
        return dump(payload(conv))

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
            state["search"] = factory()  # the new catalog / wording, from now on
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
        search = factory()  # its own instance: calibration sets thresholds to 0 for a while
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

    @app.get("/api/evaluation.json")
    def evaluation_set():
        return Response(json.dumps(store.to_evaluation(), indent=2, ensure_ascii=False),
                        media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="feedback_evaluation.json"'})

    app.state.duckduck = state
    return app


def run(app: Any, host: str = "127.0.0.1", port: int = 8765) -> None:  # pragma: no cover - blocks
    try:
        import uvicorn
    except ImportError as exc:
        raise ImportError('the web app needs FastAPI and uvicorn: pip install "duckduck[server]"') from exc
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("DUCKDUCK_SERVER_TOKEN"):
        logger.warning("serving on %s without a token: anyone who can reach it can query your data "
                       "(set DUCKDUCK_SERVER_TOKEN)", host)
    uvicorn.run(app, host=host, port=port)

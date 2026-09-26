"""
A small web app for asking questions and collecting feedback on the answers.

``python -m duckduck.semantic serve`` (or ``serve()`` / ``create_app()``)
runs it with FastAPI + uvicorn (``pip install "duckduck[server]"``). One
HTML page (``webpage.PAGE``) over a JSON API:

==========================================  ==============================================
``POST /api/preview {question, reader}``    while typing: entity, answer kind, relevant tables by system
``POST /api/ask {question, user, reader, only_sources, entity, values_field, blocked_joins, in_scope}``  starts a conversation → its first result
``POST /api/answer {conversation_id, reply}`` answers the open clarification → next result
``POST /api/ask {..., background: true}``  the same, as a job → 202 ``{job_id}`` (also ``/api/answer``)
``GET  /api/jobs/{id}?since=n``              what it's doing: steps from n, what it has so far (decisions, SQL, rows read), the result
``POST /api/jobs/{id}/pause|resume|cancel``  stop at the next step / go on / give up (answers at once with what was done)
``POST /api/all {conversation_id, background}``  the latest answer with every row, not just its sample (a job with ``background``)
``POST /api/columns {conversation_id, columns}``  the latest answer again with these extra fields (``column_options``)
``POST /api/feedback {search_id, verdict, categories, reason, expected, user, feedback_id}``
``GET  /api/searches``, ``/api/searches/{id}``  history / one search's decision trail
``GET  /api/stats``                          the dashboard numbers
``GET  /api/suggestions``                    catalog suggestions (``?all=1``: reviewed too)
``POST /api/suggestions/{id}/accept|dismiss``  apply (and reload) / set aside
``POST /api/evaluate``                       feedback → evaluation set → metrics + thresholds
``GET  /api/evaluation.json``                that evaluation set, to keep
``GET  /api/export.md``                      the still-failing questions, as a brief for a developer
``GET  /api/meta``                           categories, answer kinds, tables (+ icon kind), entities, values
``POST /api/sql {sql}`` · ``GET /api/tables``  the SQL console (``allow_sql``, on by default; read-only, no files/network)
``POST /api/takeover {conversation_id, name, full, user}``  "take over from here": the answer's rows as a table (an unrated answer → answered)
``POST /api/takeover/proposal {conversation_id}``  what that would give: suggested name, rows, columns, capped
``GET  /api/config``                         duckduck.json, secrets masked, + every option documented
``POST /api/config/validate {config}``       check an edited config without saving
``PUT  /api/config {config}``                save it (``allow_config_edit``; ``.bak`` kept) and reload
``GET  /api/catalog``                        the semantic catalog: its tables, their age, the tables not in it yet
``POST /api/catalog/generate {force, only}``  draft it with the LLM, as a job (``allow_config_edit``) → 202
``GET  /api/catalog/jobs/{id}?since=n``      that job: running / done / failed, its log from line n, the result
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
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("duckduck.semantic.server")

#: Rows sent to the page per table (the full result stays available through the Python API).
MAX_ROWS = 500
MAX_CONVERSATIONS = 500
MAX_JOBS = 100


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
    catalog_runner: Optional[Callable[[Any, Any], Any]] = None,
    catalog_info: Optional[Callable[[], Dict[str, Any]]] = None,
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
    ``catalog_runner(force, only)`` → a ``GenerationResult``: the Config
    tab's *Semantic catalog* card generates with it (as a background job,
    ``catalog_jobs``) — also gated by ``allow_config_edit``, since it
    rewrites the catalog file and spends LLM calls; ``catalog_info()`` →
    the file, the LLM and the limits it shows.
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
        data["column_options"] = conv.search.column_options(conv.result)
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
                         "config_edit": bool(config_path and allow_config_edit),
                         "catalog_generation": bool(catalog_runner is not None and allow_config_edit)},
            "source_icons": {n: search.source_icon(n) for n in cat.sources},
            "unavailable_sources": search.unavailable(),
            "texts": {"ask_anyway": search.texts.t("reply.ask_anyway")},
            "readers": {"available": search.readers, "default": search.reader,
                        "llm_unavailable": None if "llm" in search.readers else (
                            search.llm_reader_error or "no LLM configured (semantic.default_llm or extractor.llm)")},
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

    # -- questions as jobs: live steps, pause, cancel ---------------------------------------------

    from ..progress import Cancelled, Progress, tracking

    ask_jobs: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def start_job(run: Callable[[], Any]) -> Any:
        progress = Progress()
        job = {"id": uuid.uuid4().hex[:16], "progress": progress, "conv": None, "error": None, "finished": False}

        def work() -> None:
            with tracking(progress):
                try:
                    job["conv"] = run()
                except Cancelled:
                    pass
                except HTTPException as exc:
                    job["error"] = {"status": exc.status_code, "message": str(exc.detail)}
                except ValueError as exc:
                    job["error"] = {"status": 400, "message": str(exc)}
                except Exception as exc:  # shown on the page, like a failed synchronous ask
                    logger.exception("question failed")
                    job["error"] = {"status": 500, "message": f"{type(exc).__name__}: {exc}"}
                finally:
                    job["finished"] = True

        with lock:
            ask_jobs[job["id"]] = job
            while len(ask_jobs) > MAX_JOBS:
                ask_jobs.popitem(last=False)
        threading.Thread(target=work, name=f"duckduck-ask-{job['id']}", daemon=True).start()
        return dump({"job_id": job["id"]}, status=202)

    def the_job(job_id: str) -> Dict[str, Any]:
        with lock:
            job = ask_jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found (the server restarted?) — ask again")
        return job

    def job_view(job: Dict[str, Any], since: int = 0) -> Dict[str, Any]:
        view = {"job_id": job["id"], **job["progress"].to_dict(since)}
        if job["progress"].cancelled:
            view["state"] = "cancelled"
        elif job["finished"]:
            view["state"] = "failed" if job["error"] else "done"
            if job["conv"] is not None:
                view["result"] = payload(job["conv"])
            view["error"] = job["error"]
        return view

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, since: int = 0):
        return dump(job_view(the_job(job_id), since))

    @app.post("/api/jobs/{job_id}/{action}")
    def job_action(job_id: str, action: str):
        job = the_job(job_id)
        if action not in ("pause", "resume", "cancel"):
            raise HTTPException(404, f"unknown action {action!r}: pause, resume or cancel")
        if not job["finished"]:
            getattr(job["progress"], action)()
        return dump(job_view(job))

    @app.post("/api/ask")
    def ask(body: Dict[str, Any] = Body(...)):
        question = str(body.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "empty question")
        search = state["search"]
        pins = checked_pins(body, search)
        if body.get("background"):
            return start_job(lambda: start_conversation(body, question, search, pins))
        return dump(payload(start_conversation(body, question, search, pins)))

    def checked_pins(body: Dict[str, Any], search: Any) -> Dict[str, Any]:
        """The choices sent with the question as pins — checked before anything runs (400 otherwise)."""
        entity = body.get("entity")
        if entity is not None and entity not in search.catalog.entities:
            raise HTTPException(400, f"unknown entity {entity!r}")
        try:
            pins = {"entity": entity} if entity else {}
            if body.get("in_scope"):  # "it is about the data — try anyway"
                pins["in_scope"] = True
            values_field = body.get("values_field")  # "about" a field: the one whose values the answer lists
            if values_field is not None:
                if not isinstance(values_field, str) or not search.catalog.has_field(values_field):
                    raise ValueError(f"values_field: {values_field!r} isn't a source.field name")
                pins["values_field"] = values_field
                pins[f"source:{values_field.split('.')[0]}"] = True
            for pair in body.get("blocked_joins") or []:  # joins the user unticked: routed around, never used
                if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                        or not all(isinstance(r, str) and search.catalog.has_field(r) for r in pair)):
                    raise ValueError(f"blocked_joins: {pair!r} isn't a pair of source.field names")
                pins[f"join:{pair[0]}={pair[1]}"] = False
            only = body.get("only_sources")
            search._check_scope(list(only) if only is not None else None)
            search.check_reader(body.get("reader") or None)
            if "sample" in body:
                from .engine import _check_sample

                _check_sample(body["sample"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return pins

    def start_conversation(body: Dict[str, Any], question: str, search: Any, pins: Dict[str, Any]) -> Any:
        only = body.get("only_sources")
        try:
            extra = {"sample": body["sample"]} if "sample" in body else {}
            conv = search.conversation(question, user=body.get("user") or None,
                                       only_sources=list(only) if only is not None else None,
                                       pinned=pins or None, reader=body.get("reader") or None, **extra)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        with lock:
            convs = state["conversations"]
            convs[conv.id] = conv
            while len(convs) > MAX_CONVERSATIONS:
                convs.popitem(last=False)
        return conv

    @app.post("/api/preview")
    def preview(body: Dict[str, Any] = Body(...)):
        question = str(body.get("question") or "").strip()
        if len(question) < 3:
            return dump({"question": question, "systems": [], "sources": [], "entity": None})
        try:
            return dump(state["search"].preview(question, reader=body.get("reader") or None))
        except Exception as exc:  # a preview is a hint: never an error on screen while typing
            logger.info("preview failed: %s", exc)
            return dump({"question": question, "error": str(exc), "systems": [], "sources": [], "entity": None})

    @app.post("/api/answer")
    def answer(body: Dict[str, Any] = Body(...)):
        conv = conversation(str(body.get("conversation_id")))
        if conv.done:
            raise HTTPException(409, "nothing to answer: this conversation has no open question")
        reply = str(body.get("reply") or "")
        if body.get("background"):
            def run() -> Any:
                conv.answer(reply)
                return conv

            return start_job(run)
        conv.answer(reply)
        return dump(payload(conv))

    @app.post("/api/columns")
    def columns(body: Dict[str, Any] = Body(...)):
        conv = conversation(str(body.get("conversation_id")))
        wanted = body.get("columns") or []
        if not isinstance(wanted, list):
            raise HTTPException(400, "columns: a list of source.field names")

        def run() -> Any:
            try:
                conv.with_columns([str(c) for c in wanted])
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return conv

        if body.get("background"):  # the rows aren't kept any more: read again, with its steps shown
            return start_job(run)
        return dump(payload(run()))

    @app.post("/api/all")
    def fetch_all(body: Dict[str, Any] = Body(...)):
        conv = conversation(str(body.get("conversation_id")))

        def run() -> Any:
            try:
                conv.fetch_all()
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return conv

        if body.get("background"):
            return start_job(run)
        return dump(payload(run()))

    @app.post("/api/feedback")
    def feedback(body: Dict[str, Any] = Body(...)):
        try:
            feedback_id = store.record_feedback(
                str(body.get("search_id")), str(body.get("verdict")), body.get("categories") or [],
                str(body.get("reason") or ""), body.get("expected") or None, body.get("user") or None,
                feedback_id=body.get("feedback_id") or None)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return dump({"id": feedback_id})

    @app.get("/api/searches")
    def searches(limit: int = 100, reader: str = ""):
        return dump(store.searches(limit=min(max(limit, 1), 1000), reader=reader or None).to_dict(orient="records"))

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
    def evaluate_feedback(body: Optional[Dict[str, Any]] = Body(None)):
        from .evaluation import calibrate_thresholds, compare_readers, evaluate

        dataset = store.to_evaluation()
        if not dataset:
            return dump({"size": 0, "note": "no rated questions yet"})
        search = state["factory"]()  # its own instance: calibration sets thresholds to 0 for a while
        readers = [r for r in ((body or {}).get("readers") or []) if r in search.readers]
        report = evaluate(search, dataset, execute=False)
        calibration = calibrate_thresholds(search, dataset)
        # the same rated questions through each mode chosen: which reads them best, asks back least, costs least
        compared = {r: rep.metrics for r, rep in compare_readers(search, dataset, readers).items()} if readers else {}
        return dump({
            "size": len(dataset),
            "readers": compared,
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

    @app.post("/api/takeover/proposal")
    def takeover_proposal(body: Dict[str, Any] = Body(...)):
        from .takeover import proposal

        the_console()
        conv = conversation(str(body.get("conversation_id")))
        try:
            return dump(proposal(state["search"], conv.result))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/takeover")
    def takeover(body: Dict[str, Any] = Body(...)):
        the_console()  # the rows are for the SQL tab: no console, nothing to query them with
        conv = conversation(str(body.get("conversation_id")))
        name = body.get("name")
        try:
            taken = state["search"].take_over(conv.result, name=str(name) if name else None,
                                              full=body.get("full", True) is not False,
                                              user=body.get("user") or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return dump(taken.to_dict())

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

    # -- the semantic catalog: generate it from the page ------------------------------------

    from .catalog_jobs import CatalogJobs

    def reload_after_generation(_result: Any) -> None:
        with lock:
            state["search"] = state["factory"]()  # the new catalog, from now on
            state["conversations"].clear()

    jobs = CatalogJobs(catalog_runner, on_changed=reload_after_generation) if catalog_runner else None
    state["catalog_jobs"] = jobs

    def generation_off() -> Optional[str]:
        if catalog_runner is None:
            return "this server wasn't started from a config file (duckduck.json), so it can't generate a catalog"
        if not allow_config_edit:
            return ("generating rewrites the catalog file and calls the LLM once per table — start the server "
                    "with --edit-config (serve(allow_config_edit=True)) to do it from here")
        return None

    @app.get("/api/catalog")
    def catalog_status():
        search = state["search"]
        sources = []
        for name, src in search.catalog.sources.items():
            sources.append({
                "name": name, "table": src.table, "args": src.args, "relation": bool(src.relation),
                "fields": len(src.fields), "description": (src.description or "").split("\n")[0][:200],
                "generated_at": src.generated_at.isoformat(timespec="seconds") if src.generated_at else None,
                "generated_by": src.generated_by, "notes": bool(src.notes or any(f.notes for f in src.fields.values())),
                "service": search.duck.service_of.get(src.table) if search.duck is not None and src.table else None,
            })
        bound = {s["table"] for s in sources if s["table"] and not s["args"]}
        missing = []
        if search.duck is not None:
            try:
                listed = search.duck.list_tables(kind="table")
                missing = [n for n in listed["name"] if n not in bound and search.duck.service_of.get(n) != "taken over"]
            except Exception as exc:  # the card still shows the catalog
                logger.info("catalog status: couldn't list tables (%s)", exc)
        info = {}
        if catalog_info is not None:
            try:
                info = catalog_info()
            except Exception as exc:
                info = {"error": f"{type(exc).__name__}: {exc}"}
        latest = jobs.latest() if jobs else None
        return dump({"sources": sources, "not_in_catalog": missing, "unavailable": search.unavailable(),
                     "info": info, "off": generation_off(),
                     "job": latest.to_dict() if latest else None})

    @app.post("/api/catalog/generate")
    def catalog_generate(body: Optional[Dict[str, Any]] = Body(None)):
        off = generation_off()
        if off:
            raise HTTPException(403, off)
        body = body or {}
        force, only = body.get("force", False), body.get("only") or None
        if not (isinstance(force, bool) or (isinstance(force, list) and all(isinstance(f, str) for f in force))):
            raise HTTPException(400, "force: true/false, or a list of table names")
        if only is not None and not (isinstance(only, list) and all(isinstance(o, str) for o in only)):
            raise HTTPException(400, "only: a list of table names")
        try:
            job = jobs.start(force=force or False, only=only)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return dump(job.to_dict(), status=202)

    @app.get("/api/catalog/jobs/{job_id}")
    def catalog_job(job_id: str, since: int = 0):
        job = jobs.get(job_id) if jobs else None
        if job is None:
            raise HTTPException(404, f"no catalog job {job_id!r}")
        return dump(job.to_dict(since=since))

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

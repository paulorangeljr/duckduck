"""
Catalog generation from the web app (Config tab → *Semantic catalog*).

Generating drafts one LLM call per table, so it can take minutes: it runs
as a background **job**, one at a time, and the page polls it. A job keeps
the generation's own log lines (the same ``[n/total] table — profiling…``,
``→ 12 fields…`` lines ``generate-catalog -v`` prints) as they happen, and
at the end what was drafted, kept and deferred, and the warnings.

The work itself is ``commands.refresh_catalog`` — the same function the
CLI runs — so the file, the selectors (``force`` / ``only``) and the
merge rules (hand-written sources and notes kept) are the same.
"""

import logging
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Union

#: The loggers whose INFO lines make a generation's progress — set to INFO for the job, whatever the global level.
PROGRESS_LOGGERS = ("duckduck.semantic.generation", "duckduck.semantic.llm", "duckduck.semantic.apidocs")
MAX_LOG_LINES = 5000


class _JobLog(logging.Handler):
    """The ``duckduck`` lines one thread writes (a job's worker), at INFO."""

    def __init__(self, sink: List[str], thread: int):
        super().__init__(logging.INFO)
        self.sink, self.thread = sink, thread
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread:
            return
        try:
            line = self.format(record)
        except Exception:
            return
        if len(self.sink) >= MAX_LOG_LINES:
            del self.sink[: len(self.sink) - MAX_LOG_LINES + 1]
        self.sink.append(line)


class CatalogJob:
    def __init__(self, force: Union[bool, List[str]], only: Optional[List[str]]):
        self.id = uuid.uuid4().hex[:12]
        self.force, self.only = force, only
        self.state = "running"  # → done | failed
        self.started_at = datetime.now()
        self.finished_at: Optional[datetime] = None
        self.log: List[str] = []
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        #: After a run that changed the file: whether the app reloaded with the new catalog (or why not).
        self.reloaded: Optional[bool] = None
        self.reload_error: Optional[str] = None
        self._started = time.monotonic()

    def to_dict(self, since: int = 0) -> Dict[str, Any]:
        """``since``: log lines the page already has — only the new ones are sent."""
        since = max(0, min(int(since or 0), len(self.log)))
        return {
            "id": self.id, "state": self.state, "force": self.force, "only": self.only,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
            "elapsed_s": round((time.monotonic() - self._started) if self.finished_at is None
                               else (self.finished_at - self.started_at).total_seconds(), 1),
            "log": self.log[since:], "log_size": len(self.log),
            "result": self.result, "error": self.error,
            "reloaded": self.reloaded, "reload_error": self.reload_error,
        }


def result_summary(result: Any) -> Dict[str, Any]:
    """A ``GenerationResult`` as the page shows it."""
    return {
        "changed": bool(result.changed), "path": result.path, "drafted": dict(result.drafted),
        "kept": list(result.kept), "deferred": list(result.deferred), "warnings": list(result.warnings),
        "sources": len(result.catalog.sources) if result.catalog is not None else 0,
        "summary": result.summary(),
    }


class CatalogJobs:
    """
    ``run(force, only)`` → a ``GenerationResult`` (``commands.refresh_catalog``
    on the app's current DuckAPI and config); ``on_changed(result)`` runs
    after a job that wrote the file (the app reloads its search with it).
    """

    def __init__(self, run: Callable[[Any, Any], Any], on_changed: Optional[Callable[[Any], None]] = None,
                 keep: int = 20):
        self.run, self.on_changed, self.keep = run, on_changed, keep
        self.jobs: "OrderedDict[str, CatalogJob]" = OrderedDict()
        self._lock = threading.Lock()

    def running(self) -> Optional[CatalogJob]:
        return next((j for j in self.jobs.values() if j.state == "running"), None)

    def latest(self) -> Optional[CatalogJob]:
        return next(reversed(self.jobs.values()), None) if self.jobs else None

    def get(self, job_id: str) -> Optional[CatalogJob]:
        return self.jobs.get(job_id)

    def start(self, force: Union[bool, List[str]] = False, only: Optional[List[str]] = None,
              wait: bool = False) -> CatalogJob:
        """Starts a job — ``RuntimeError`` while another one runs. ``wait``: return once it ended (tests)."""
        with self._lock:
            if self.running() is not None:
                raise RuntimeError("a catalog generation is already running — wait for it to finish")
            job = CatalogJob(force, only)
            self.jobs[job.id] = job
            while len(self.jobs) > self.keep:
                self.jobs.popitem(last=False)
        worker = threading.Thread(target=self._work, args=(job,), name=f"catalog-job-{job.id}", daemon=True)
        worker.start()
        if wait:
            worker.join()
        return job

    def _work(self, job: CatalogJob) -> None:
        root = logging.getLogger("duckduck")
        handler = _JobLog(job.log, threading.get_ident())
        levels = {name: logging.getLogger(name).level for name in PROGRESS_LOGGERS}
        root.addHandler(handler)
        for name in PROGRESS_LOGGERS:  # a child's own level wins over its parent's: the lines reach the handler
            if logging.getLogger(name).getEffectiveLevel() > logging.INFO:
                logging.getLogger(name).setLevel(logging.INFO)
        result = None
        try:
            result = self.run(job.force, job.only)
            job.result = result_summary(result)
            job.state = "done"
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            job.state = "failed"
        finally:
            root.removeHandler(handler)
            for name, level in levels.items():
                logging.getLogger(name).setLevel(level)
            job.finished_at = datetime.now()
        if result is not None and result.changed and self.on_changed is not None:
            try:
                self.on_changed(result)
                job.reloaded = True
            except Exception as exc:  # the file is written; say why the app didn't take it
                job.reloaded, job.reload_error = False, f"{type(exc).__name__}: {exc}"

"""
Verbose/diagnostic output for duckduck, on Python's standard ``logging``.

Everything logs under the ``"duckduck"`` logger tree
(``duckduck.core``, ``duckduck.servicenow``, ...). ``DuckAPI(verbose=...)``
(or ``set_verbose``) attaches a compact console handler; without it
nothing is printed, and an application can route the same records
wherever it already sends its logs.

Levels:

- ``INFO``: per query — what each table was called with, which WHERE
  conditions / LIMIT went to the source and which didn't (and why), every
  HTTP request (method, URL, status, time, size), the SQL/KQL sent to
  databases/ADX/lakehouse, pagination progress (page, rows, elapsed, and
  time remaining when the API reports a total), and totals.
- ``DEBUG``: adds request bodies/params and bound query parameters.

Secrets never reach the log: ``Authorization``-like headers are never
logged, and body/query-string fields whose name looks like a credential
(password, secret, token, api key...) are masked.
"""

import json
import logging
import os
import re
import time
from typing import Any, Optional, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = "duckduck"

_SENSITIVE = re.compile(r"pass(word)?|secret|token|api[-_]?key|auth|credential|sig(nature)?$|code$", re.I)
_HANDLER_FLAG = "_duckduck_verbose_handler"


def get_logger(component: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{component}")


def _level(verbose: Union[bool, str, int, None]) -> Optional[int]:
    if verbose is None or verbose is False:
        return None
    if verbose is True:
        return logging.INFO
    if isinstance(verbose, int):
        return verbose
    name = str(verbose).strip().upper()
    if name in ("", "0", "FALSE", "OFF", "NONE"):
        return None
    if name in ("1", "TRUE", "ON", "VERBOSE"):
        return logging.INFO
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        raise ValueError(f"verbose must be True/False, 'info', 'debug' or a logging level (got {verbose!r})")
    return level


def set_verbose(verbose: Union[bool, str, int, None]) -> None:
    """
    ``True`` / ``"info"`` → INFO, ``"debug"`` → DEBUG, ``False`` → off.
    Attaches (once) a console handler to the ``duckduck`` logger. Process-
    wide, like ``logging`` itself: the last call wins.
    """
    logger = logging.getLogger(ROOT)
    ours = [h for h in logger.handlers if getattr(h, _HANDLER_FLAG, False)]
    level = _level(verbose)
    if level is None:
        for h in ours:
            logger.removeHandler(h)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
        return
    if not ours:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[duckduck %(asctime)s.%(msecs)03d] %(message)s", "%H:%M:%S"))
        setattr(handler, _HANDLER_FLAG, True)
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False  # don't print twice if the app also logs to root


def verbose_from_env() -> Optional[str]:
    """``DUCKDUCK_VERBOSE`` (e.g. ``info`` / ``debug``) — handy for scripts and notebooks."""
    return os.environ.get("DUCKDUCK_VERBOSE")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def human_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    minutes, seconds = divmod(int(round(s)), 60)
    return f"{minutes}m{seconds:02d}s" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


def short(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [(k, "***" if _SENSITIVE.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(query, safe="*^=%/:,")))


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: "***" if isinstance(k, str) and _SENSITIVE.search(k) else redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _body_text(body: Any) -> Optional[str]:
    if body is None:
        return None
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    try:
        return json.dumps(redact(json.loads(body)), default=str)
    except (ValueError, TypeError):
        pass
    if isinstance(body, str) and "=" in body:  # form-encoded
        pairs = parse_qsl(body, keep_blank_values=True)
        if pairs:
            return urlencode([(k, "***" if _SENSITIVE.search(k) else v) for k, v in pairs])
    return "<body>"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def log_http(component: str, response: Any, log_body: bool = True) -> None:
    """One INFO line per HTTP response (+ the request body at DEBUG). Never raises."""
    logger = get_logger(component)
    if not logger.isEnabledFor(logging.INFO):
        return
    try:
        _log_http(logger, response, log_body)
    except Exception as exc:  # diagnostics must never break the request they describe
        logger.debug("could not log HTTP response: %r", exc)


def _log_http(logger: logging.Logger, response: Any, log_body: bool) -> None:
    request = response.request
    try:
        size = human_bytes(len(response.content))
    except Exception:
        size = "?"
    elapsed = getattr(response, "elapsed", None)
    took = f"{elapsed.total_seconds():.2f}s" if elapsed is not None else "?"
    logger.info("%s %s → %s (%s, %s)", request.method, redact_url(request.url), response.status_code, took, size)
    if log_body and logger.isEnabledFor(logging.DEBUG):
        text = _body_text(getattr(request, "body", None))
        if text:
            logger.debug("    body: %s", text if len(text) <= 2000 else text[:2000] + "…")


def instrument_session(session: Any, component: str) -> None:
    """Logs every response of a ``requests.Session`` (via its response hook)."""
    def hook(response, *args, **kwargs):
        log_http(component, response)
        return response

    session.hooks.setdefault("response", []).append(hook)


# ---------------------------------------------------------------------------
# Pagination progress
# ---------------------------------------------------------------------------


class PageProgress:
    """
    Logs ``what: page 3/12 · 1,500 rows · 4.2s elapsed · ~12.6s left`` per
    page. The page total / row total are whatever the API reports (pass
    ``None`` when it doesn't) — time left is only estimated from those.
    """

    def __init__(self, component: str, what: str):
        self.logger = get_logger(component)
        self.what = what
        self.started = time.perf_counter()
        self.pages = 0
        self.rows = 0

    def page(self, rows: int, total_pages: Optional[int] = None, total_rows: Optional[int] = None) -> None:
        self.pages += 1
        self.rows += rows
        if not self.logger.isEnabledFor(logging.INFO):
            return
        try:
            self._log(total_pages, total_rows)
        except Exception as exc:  # e.g. a malformed total from the API — never fail the fetch over it
            self.logger.debug("could not log page progress: %r", exc)

    def _log(self, total_pages: Optional[int], total_rows: Optional[int]) -> None:
        elapsed = time.perf_counter() - self.started
        left = None
        if total_pages and self.pages < total_pages:
            left = elapsed / self.pages * (total_pages - self.pages)
        elif total_rows and 0 < self.rows < total_rows:
            left = elapsed / self.rows * (total_rows - self.rows)
        pages = f"{self.pages}/{total_pages}" if total_pages else str(self.pages)
        rows_text = f"{self.rows:,}" + (f"/{total_rows:,}" if total_rows else "")
        message = f"{self.what}: page {pages} · {rows_text} rows · {human_seconds(elapsed)} elapsed"
        if left is not None:
            message += f" · ~{human_seconds(left)} left"
        self.logger.info(message)

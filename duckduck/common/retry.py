"""
Retries for the HTTP connectors (ServiceNow, InsightVM, Axonius, SharePoint, REST Countries).

Each connector keeps a ``RetryPolicy`` as ``self.retry`` and sends every request through
``send(self.retry, component, call, default_timeout)``: a timeout, a dropped connection or an
answer in ``statuses`` (429 and 5xx by default) is tried again after a wait that doubles each
time (``backoff`` seconds first, at most ``max_wait``; a ``Retry-After`` header wins). The wait
goes through ``progress.wait``, so a paused or cancelled read doesn't sit it out.

Set per service in duckduck.json (``"retry": {"retries": 5, "timeout": 120}`` or ``false``) and
per pipeline for its run (``"retry": {...}`` or ``{"servicenow": {...}}``).
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from .logs import get_logger, redact_url

#: what a policy can say; a dict with only these keys is one policy, anything else is {service: policy}
KEYS = ("retries", "backoff", "max_wait", "timeout", "statuses")

DEFAULT_STATUSES: Tuple[int, ...] = (429, 500, 502, 503, 504)


@dataclass(frozen=True)
class RetryPolicy:
    #: how many times a failed request is tried again (0 = never)
    retries: int = 3
    #: seconds before the first retry; doubled each time
    backoff: float = 2.0
    #: the longest wait between two tries (a Retry-After header included)
    max_wait: float = 60.0
    #: seconds a request may take before it counts as failed (None: the connector's own, 30–60 s)
    timeout: Optional[float] = None
    #: HTTP statuses tried again
    statuses: Tuple[int, ...] = DEFAULT_STATUSES

    def with_(self, block: Any) -> "RetryPolicy":
        """This policy with ``block`` over it: ``False`` → no retries, ``True``/None → as is, a dict → those keys."""
        problem = policy_problem(block)
        if problem:
            raise ValueError(problem)
        if block is False:
            return dataclasses.replace(self, retries=0)
        if block is True or block is None:
            return self
        values = dict(block)
        if "statuses" in values:
            values["statuses"] = tuple(int(s) for s in values["statuses"])
        return dataclasses.replace(self, **values)

    def wait_before(self, attempt: int, retry_after: Optional[str] = None) -> float:
        """Seconds to wait before try ``attempt + 1`` (attempt counts from 0)."""
        seconds = self.backoff * (2 ** attempt)
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                pass
        return max(0.0, min(seconds, self.max_wait))


def _number(v: Any, minimum: float = 0) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= minimum


def policy_problem(block: Any) -> Optional[str]:
    """Why ``block`` isn't a retry setting (None when it is)."""
    if block is None or isinstance(block, bool):
        return None
    if not isinstance(block, dict):
        return 'retry is true, false or {"retries": 3, "backoff": 2, "max_wait": 60, "timeout": 60, "statuses": [429, 500, 502, 503, 504]}'
    unknown = sorted(set(block) - set(KEYS))
    if unknown:
        return f"retry: unknown key(s) {', '.join(unknown)} (it takes {', '.join(KEYS)})"
    if "retries" in block and not (isinstance(block["retries"], int) and _number(block["retries"])):
        return "retry.retries is a whole number ≥ 0 (how many times a failed request is tried again)"
    for key in ("backoff", "max_wait"):
        if key in block and not _number(block[key]):
            return f"retry.{key} is a number of seconds ≥ 0"
    if "timeout" in block and block["timeout"] is not None and not (_number(block["timeout"]) and block["timeout"] > 0):
        return "retry.timeout is the seconds a request may take (> 0), or null for the connector's own"
    statuses = block.get("statuses")
    if statuses is not None and not (isinstance(statuses, list) and all(
            isinstance(s, int) and not isinstance(s, bool) and 100 <= s <= 599 for s in statuses)):
        return "retry.statuses is a list of HTTP statuses, e.g. [429, 500, 502, 503, 504]"
    return None


def is_policy(block: Any) -> bool:
    """A retry setting for every connector (vs. ``{service: setting}``)."""
    return not isinstance(block, dict) or set(block) <= set(KEYS)


def policy_of(owner: Any) -> RetryPolicy:
    policy = getattr(owner, "retry", None)
    return policy if isinstance(policy, RetryPolicy) else RetryPolicy()


def _retryable_error(exc: BaseException) -> Optional[str]:
    """What went wrong, when it's worth trying again (a timeout, a dropped or refused connection)."""
    if isinstance(exc, requests.exceptions.Timeout):
        return f"{type(exc).__name__} (no answer in time)"
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError)):
        return f"{type(exc).__name__} (the connection failed or dropped)"
    return None


def send(policy: Optional[RetryPolicy], component: str, call: Callable[[float], Any], default_timeout: float,
         what: str = "", sleep: Callable[[float], None] = time.sleep) -> Any:
    """``call(timeout)`` — a ``requests`` call returning a response — tried again as ``policy`` says. Returns the
    last response (a status not retried, or the last try's); raises the last error once every try failed."""
    from . import progress

    policy = policy or RetryPolicy()
    timeout = policy.timeout or default_timeout
    logger = get_logger(component)
    tries = policy.retries + 1
    for attempt in range(tries):
        last = attempt == tries - 1
        try:
            response = call(timeout)
        except Exception as exc:  # noqa: BLE001 — only the retryable kinds are tried again
            reason = _retryable_error(exc)
            if reason is None:
                raise
            if last:
                raise _gave_up(exc, tries, timeout) from exc
            retry_after = None
        else:
            status = getattr(response, "status_code", None)
            if status not in policy.statuses or last:
                if status in policy.statuses and tries > 1:
                    logger.warning("%s: %s still answered %s after %d tries", component, what or "request",
                                   status, tries)
                return response
            reason = f"HTTP {status}"
            retry_after = (getattr(response, "headers", None) or {}).get("Retry-After")
            url = getattr(getattr(response, "request", None), "url", None) or getattr(response, "url", None)
            if url and not what:
                what = redact_url(str(url))
            close = getattr(response, "close", None)
            if callable(close):
                close()
        wait = policy.wait_before(attempt, retry_after)
        logger.warning("%s: %s — %s; try %d of %d in %.0fs", component, what or "request", reason, attempt + 2,
                       tries, wait)
        progress.wait(wait, sleep)
    raise AssertionError("unreachable")  # pragma: no cover


def _gave_up(exc: BaseException, tries: int, timeout: float) -> BaseException:
    """The same kind of error, saying how many tries it took and what to change."""
    message = (f"{exc} — gave up after {tries} tr{'y' if tries == 1 else 'ies'} of up to {timeout:g}s each; "
               f'raise the service\'s "retry": {{"timeout": …, "retries": …}} in duckduck.json '
               f"(or the pipeline's \"retry\")")
    try:
        return type(exc)(message, request=getattr(exc, "request", None), response=getattr(exc, "response", None))
    except Exception:  # noqa: BLE001 — an exception type with another signature keeps its own message
        return exc


def describe(policy: RetryPolicy) -> Dict[str, Any]:
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in dataclasses.asdict(policy).items()}

"""
What one search cost: decision-engine calls, LLM calls, their tokens, time
and — where the provider reports it — money.

``SemanticSearch.search`` runs inside ``metered()``; the decision engine
(``JEVAdapter._call``), the Decisions API client (``JevClient``, which
reports ``usage.cost``) and every LLM client (``llm._log_call``) add to
the ``Usage`` of the search in progress through a ``ContextVar`` — so
concurrent searches in the web app never mix, and nothing is counted
outside a search. Thread pools that run engine calls or probes submit
through ``submit_in_context`` so the counting follows them.

Counted per role: ``engine_*`` is every call to the decision engine
(Jev, or the LLM when it decides — the ``llm_decides`` reader), ``llm_*``
every LLM call (reading, extraction, and deciding in ``llm_decides``).
``cost`` sums only what providers report (the Decisions API does; the
Anthropic API reports tokens, not money) — ``None`` when none did.

Nothing is counted twice: a search counts the calls *it* made; an LLM
reading made while typing and reused from the cache counts as ``reused``
in the search, and as a call in the preview that made it
(``SemanticSearch.preview`` is metered too, recorded per mode by the
feedback store — the cost of typing).
"""

import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional


@dataclass
class Usage:
    engine_calls: int = 0
    engine_questions: int = 0
    engine_seconds: float = 0.0
    llm_calls: int = 0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    llm_seconds: float = 0.0
    cost: Optional[float] = None
    #: LLM readings reused from the cache (made while typing, by the preview) — no call made here.
    reused: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine_calls": self.engine_calls, "engine_questions": self.engine_questions,
            "engine_seconds": round(self.engine_seconds, 3), "llm_calls": self.llm_calls,
            "llm_tokens_in": self.llm_tokens_in, "llm_tokens_out": self.llm_tokens_out,
            "llm_seconds": round(self.llm_seconds, 3),
            "cost": None if self.cost is None else round(self.cost, 8),
            "reused": self.reused,
        }

    @property
    def spent(self) -> bool:
        """Anything called at all (an all-cached preview spent nothing)."""
        return bool(self.engine_calls or self.llm_calls)


_CURRENT: contextvars.ContextVar[Optional[Usage]] = contextvars.ContextVar("duckduck_usage", default=None)


@contextmanager
def metered() -> Iterator[Usage]:
    """Counts what runs inside into a fresh ``Usage`` (nested: the inner one counts, the outer doesn't)."""
    usage = Usage()
    token = _CURRENT.set(usage)
    try:
        yield usage
    finally:
        _CURRENT.reset(token)


def record_engine(questions: int, seconds: float) -> None:
    usage = _CURRENT.get()
    if usage is None:
        return
    with usage._lock:
        usage.engine_calls += 1
        usage.engine_questions += max(int(questions or 0), 0)
        usage.engine_seconds += max(float(seconds or 0.0), 0.0)


def record_llm(tokens_in: Any, tokens_out: Any, seconds: float) -> None:
    usage = _CURRENT.get()
    if usage is None:
        return
    with usage._lock:
        usage.llm_calls += 1
        usage.llm_tokens_in += tokens_in if isinstance(tokens_in, int) else 0
        usage.llm_tokens_out += tokens_out if isinstance(tokens_out, int) else 0
        usage.llm_seconds += max(float(seconds or 0.0), 0.0)


def record_cost(cost: Any) -> None:
    usage = _CURRENT.get()
    if usage is None or not isinstance(cost, (int, float)):
        return
    with usage._lock:
        usage.cost = (usage.cost or 0.0) + float(cost)


def record_reuse() -> None:
    usage = _CURRENT.get()
    if usage is None:
        return
    with usage._lock:
        usage.reused += 1


def submit_in_context(pool: Any, fn: Any, *args: Any) -> Any:
    """``pool.submit`` that keeps the caller's context (so the search's ``Usage`` sees the work)."""
    return pool.submit(contextvars.copy_context().run, fn, *args)

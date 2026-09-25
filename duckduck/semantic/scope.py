"""
The tables one question may use — chosen by the user ("only the SharePoint data").

``SemanticSearch.search(question, only_sources=[...])`` sets it for the
duration of that one search (a ``ContextVar``, so concurrent requests in the
web app don't see each other's choice). Everything that picks tables reads
it: retrieval candidates, the planner's primary source and join paths,
``lookup`` / ``locate`` / ``catalog`` answers. It narrows
``allowed_sources``; it never widens it.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import FrozenSet, Iterable, Iterator, Optional

_ONLY: ContextVar[Optional[FrozenSet[str]]] = ContextVar("duckduck_only_sources", default=None)


def in_scope(source: str) -> bool:
    """May this search use ``source``? (Always, unless the user narrowed it.)"""
    only = _ONLY.get()
    return only is None or source in only


def current() -> Optional[FrozenSet[str]]:
    return _ONLY.get()


@contextmanager
def only_sources(sources: Optional[Iterable[str]]) -> Iterator[None]:
    token = _ONLY.set(frozenset(sources) if sources is not None else None)
    try:
        yield
    finally:
        _ONLY.reset(token)

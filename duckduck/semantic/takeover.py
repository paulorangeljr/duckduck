"""
"Take over from here": an answer's rows become a table of their own.

After an answer, ``search.take_over(result, "severities")`` (or
``conversation.take_over(...)``, or *Take over from here* in the web app)
registers its rows in the same ``DuckAPI`` the search reads from. From then
on ``duck.sql`` — and the web app's SQL tab — queries them like any other
table: filter, aggregate, join them with the source tables.

- **What gets registered.** The answer's rows. When the answer stopped at its
  row cap (``default_limit``, 1000), its validated plan runs again without
  it (up to ``plan.MAX_LIMIT``), so the table holds the whole subset, not the
  first page of it. ``full=False`` keeps exactly the rows shown.
- **A snapshot.** The rows are kept in memory, as they were when taken over:
  querying the table never calls the sources again. Take over again for
  fresh rows (the same name replaces it).
- **Lifetime.** The Python process (the web app: until it restarts or the
  config is saved, which reconnects everything). Nothing is written to disk.
- **Names** are SQL identifiers (letters, digits, ``_``), not a DuckDB
  reserved word and not a table a connector registered; the default comes
  from the question ("What are the severities of the events?" →
  ``severities_events``).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from .plan import MAX_LIMIT
from .text import STOPWORDS

NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
#: What ``list_tables()`` / the SQL tab show as these tables' system.
SERVICE = "taken over"


@dataclass
class TakenOver:
    name: str
    question: str
    rows: int
    columns: List[str]
    #: How the rows were obtained: the answer's own rows, or its plan run again without the row cap.
    how: str
    taken_at: datetime = field(default_factory=datetime.now)

    @property
    def sql(self) -> str:
        return f"SELECT * FROM {self.name} LIMIT 100"

    def report(self) -> str:
        return (f"Registered '{self.name}': {self.rows} row{'s' if self.rows != 1 else ''}, "
                f"{len(self.columns)} column{'s' if len(self.columns) != 1 else ''} ({self.how}).\n"
                f"Query it with duck.sql(\"{self.sql}\").")

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "question": self.question, "rows": self.rows, "columns": self.columns,
                "how": self.how, "taken_at": self.taken_at.isoformat(timespec="seconds"), "sql": self.sql}


def _check(search: Any, result: Any) -> None:
    if search.duck is None:
        raise ValueError("take_over needs the DuckAPI the search reads from (SemanticSearch(..., duck))")
    if result.status != "ok" or result.results is None:
        raise ValueError(f"only an answer with rows can be taken over (this one is {result.status!r}"
                         f"{', a direct reply' if getattr(result, 'reply', None) else ''})")


def proposal(search: Any, result: Any) -> Dict[str, Any]:
    """
    What taking ``result`` over would give, before doing it — the web app's
    dialog: the suggested name, the rows and columns, and whether the answer
    stopped at its row cap (``capped``: ``full`` would run it again whole).
    """
    _check(search, result)
    plan = result.query_plan
    rows = len(result.results)
    return {"name": _name(search, None, result), "rows": rows, "columns": [str(c) for c in result.results.columns],
            "capped": bool(plan is not None and rows >= plan.limit), "limit": plan.limit if plan is not None else None,
            "question": result.question, "taken": sorted(search.taken_over)}


def take_over(search: Any, result: Any, name: Optional[str] = None, full: bool = True,
              now: Optional[datetime] = None) -> TakenOver:
    """See the module docstring. Raises ``ValueError`` for an answer without rows or a bad name."""
    _check(search, result)
    duck = search.duck
    frame, how = result.results, "the rows of the answer"
    plan = result.query_plan
    if full and plan is not None and search.executor is not None and len(frame) >= plan.limit:
        frame, _, _ = search.executor.execute(plan.model_copy(update={"limit": MAX_LIMIT}), now or search.clock())
        how = f"its plan run again without the {plan.limit}-row cap"
    name = _name(search, name, result)
    snapshot = frame.reset_index(drop=True).copy()

    def dataset(limit: Optional[int] = None) -> pd.DataFrame:
        return snapshot if limit is None else snapshot.head(limit)

    dataset.__module__ = __name__
    dataset.__name__ = dataset.__qualname__ = name
    dataset.__doc__ = f"Taken over from the answer to: {result.question}"
    duck.register_api_function(name, dataset)
    duck.service_of[name] = SERVICE
    taken = TakenOver(name=name, question=result.question, rows=len(snapshot),
                      columns=[str(c) for c in snapshot.columns], how=how)
    search.taken_over[name] = taken
    return taken


def _name(search: Any, name: Optional[str], result: Any) -> str:
    """The user's name (checked), or one made from the question that nothing uses yet."""
    duck = search.duck
    ours = set(search.taken_over) | {n for n, svc in duck.service_of.items() if svc == SERVICE}
    if name is not None:
        name = name.strip().lower()
        if not NAME_RE.match(name):
            raise ValueError(f"{name!r} isn't a table name: use letters, digits and _, starting with a letter")
        if _reserved(search, name):
            raise ValueError(f"{name!r} is a reserved word in SQL — pick another name")
        if name in duck.functions and name not in ours:
            raise ValueError(f"{name!r} is already a table ({duck.service_of.get(name) or 'registered'}) "
                             f"— pick another name")
        return name  # one of ours: taken over again, replaced
    question = getattr(result.intent, "working_question", None) or result.question
    text = unicodedata.normalize("NFKD", question).encode("ascii", "ignore").decode().lower()
    words = [w for w in re.findall(r"[a-z0-9]+", text) if len(w) > 2 and w not in STOPWORDS and w not in _FILLER]
    base = "_".join(words[:3]) or "answer"
    if not re.match(r"[a-z_]", base):
        base = "answer_" + base
    base = base[:50]
    candidate, n = base, 2
    while candidate in duck.functions or _reserved(search, candidate):
        candidate, n = f"{base}_{n}", n + 1
    return candidate


_FILLER = {"what", "which", "are", "were", "the", "show", "list", "give", "get", "all", "have", "has", "how",
           "many", "much", "who", "whom", "when", "where", "does", "did", "there", "bring", "tell", "about",
           "quais", "qual", "sao", "mostre", "liste", "traga", "todos", "todas", "dos", "das", "que"}


def _reserved(search: Any, name: str) -> bool:
    try:
        return bool(search.duck.conn.execute(
            "SELECT 1 FROM duckdb_keywords() WHERE keyword_name = ? AND keyword_category = 'reserved'",
            [name]).fetchone())
    except Exception:
        return False

"""
Feedback: every search's decision trail, and what the user said about it.

``FeedbackStore`` keeps two things in a DuckDB file:

- **searches** — one row per ``search()`` (each round of a conversation):
  the question (as asked, in English, and as a *template* with its values
  replaced by their types — "what can I find for this ip <ip_address>"),
  status, answer shape, entity, activity, the tables that answered, every
  decision with its probability and who made it, the user's pins, the SQL,
  how many rows came back (never the rows themselves), and the catalog /
  engine versions — so numbers from different versions aren't mixed up.
- **feedback** — per search: ``answered`` / ``partial`` / ``not_answered``,
  what went wrong (``CATEGORIES``), why (free text), and optionally what
  was right (``expected``: tables, answer shape, entity, activity, a
  ``word → field value`` synonym).

What's built on it — none of it changes anything by itself:

- ``stats()`` — answer rate overall, per answer shape, per table, per
  category, per day (the dashboard).
- ``to_evaluation()`` — rated questions as an evaluation dataset
  (``evaluation.json`` shape): answered ones label themselves, corrected
  ones carry the correction. Feeds ``evaluate`` and ``calibrate``.
- ``cases()`` — confirmed and corrected questions, for ``CaseMemory``
  (evidence for the decision engine) and ``CatalogSuggester``.
"""

import datetime as dt
import hashlib
import json
import re
import threading
import uuid
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

VERDICTS = ("answered", "partial", "not_answered")

#: What went wrong — the reliable, countable part of feedback (the ``reason`` text is the rest).
CATEGORIES: Dict[str, str] = {
    "wrong_tables": "looked in the wrong tables",
    "wrong_answer_kind": "wrong kind of answer (a list instead of a count, ...)",
    "wrong_filter": "filtered on the wrong thing",
    "wrong_values": "misread a value or a word",
    "missing_data": "the data to answer isn't there",
    "too_many_questions": "asked me too many questions",
    "other": "something else",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id VARCHAR PRIMARY KEY,
    created_at TIMESTAMP,
    conversation_id VARCHAR,
    user_name VARCHAR,
    question VARCHAR,
    english_question VARCHAR,
    template VARCHAR,
    status VARCHAR,
    answer_shape VARCHAR,
    entity VARCHAR,
    activity VARCHAR,
    sources VARCHAR,
    decisions VARCHAR,
    pinned VARCHAR,
    sql VARCHAR,
    rows INTEGER,
    clarification VARCHAR,
    elapsed_ms DOUBLE,
    catalog_version VARCHAR,
    engine VARCHAR
);
CREATE TABLE IF NOT EXISTS feedback (
    id VARCHAR PRIMARY KEY,
    search_id VARCHAR,
    created_at TIMESTAMP,
    user_name VARCHAR,
    verdict VARCHAR,
    categories VARCHAR,
    reason VARCHAR,
    expected VARCHAR
);
CREATE TABLE IF NOT EXISTS reviews (
    suggestion_id VARCHAR,
    created_at TIMESTAMP,
    user_name VARCHAR,
    status VARCHAR,
    detail VARCHAR
);
"""


def catalog_version(catalog: Any) -> str:
    """A short fingerprint of the catalog, so feedback on different versions isn't mixed up."""
    return hashlib.sha1(catalog.model_dump_json(by_alias=True).encode("utf-8")).hexdigest()[:12]


def template_of(result: Any) -> str:
    """The question with its values replaced by their kinds: "what can I find for this ip <ip_address>"."""
    from .memory import question_template

    intent = getattr(result, "intent", None)
    if intent is None:
        return question_template(result.question, [])
    return question_template(intent.working_question, intent.literals)


def answer_sources(result: Any) -> List[str]:
    """The tables that answered: the plan's, or (lookup / locate) those where the value was found."""
    if getattr(result, "query_plan", None) is not None:
        return list(result.query_plan.sources)
    return [s.source for s in getattr(result, "sections", []) if s.rows]


class FeedbackStore:
    """Searches and feedback in a DuckDB file (``":memory:"`` for tests). Thread-safe."""

    def __init__(self, path: str = ":memory:"):
        import duckdb

        self.path = path
        self._lock = threading.Lock()
        self._conn = duckdb.connect(path)
        self._conn.execute(_SCHEMA)
        #: Bumped on every write — lets readers (``CaseMemory``) cache until something changes.
        self.version = 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writing -------------------------------------------------------

    def record_search(self, result: Any, conversation_id: Optional[str] = None, user: Optional[str] = None,
                      catalog_version: Optional[str] = None, engine: Optional[str] = None) -> str:
        """Stores a ``SearchResult``'s decision trail (never its rows) and returns its id."""
        intent = result.intent
        search_id = uuid.uuid4().hex[:16]
        rows = None if result.results is None else int(len(result.results))
        values = [
            search_id, _now(), conversation_id, user, result.question,
            getattr(intent, "english_question", None) if intent else None, template_of(result),
            result.status, intent.answer_shape if intent else None,
            intent.target_entity if intent else None, intent.activity if intent else None,
            json.dumps(answer_sources(result)),
            json.dumps([d.model_dump(mode="json") for d in result.decisions], default=str),
            json.dumps(result.pinned, default=str), result.sql, rows, result.clarification,
            float(result.elapsed_ms or 0.0), catalog_version, engine,
        ]
        with self._lock:
            self._conn.execute(f"INSERT INTO searches VALUES ({', '.join('?' * len(values))})", values)
            self.version += 1
        return search_id

    def record_feedback(self, search_id: str, verdict: str, categories: Iterable[str] = (), reason: str = "",
                        expected: Optional[Dict[str, Any]] = None, user: Optional[str] = None) -> str:
        """
        What the user said about a search. ``expected`` (optional): what was
        right — ``sources`` (list), ``answer_shape``, ``entity``,
        ``activity``, ``synonym`` (``{"field": "src.f", "value": "DENY",
        "word": "negado"}``).
        """
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}, not {verdict!r}")
        categories = list(categories or [])
        unknown = [c for c in categories if c not in CATEGORIES]
        if unknown:
            raise ValueError(f"unknown categories {unknown}; known: {sorted(CATEGORIES)}")
        expected = _clean_expected(expected)
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM searches WHERE id = ?", [search_id]).fetchone():
                raise KeyError(f"no search {search_id!r}")
            feedback_id = uuid.uuid4().hex[:16]
            self._conn.execute(
                "INSERT INTO feedback VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [feedback_id, search_id, _now(), user, verdict, json.dumps(categories), reason or "",
                 json.dumps(expected)],
            )
            self.version += 1
        return feedback_id

    def record_review(self, suggestion_id: str, status: str, user: Optional[str] = None, detail: str = "") -> None:
        if status not in ("accepted", "dismissed"):
            raise ValueError("status must be 'accepted' or 'dismissed'")
        with self._lock:
            self._conn.execute("INSERT INTO reviews VALUES (?, ?, ?, ?, ?)",
                               [suggestion_id, _now(), user, status, detail])
            self.version += 1

    # -- reading -------------------------------------------------------

    def query(self, sql: str, params: Optional[list] = None) -> pd.DataFrame:
        with self._lock:
            return self._conn.execute(sql, params or []).df()

    def searches(self, limit: int = 100) -> pd.DataFrame:
        """The latest searches, each with its latest feedback (if any). SQL NULLs are ``None``."""
        return _nulls(self.query(f"""
            SELECT s.id, s.created_at, s.conversation_id, s.user_name, s.question, s.english_question,
                   s.status, s.answer_shape, s.entity, s.sources, s.rows, f.verdict, f.categories, f.reason
            FROM searches s LEFT JOIN ({_LATEST_FEEDBACK}) f ON f.search_id = s.id
            ORDER BY s.created_at DESC, s.rowid DESC LIMIT {int(limit)}"""))

    def search(self, search_id: str) -> Optional[Dict[str, Any]]:
        df = _nulls(self.query("SELECT * FROM searches WHERE id = ?", [search_id]))
        return _decode(df.iloc[0].to_dict()) if not df.empty else None

    def rated(self) -> List[Dict[str, Any]]:
        """Every search with feedback, joined to its latest feedback, oldest first."""
        df = self.query(f"""
            SELECT s.*, f.verdict, f.categories, f.reason, f.expected, f.created_at AS rated_at
            FROM searches s JOIN ({_LATEST_FEEDBACK}) f ON f.search_id = s.id ORDER BY f.created_at""")
        return [_decode(row) for row in _nulls(df).to_dict(orient="records")]

    def reviews(self) -> Dict[str, str]:
        """``{suggestion_id: accepted|dismissed}`` — the latest review of each."""
        df = self.query("""SELECT suggestion_id, arg_max(status, created_at) AS status
                           FROM reviews GROUP BY suggestion_id""")
        return dict(zip(df["suggestion_id"], df["status"]))

    def stats(self) -> Dict[str, Any]:
        """The dashboard: overall numbers and breakdowns (answer shape, table, category, day)."""
        base = f"""SELECT s.*, f.verdict, f.categories FROM searches s
                   LEFT JOIN ({_LATEST_FEEDBACK}) f ON f.search_id = s.id"""
        overall = self.query(f"""
            SELECT count(*) AS searches,
                   count(verdict) AS rated,
                   count(*) FILTER (WHERE verdict = 'answered') AS answered,
                   count(*) FILTER (WHERE verdict = 'partial') AS partial,
                   count(*) FILTER (WHERE verdict = 'not_answered') AS not_answered,
                   count(*) FILTER (WHERE status = 'needs_clarification') AS asked_back,
                   count(DISTINCT conversation_id) AS conversations
            FROM ({base})""").iloc[0].to_dict()
        rated = overall["rated"] or 0
        overall = {k: int(v) for k, v in overall.items()}
        overall["answer_rate"] = round(overall["answered"] / rated, 3) if rated else None
        overall["asked_back_rate"] = round(overall["asked_back"] / overall["searches"], 3) if overall["searches"] else None

        def breakdown(select: str, source: str) -> pd.DataFrame:
            return self.query(f"""
                SELECT {select} AS key, count(*) AS rated,
                       count(*) FILTER (WHERE verdict = 'answered') AS answered,
                       count(*) FILTER (WHERE verdict = 'not_answered') AS not_answered,
                       round(count(*) FILTER (WHERE verdict = 'answered') / count(*), 3) AS answer_rate
                FROM {source} WHERE verdict IS NOT NULL GROUP BY 1 ORDER BY rated DESC, 1""")

        by_source = f"(SELECT unnest(from_json(sources, '[\"VARCHAR\"]')) AS source, verdict FROM ({base}))"
        by_category = self.query(f"""
            SELECT category AS key, count(*) AS count FROM (
                SELECT unnest(from_json(categories, '[\"VARCHAR\"]')) AS category FROM ({base})
                WHERE verdict IS NOT NULL AND verdict <> 'answered')
            GROUP BY 1 ORDER BY count DESC, 1""")
        by_category["label"] = by_category["key"].map(CATEGORIES)
        return {
            "overall": overall,
            "by_answer_shape": breakdown("coalesce(answer_shape, '—')", f"({base})"),
            "by_source": breakdown("source", by_source),
            "by_category": by_category,
            "by_day": self.query(f"""
                SELECT CAST(created_at AS DATE) AS day, count(*) AS searches, count(verdict) AS rated,
                       count(*) FILTER (WHERE verdict = 'answered') AS answered
                FROM ({base}) GROUP BY 1 ORDER BY 1"""),
        }

    # -- derived -------------------------------------------------------

    def cases(self) -> List[Dict[str, Any]]:
        """
        Questions whose right reading is known, latest per template:
        ``confirmed`` (answered — the decisions made were right) or
        ``corrected`` (not answered, with ``expected`` saying what was right).
        """
        out: Dict[str, Dict[str, Any]] = {}
        for row in self.rated():
            expected = row["expected"] or {}
            if row["verdict"] == "answered" and row["status"] == "ok":
                case = {"kind": "confirmed", "sources": row["sources"], "answer_shape": row["answer_shape"],
                        "entity": row["entity"], "activity": row["activity"]}
            elif row["verdict"] != "answered" and any(expected.get(k) for k in _READING):
                case = {"kind": "corrected", "sources": expected.get("sources") or [],
                        "answer_shape": expected.get("answer_shape"), "entity": expected.get("entity"),
                        "activity": expected.get("activity")}
            else:
                out.pop(row["template"], None)  # a later "not answered" retires an old confirmation
                continue
            case.update(search_id=row["id"], question=row["question"],
                        english_question=row["english_question"], template=row["template"],
                        pinned=row["pinned"], rated_at=str(row["rated_at"]))
            out[row["template"]] = {k: v for k, v in case.items() if v not in (None, [], {})}
        return list(out.values())

    def to_evaluation(self) -> List[Dict[str, Any]]:
        """Rated questions as an evaluation dataset (``evaluation.json`` shape), one per question."""
        dataset: Dict[str, Dict[str, Any]] = {}
        for case in self.cases():
            item: Dict[str, Any] = {"question": case["question"]}
            for key, label in (("sources", "expected_sources"), ("entity", "expected_entity"),
                               ("activity", "expected_activity"), ("answer_shape", "expected_answer_shape")):
                if case.get(key):
                    item[label] = case[key]
            item["_from"] = f"feedback {case['kind']} ({case['search_id']})"
            dataset[case["question"]] = item
        return list(dataset.values())


#: The parts of a correction that say how the question should have been read (a synonym alone doesn't).
_READING = ("sources", "answer_shape", "entity", "activity")

_LATEST_FEEDBACK = """
    SELECT search_id, arg_max(verdict, created_at) AS verdict, arg_max(categories, created_at) AS categories,
           arg_max(reason, created_at) AS reason, arg_max(expected, created_at) AS expected,
           max(created_at) AS created_at
    FROM feedback GROUP BY search_id"""


def _nulls(df: pd.DataFrame) -> pd.DataFrame:
    """SQL NULL as ``None`` (not NaN)."""
    return df.astype(object).where(df.notna(), None)


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("sources", "decisions", "pinned", "categories", "expected"):
        if key in row and isinstance(row[key], str):
            try:
                row[key] = json.loads(row[key])
            except ValueError:
                pass
    return row


def _clean_expected(expected: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not expected:
        return {}
    allowed = {"sources", "answer_shape", "entity", "activity", "synonym"}
    unknown = set(expected) - allowed
    if unknown:
        raise ValueError(f"expected: unknown key(s) {sorted(unknown)}; use {sorted(allowed)}")
    out = {k: v for k, v in expected.items() if v not in (None, "", [], {})}
    if "sources" in out and isinstance(out["sources"], str):
        out["sources"] = [out["sources"]]
    syn = out.get("synonym")
    if syn is not None and not (isinstance(syn, dict) and syn.get("field") and syn.get("value") and syn.get("word")):
        raise ValueError("expected.synonym needs field, value and word")
    return out


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)

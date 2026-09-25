"""
Catalog suggestions from feedback — proposed, never applied until a person accepts them.

``CatalogSuggester.suggest()`` reads the rated searches (``FeedbackStore``)
and proposes:

- ``value_synonym`` — a correction said "*this word* means *field = value*"
  (``expected.synonym``): add the word to that value's synonyms.
- ``source_example`` — a correction said the right tables were others
  (``expected.sources``): add the question to those sources' ``examples``,
  which retrieval and the decision engine read.
- ``answer_wording`` — a correction said the answer should have been
  another kind (``expected.answer_shape``): add the question's opening
  words ("bring me information for") to that shape's wording.
- ``entity_keyword`` / ``activity_keyword`` / ``source_note`` — a word
  the catalog doesn't know shows up in at least ``min_support`` failed
  questions, and their corrections point at an entity, an activity or a
  table: make it a keyword of that entity/activity, or a note on the table.
- ``unknown_word`` — the same, with no correction saying where it belongs:
  shown for information, nothing to apply.

Each suggestion carries its evidence (the searches behind it). ``apply()``
writes an accepted one: catalog changes go into the catalog file (a
``.bak`` copy is kept; the file is rewritten, so ``#`` comments are lost),
wording into ``feedback.learned_answer_shapes``, which is loaded on top of
``answer_shapes``.
"""

import hashlib
import json
import os
import shutil
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .catalog import Catalog
from .shapes import AnswerShapes
from .text import STOPWORDS, stem, tokenize, vocabulary

KINDS = ("value_synonym", "source_example", "answer_wording", "entity_keyword", "activity_keyword",
         "source_note", "unknown_word")


@dataclass
class Suggestion:
    kind: str
    #: What changes: an entity / activity name, ``source``, ``source.field=value``, or an answer shape.
    target: Optional[str]
    #: The word, example question, note or phrase to add.
    change: str
    #: Why, in plain words.
    reason: str
    #: The searches behind it.
    evidence: List[str] = field(default_factory=list)
    status: str = "open"

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.kind}|{self.target}|{self.change.lower()}".encode()).hexdigest()[:10]

    @property
    def applicable(self) -> bool:
        return self.kind != "unknown_word" and self.target is not None

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, **asdict(self), "support": len(self.evidence), "applicable": self.applicable}


class CatalogSuggester:
    def __init__(self, store: Any, catalog: Catalog, shapes: Optional[AnswerShapes] = None, min_support: int = 2):
        self.store = store
        self.catalog = catalog
        self.shapes = shapes or AnswerShapes()
        self.min_support = min_support
        self._known = vocabulary(self._catalog_texts())

    def _catalog_texts(self) -> List[str]:
        texts: List[str] = []
        for name in self.catalog.sources:
            texts += self.catalog.source_texts(name)
            for f in self.catalog.sources[name].fields.values():
                for value, synonyms in f.values.items():
                    texts += [value, *synonyms]
        for name, e in self.catalog.entities.items():
            texts += [name, e.description, *e.keywords]
        for name, a in self.catalog.activities.items():
            texts += [name, a.description, *a.keywords]
        return [t.replace("_", " ") for t in texts if t]

    # ------------------------------------------------------------------

    def suggest(self, include_reviewed: bool = False) -> List[Suggestion]:
        rated = [r for r in self.store.rated() if r["verdict"] != "answered"]
        out: Dict[str, Suggestion] = {}

        def add(s: Suggestion) -> None:
            if s.id in out:
                out[s.id].evidence += [e for e in s.evidence if e not in out[s.id].evidence]
            else:
                out[s.id] = s

        for row in rated:
            expected = row.get("expected") or {}
            text = row.get("english_question") or row["question"]
            syn = expected.get("synonym")
            if syn:
                s = self._synonym(syn, row["id"])
                if s:
                    add(s)
            wrong_tables = sorted(set(expected.get("sources") or []) - set(row.get("sources") or []))
            for name in wrong_tables:
                if name in self.catalog.sources and row["question"] not in self.catalog.sources[name].examples:
                    add(Suggestion("source_example", name, row["question"],
                                   f"the user said '{name}' should have answered this", [row["id"]]))
            shape = expected.get("answer_shape")
            if shape and shape != row.get("answer_shape") and shape in self.shapes.descriptions:
                phrase = self._opening(text, shape)
                if phrase:
                    add(Suggestion("answer_wording", shape, phrase,
                                   f"the user wanted {shape} ({self.shapes.descriptions[shape]})"
                                   + (f", not {row['answer_shape']}" if row.get("answer_shape") else ""),
                                   [row["id"]]))

        for s in self._unknown_words(rated):
            add(s)
        reviews = self.store.reviews()
        suggestions = []
        for s in out.values():
            s.status = reviews.get(s.id, "open")
            if include_reviewed or s.status == "open":
                suggestions.append(s)
        order = {k: i for i, k in enumerate(KINDS)}
        suggestions.sort(key=lambda s: (s.status != "open", order[s.kind], -len(s.evidence), s.change))
        return suggestions

    def _synonym(self, syn: Dict[str, Any], search_id: str) -> Optional[Suggestion]:
        ref, value, word = syn["field"], str(syn["value"]), str(syn["word"]).strip()
        if not self.catalog.has_field(ref) or value not in self.catalog.field(ref).values:
            return None
        if word.lower() in {w.lower() for w in [value, *self.catalog.field(ref).values[value]]}:
            return None
        return Suggestion("value_synonym", f"{ref}={value}", word,
                          f"the user said “{word}” means {ref} = {value!r}", [search_id])

    def _opening(self, text: str, shape: str) -> Optional[str]:
        """The words before the first thing the catalog knows or a value: "bring me information for"."""
        words = []
        for tok in tokenize(text):
            if tok.isdigit() or (stem(tok) in self._known and tok not in STOPWORDS):
                break
            words.append(tok)
        while words and words[-1] in STOPWORDS | {"this", "that", "these", "those", "my", "our"}:
            words.pop()
        if len(words) < 2:
            return None
        phrase = " ".join(words)
        if shape in self.shapes.candidates(phrase)[0] and len(self.shapes.candidates(phrase)[0]) == 1:
            return None  # already worded so
        return phrase

    def _unknown_words(self, rated: List[Dict[str, Any]]) -> List[Suggestion]:
        seen: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        surface: Dict[str, Counter] = defaultdict(Counter)
        for row in rated:
            template = row.get("template") or ""
            shape_tokens = self.shapes.matched_tokens(template)
            for tok in set(tokenize(template.replace("<", " <").replace(">", "> "))):
                st = stem(tok)
                if (tok in STOPWORDS or tok in shape_tokens or len(tok) < 3 or tok.isdigit()
                        or st in self._known or f"<{tok}>" in template):
                    continue
                seen[st].append(row)
                surface[st][tok] += 1
        out = []
        for st, rows in seen.items():
            if len({r["id"] for r in rows}) < self.min_support:
                continue
            word = surface[st].most_common(1)[0][0]
            ids = sorted({r["id"] for r in rows})
            votes = {key: Counter((r.get("expected") or {}).get(key) for r in rows if (r.get("expected") or {}).get(key))
                     for key in ("entity", "activity")}
            sources = Counter(s for r in rows for s in (r.get("expected") or {}).get("sources") or [])
            if votes["entity"] and votes["entity"].most_common(1)[0][0] in self.catalog.entities:
                target = votes["entity"].most_common(1)[0][0]
                out.append(Suggestion("entity_keyword", target, word,
                                      f"“{word}” isn't in the catalog; the corrections point at {target}", ids))
            elif votes["activity"] and votes["activity"].most_common(1)[0][0] in self.catalog.activities:
                target = votes["activity"].most_common(1)[0][0]
                out.append(Suggestion("activity_keyword", target, word,
                                      f"“{word}” isn't in the catalog; the corrections point at {target}", ids))
            elif sources and sources.most_common(1)[0][0] in self.catalog.sources:
                target = sources.most_common(1)[0][0]
                out.append(Suggestion("source_note", target, f"People call this “{word}”.",
                                      f"“{word}” isn't in the catalog; the corrections point at {target}", ids))
            else:
                out.append(Suggestion("unknown_word", None, word,
                                      f"“{word}” isn't in the catalog and shows up in {len(ids)} unanswered "
                                      f"questions — say which entity/table it belongs to in their feedback", ids))
        return out


# ---------------------------------------------------------------------------
# Applying an accepted suggestion
# ---------------------------------------------------------------------------


def apply_suggestion(s: Suggestion, catalog_path: str, learned_shapes_path: str) -> str:
    """Writes an accepted suggestion; returns what was changed, in words."""
    if not s.applicable:
        raise ValueError(f"a {s.kind} suggestion has nothing to apply")
    if s.kind == "answer_wording":
        return _add_wording(learned_shapes_path, s.target, s.change)
    data = _read(catalog_path)
    if s.kind == "value_synonym":
        ref, value = s.target.split("=", 1)
        src, fname = ref.split(".", 1)
        synonyms = data["sources"][src]["fields"][fname].setdefault("values", {}).setdefault(value, [])
        _append(synonyms, s.change)
        what = f"“{s.change}” is now a synonym of {ref} = {value!r}"
    elif s.kind == "source_example":
        _append(data["sources"][s.target].setdefault("examples", []), s.change)
        what = f"{s.target} has a new example question"
    elif s.kind in ("entity_keyword", "activity_keyword"):
        section = "entities" if s.kind == "entity_keyword" else "activities"
        _append(data[section][s.target].setdefault("keywords", []), s.change)
        what = f"“{s.change}” is now a keyword of {s.target}"
    elif s.kind == "source_note":
        src = data["sources"][s.target]
        notes = (src.get("notes") or "").strip()
        if s.change not in notes:
            src["notes"] = (notes + "\n" + s.change).strip()
        what = f"{s.target} has a new note"
    else:
        raise ValueError(f"unknown suggestion kind {s.kind!r}")
    catalog = Catalog.model_validate(data)  # never write an invalid catalog
    _write(catalog_path, catalog)
    return what


def _append(items: list, value: str) -> None:
    if value.lower() not in {str(v).lower() for v in items}:
        items.append(value)


def _read(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        if path.lower().endswith(".json"):
            return json.load(f)
        import yaml

        return yaml.safe_load(f) or {}


def _write(path: str, catalog: Catalog) -> None:
    shutil.copyfile(path, path + ".bak")
    if path.lower().endswith(".json"):
        text = json.dumps(catalog.model_dump(by_alias=True, exclude_defaults=True, mode="json"), indent=2)
    else:
        from .generation import GenerationResult

        text = GenerationResult(catalog=catalog).to_yaml()
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _add_wording(path: str, shape: str, phrase: str) -> str:
    data: Dict[str, Any] = {}
    if os.path.exists(path):
        data = _read(path)
    spec = data.setdefault(shape, {})
    _append(spec.setdefault("wording", []), phrase)
    AnswerShapes(data)  # never write wording that wouldn't load
    import yaml

    with open(path, "w", encoding="utf-8") as f:
        f.write("# Answer wording accepted from feedback suggestions — loaded on top of semantic.answer_shapes.\n")
        f.write(yaml.safe_dump(data, sort_keys=True, allow_unicode=True))
    return f"“{phrase}” now means {shape}"


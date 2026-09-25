"""
The questions asked back to the user — plain language, built from
templates and the catalog's own descriptions (never an LLM).

Every text is a template in ``DEFAULT_TEXTS``; override any of them with
``semantic.clarification_texts`` in ``duckduck.json`` (e.g. to write them
in Portuguese). Placeholders come from the catalog:

- ``{question}`` — the user's question
- ``{value}`` / ``{term}`` — the value / the words used for it
- ``{field}`` / ``{field_name}`` — a field's description / its ``source.field`` name
- ``{source}`` / ``{source_name}`` — a source's description / its name
- ``{value_label}`` — an enumerated value as stored
- ``{entity}`` / ``{entity_name}`` — an entity's description / name
- ``{other_source}`` / ``{other_field}`` — the far side of a join

Each clarification has a ``question``, a ``context`` (why it's being
asked) and options; yes/no options carry a ``detail`` (what answering it
does).
"""

import re
import string
from typing import Any, Dict, Iterable, Optional

from .catalog import Catalog
from .intent import Clarification, ClarificationOption

DEFAULT_TEXTS: Dict[str, str] = {
    "yes": "yes",
    "no": "no",
    "none": "none of these",
    "entity.question": "What should the answer list?",
    "entity.question_count": "What should I count?",
    "entity.context": "“{question}” could be asking for more than one kind of thing.",
    "value_type.question": "What is “{value}”?",
    "value_type.context": "I don't know what kind of information “{value}” is, so I don't know where to look for it.",
    "value_type.context_retry": "You said “{value}” isn't the {field}, and none of the other data that fits your question has that kind of field.",
    "entity.context_reachable": "I couldn't find {entity} in the {source}, or in anything connected to it. "
                                "From there, the answer can list:",
    "ignore_term.question": "Should I answer without “{term}”, then?",
    "ignore_term.context": "I couldn't find another meaning for “{term}” in the data.",
    "ignore_term.yes": "answer without filtering on it",
    "ignore_term.no": "I'll rephrase the question",
    "value_term.question": "Which of these is what you want me to look for?",
    "value_term.context": "More than one word in “{question}” could be the value to search for.",
    "source.question": "Which of these should answer your question?",
    "source.context": "None of the data I have looked clearly right for “{question}”.",
    "field_filter.question": "Should I look for “{value}” in the {field}?",
    "field_filter.context": "That's the {field} in the {source} ({field_name}); I'm not sure that's where “{value}” belongs.",
    "field_filter.yes": "look for it there",
    "field_filter.no": "it means something else",
    "field_value.question": "By “{term}”, do you mean records whose {field} is “{value_label}”?",
    "field_value.context": "In the {source}, the {field} ({field_name}) has a value “{value_label}” that looks like what you wrote.",
    "field_value.yes": "only those records",
    "field_value.no": "“{term}” means something else",
    "field_return.question": "Should the answer list the {field}?",
    "field_return.context": "In the {source}, that's how {entity} is recorded ({field_name}).",
    "field_return.yes": "list it",
    "field_return.no": "that's not what I'm asking for",
    "answer_shape.question": "What kind of answer do you want?",
    "answer_shape.context": "“{question}” could be asking for more than one kind of answer.",
    "answer_shape.list": "a list of what matches",
    "answer_shape.count": "how many there are (one number)",
    "answer_shape.values": "the different values of something",
    "answer_shape.count_values": "how many different values something has",
    "answer_shape.count_by": "a count for each value of something (a breakdown)",
    "answer_shape.lookup": "everything about it, from every table that has it",
    "answer_shape.locate": "which tables have it",
    "answer_shape.catalog": "what data I have access to",
    "reply.out_of_scope": "I couldn't find anything about that in the data I have.",
    "reply.topics": "I can answer questions about {topics}.",
    "reply.examples": "For example:",
    "reply.ask_anyway": "It is about the data — try anyway",
    "reply.greeting": "Hi! Ask me anything about the data I have.",
    "reply.thanks": "You're welcome!",
    "reply.goodbye": "Bye — come back any time.",
    "values_field.question": "The different values of what?",
    "values_field.context": "“{question}” asks for the different values of something in the {source}, "
                            "but I'm not sure of what.",
    "count_by_field.question": "A count for each what?",
    "count_by_field.context": "“{question}” asks for a count broken down by something in the {source}, "
                              "but I'm not sure by what.",
    "join.question": "Should I match the {field} in the {source} with the {other_field} in the {other_source}?",
    "join.context": "That's how the two can be combined to answer; if they don't refer to the same thing, I'll look for another way.",
    "join.yes": "combine them",
    "join.no": "they're unrelated",
}

_PLACEHOLDERS = {"topics", "question", "value", "term", "field", "field_name", "source", "source_name", "value_label",
                 "entity", "entity_name", "other_source", "other_field"}


def human(name: str) -> str:
    """``destination_domain`` → ``destination domain``."""
    return name.replace("_", " ").strip()


def first_sentence(text: str) -> str:
    return (text or "").strip().split(". ")[0].rstrip(".").strip()


def phrase(description: str, name: str, max_words: int = 6) -> str:
    """
    How to name something inside a sentence: its description when that's a
    short noun phrase ("web proxy traffic"), else its humanized name —
    generated descriptions are often whole sentences ("Each row represents
    a security alert triggered by…") that don't fit mid-sentence.
    """
    text = short(description)
    words = text.split()
    if not words or len(words) > max_words or re.match(r"(?i)^(each|every|one|a single)\b.*\brows?\b", first_sentence(description)):
        return human(name)
    return _lower_first(text)


def short(text: str) -> str:
    """The first sentence up to its first comma/dash — for use inside a sentence."""
    first = first_sentence(text)
    for cut in (" — ", " - ", ", "):
        first = first.split(cut)[0]
    return first.strip()


class ClarificationTexts:
    """``DEFAULT_TEXTS`` with overrides — validated: known keys, known placeholders."""

    def __init__(self, overrides: Optional[Dict[str, str]] = None):
        overrides = dict(overrides or {})
        unknown = sorted(set(overrides) - set(DEFAULT_TEXTS))
        if unknown:
            raise ValueError(f"clarification_texts: unknown key(s) {unknown}; known: {sorted(DEFAULT_TEXTS)}")
        for key, text in overrides.items():
            names = {f for _, f, _, _ in string.Formatter().parse(text) if f}
            bad = sorted(names - _PLACEHOLDERS)
            if bad:
                raise ValueError(f"clarification_texts[{key!r}]: unknown placeholder(s) {bad}; known: {sorted(_PLACEHOLDERS)}")
        self.texts = {**DEFAULT_TEXTS, **overrides}

    def t(self, key: str, **values: Any) -> str:
        return self.texts[key].format_map(_Blank(values))

    # ------------------------------------------------------------------
    # one builder per kind
    # ------------------------------------------------------------------

    def entity(self, question: str, ranked: Iterable[str], catalog: Catalog, context: Optional[str] = None,
               counting: bool = False) -> Clarification:
        ranked = list(ranked)
        return Clarification(
            kind="entity", question=self.t("entity.question_count" if counting else "entity.question", question=question),
            context=context or self.t("entity.context", question=question),
            options=[ClarificationOption(value=e, label=_label(catalog.entities[e].description, e),
                                         pins={"entity": e}) for e in ranked]
            + [self._none({f"not_entity:{e}": True for e in ranked})],
        )

    def entity_reachable(self, question: str, entity: str, source: str, reachable: Iterable[str],
                         catalog: Catalog) -> Clarification:
        """The entity asked for can't be reached from the primary source — offer the ones that can."""
        edef = catalog.entities.get(entity)
        context = self.t(
            "entity.context_reachable", question=question, entity_name=entity, source_name=source,
            entity=phrase(edef.description if edef else "", entity),
            source=phrase(catalog.sources[source].description, source),
        )
        return self.entity(question, reachable, catalog, context=_upper_first(context))

    def value_type(self, question: str, value: str, ranked: Iterable[str],
                   rejected_field: Optional[str] = None, catalog: Optional[Catalog] = None) -> Clarification:
        context = self.t("value_type.context", question=question, value=value)
        if rejected_field and catalog is not None:
            context = self.t("value_type.context_retry", **self._field_values(question, rejected_field, catalog, value=value))
        return Clarification(
            kind="value_type", question=self.t("value_type.question", question=question, value=value),
            context=_upper_first(context),
            options=[ClarificationOption(value=t, label=human(t), pins={f"value:{value}": t}) for t in ranked],
        )

    def ignore_term(self, question: str, term: str) -> Clarification:
        return self._yes_no("ignore_term", "ignore_term", f"term:{term}", {"question": question, "term": term},
                            yes_value="ignore")

    def value_term(self, question: str, terms: Iterable[str]) -> Clarification:
        return Clarification(
            kind="value_term", question=self.t("value_term.question", question=question),
            context=self.t("value_term.context", question=question),
            options=[ClarificationOption(value=t, label=t, pins={"value_term": t}) for t in terms],
        )

    def source(self, question: str, names: Iterable[str], catalog: Catalog) -> Clarification:
        names = list(names)
        return Clarification(
            kind="source", question=self.t("source.question", question=question),
            context=self.t("source.context", question=question),
            options=[ClarificationOption(value=n, label=_label(catalog.sources[n].description, n),
                                         pins={f"source:{n}": True}) for n in names]
            + [self._none({f"source:{n}": False for n in names})],
        )

    def answer_shape(self, question: str, shapes: Iterable[str]) -> Clarification:
        return Clarification(
            kind="answer_shape", question=self.t("answer_shape.question", question=question),
            context=self.t("answer_shape.context", question=question),
            options=[ClarificationOption(value=s, label=self.t(f"answer_shape.{s}", question=question),
                                         pins={"answer_shape": s}) for s in shapes],
        )

    def values_field(self, question: str, refs: Iterable[str], catalog: Catalog, prefix: str = "values_field") -> Clarification:
        """Which field a values / count-per-group answer is about. "None of these" → answer as a plain list."""
        refs = list(refs)
        values = self._field_values(question, refs[0], catalog)
        options = []
        for ref in refs:
            src, name = ref.split(".", 1)
            fdef = catalog.sources[src].fields.get(name)
            options.append(ClarificationOption(value=ref, label=_label(fdef.description if fdef else "", human(name)),
                                               pins={"values_field": ref}))
        return Clarification(
            kind="values_field", question=self.t(f"{prefix}.question", **values),
            context=_upper_first(self.t(f"{prefix}.context", **values)),
            options=options + [self._none({"answer_shape": "list"})],
        )

    def _none(self, pins: Dict[str, Any]) -> ClarificationOption:
        """'None of these' — rules the shown options out, so the next round offers others."""
        return ClarificationOption(value="none", label=self.t("none"), pins=pins)

    def field_filter(self, question: str, ref: str, value: str, catalog: Catalog) -> Clarification:
        return self._yes_no("field", "field_filter", f"field:{ref}", self._field_values(question, ref, catalog, value=value))

    def field_value(self, question: str, ref: str, term: str, stored: Any, catalog: Catalog) -> Clarification:
        values = self._field_values(question, ref, catalog, term=term, value_label=stored)
        return self._yes_no("field", "field_value", f"field:{ref}", values)

    def field_return(self, question: str, ref: str, entity: str, catalog: Catalog) -> Clarification:
        edef = catalog.entities.get(entity)
        values = self._field_values(question, ref, catalog, entity_name=entity,
                                    entity=phrase(edef.description if edef else "", entity))
        return self._yes_no("field", "field_return", f"field:{ref}", values)

    def join(self, question: str, left: str, right: str, catalog: Catalog) -> Clarification:
        values = self._field_values(question, left, catalog)
        other = self._field_values(question, right, catalog)
        values.update(other_source=other["source"], other_field=other["field"])
        return self._yes_no("join", "join", f"join:{left}={right}", values)

    # ------------------------------------------------------------------

    def _field_values(self, question: str, ref: str, catalog: Catalog, **extra: Any) -> Dict[str, Any]:
        source_name, field_name = ref.split(".", 1)
        src = catalog.sources[source_name]
        fdef = src.fields.get(field_name)
        field = phrase(fdef.description if fdef else "", field_name)
        source = phrase(src.description, source_name)
        return {"question": question, "field": _lower_first(field), "field_name": ref,
                "source": _lower_first(source), "source_name": source_name, **extra}

    def _yes_no(self, kind: str, prefix: str, key: str, values: Dict[str, Any], yes_value: Any = True) -> Clarification:
        return Clarification(
            kind=kind, question=_upper_first(self.t(f"{prefix}.question", **values)),
            context=_upper_first(self.t(f"{prefix}.context", **values)),
            options=[
                ClarificationOption(value="yes", label=self.t("yes"), detail=self.t(f"{prefix}.yes", **values),
                                    pins={key: yes_value}),
                ClarificationOption(value="no", label=self.t("no"), detail=self.t(f"{prefix}.no", **values), pins={key: False}),
            ],
        )


class _Blank(dict):
    """``format_map`` that leaves an unknown placeholder empty instead of failing."""

    def __missing__(self, key: str) -> str:
        return ""


def _label(description: str, name: str) -> str:
    first = first_sentence(description)
    return f"{first} ({name})" if first else name


def _lower_first(text: str) -> str:
    """'Destination domain' → 'destination domain' (keeps acronyms like 'IP address')."""
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        return text[0].lower() + text[1:]
    return text


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text

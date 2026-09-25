"""
Which kind of answer a question asks for — and so which SQL — read off its
wording.

Each shape has ``wording``: phrases (matched as whole words,
case-insensitive, any spacing) or ``re:<regex>``. ``count_by`` also has
``maybe_wording`` — words that *might* mean a breakdown ("by", "por") and
send the choice to the decision engine instead of settling it. Everything
here is extended — or, with ``replace: true``, replaced — per shape by
``semantic.answer_shapes`` in ``duckduck.json`` (or a JSON/YAML file named
there), so wording can be added as it's noticed, without code.

How the wording combines (``AnswerShapes.candidates``) — one candidate is
settled without a model, several go to the engine, none means a list:

- ``locate`` ("which tables have X") → locate; ``lookup`` ("everything about X") → lookup
- ``count`` + ``count_by`` → count_by; ``count`` + ``values`` (or ``count_values``) → count_values
- ``count`` + a ``maybe_wording`` word → count or count_by
- ``count`` → count; ``values`` → values; ``count_by`` alone → list or count_by
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .text import tokenize

#: The kinds of answer a question can ask for → what each one returns (also what the engine reads).
ANSWER_SHAPES: Dict[str, str] = {
    "list": "the matching records or things (SELECT / SELECT DISTINCT entity)",
    "count": "how many there are, one number (COUNT)",
    "values": "the different values of an attribute (SELECT DISTINCT field)",
    "count_values": "how many different values an attribute has (COUNT DISTINCT field)",
    "count_by": "a count for each value of an attribute — a breakdown (GROUP BY field)",
    "lookup": "everything the data holds about a given value, from every table that has it",
    "locate": "which tables contain a given value (or hold that kind of thing), not the rows themselves",
    "catalog": "what data there is to ask about — the tables and what they hold, from the catalog (no data read)",
}
#: The shapes that need to know which field they're about.
FIELD_SHAPES = ("values", "count_values", "count_by")
#: The shapes answered across every table holding the question's value (one query per table).
ACROSS_SHAPES = ("lookup", "locate")

DEFAULT_WORDING: Dict[str, Dict[str, List[str]]] = {
    "catalog": {"wording": [
        "re:\\bwhat (kinds?|types?|sorts?) of (information|info|data|tables|sources|datasets)\\b",
        "re:\\bwhat (information|info|data|tables|sources|datasets)( do| can| does)? (you|we|i) (have|access|see|know|hold|cover|query)",
        "re:\\b(which|what) (tables|sources|datasets|data sources) (do you have|are available|are there|exist)\\b",
        "have access to", "has access to", "what do you know", "what can you answer", "what can i ask",
        "what questions can", "what is available", "what's available", "what data is there",
        "re:\\bque tipos? de (informa\\w+|dados|tabelas|fontes)\\b",
        "re:\\bquais (informa\\w+|dados|tabelas|fontes|bases) (voc[eê]|vc) (tem|possui|acessa|conhece|enxerga)\\b",
        "re:\\b(voc[eê]|vc) tem acesso\\b", "o que você sabe", "o que voce sabe", "o que posso perguntar",
        "re:\\bquais (dados|tabelas|fontes|bases) (existem|est[aã]o dispon[ií]veis|h[aá])\\b",
    ]},
    "count": {"wording": [
        "how many", "number of", "count of", "count the", "count all", "total number of", "re:^\\s*count\\b",
        "quantos", "quantas", "número de", "numero de", "contagem",
    ]},
    "values": {"wording": [
        "different", "distinct", "unique", "diferente", "diferentes", "distinto", "distintos", "distinta",
        "distintas", "único", "únicos", "única", "únicas", "unicos", "unicas",
        "kinds of", "types of", "values of", "sorts of", "tipos de", "valores de", "categorias de",
    ]},
    "count_values": {"wording": []},
    "count_by": {
        "wording": ["per", "for each", "for every", "grouped by", "group by", "broken down by", "breakdown",
                    "split by", "por cada", "para cada", "agrupado por", "agrupados por", "agrupada por",
                    "agrupadas por", "separado por", "separados por"],
        "maybe_wording": ["by", "each", "por", "cada"],
    },
    "lookup": {"wording": [
        "what can i find", "what i can find", "what can we find", "what do we have on", "what do we know about",
        "what do you have on", "what do you know about", "tell me about", "everything about", "all about",
        "details about", "details of", "details on", "information about", "information on", "info on",
        "info about", "investigate", "look up", "lookup",
        "re:\\b(bring|give|get|show|fetch|pull|find)( me| us)?( all| the| any)? (info|information|details|data|everything)\\b",
        "re:\\b(info|information|details|data|everything|anything)\\s+(about|on|for|regarding|related to)\\b",
        "o que tem sobre", "o que temos sobre", "o que há sobre", "o que existe sobre", "o que sabemos sobre",
        "o que eu encontro", "o que encontro", "tudo sobre", "detalhes de", "detalhes do", "detalhes da",
        "detalhes sobre", "informações sobre", "informacoes sobre", "investigue", "investigar",
    ]},
    "locate": {"wording": [
        "re:\\b(which|what|in which|in what) (tables?|sources?|datasets?|systems?|data sources?)\\b",
        "re:\\bwhere (can i|can we|do i|do we|does|did|is|are) (find|see|appear|show)",
        "where does", "where is", "appears in", "shows up in",
        "re:\\b(em )?(quais|que|qual) (tabelas?|fontes?|bases?|sistemas?)\\b",
        "re:\\bonde (aparece|aparecem|existe|existem|est[aá]|est[aã]o|encontr\\w*|tem)\\b",
    ]},
}


class AnswerShapes:
    """The wording of every answer shape, compiled; see the module docstring."""

    def __init__(self, overrides: Optional[Dict[str, Any]] = None):
        wording = {shape: {k: list(v) for k, v in spec.items()} for shape, spec in DEFAULT_WORDING.items()}
        self.descriptions = dict(ANSWER_SHAPES)
        for shape, spec in (overrides or {}).items():
            if shape not in DEFAULT_WORDING:
                raise ValueError(f"answer_shapes: unknown shape {shape!r}; known: {sorted(DEFAULT_WORDING)}")
            spec = spec if isinstance(spec, dict) else {"wording": list(spec)}
            unknown = set(spec) - {"wording", "maybe_wording", "description", "replace"}
            if unknown:
                raise ValueError(f"answer_shapes[{shape!r}]: unknown key(s) {sorted(unknown)}; "
                                 f"use wording, maybe_wording, description, replace")
            if spec.get("maybe_wording") and shape != "count_by":
                raise ValueError(f"answer_shapes[{shape!r}]: maybe_wording is only for count_by")
            for key in ("wording", "maybe_wording"):
                if key in spec:
                    extra = list(spec[key] or [])
                    wording[shape][key] = extra if spec.get("replace") else wording[shape].get(key, []) + extra
            if spec.get("description"):
                self.descriptions[shape] = spec["description"]
        self.wording = wording
        self._compiled: Dict[Tuple[str, str], List[re.Pattern]] = {
            (shape, key): [_compile(p, shape) for p in phrases]
            for shape, spec in wording.items() for key, phrases in spec.items()
        }

    def _find(self, question: str, shape: str, key: str = "wording") -> Optional[re.Match]:
        hits = [m for r in self._compiled.get((shape, key), []) for m in [r.search(question)] if m]
        return min(hits, key=lambda m: m.start()) if hits else None

    def candidates(self, question: str) -> Tuple[List[str], str]:
        """The shapes the wording allows, and the words that say so."""
        found = {s: self._find(question, s) for s in DEFAULT_WORDING}
        maybe = self._find(question, "count_by", "maybe_wording")
        words = ", ".join(f"'{m.group(0).strip()}'" for m in [*found.values(), maybe] if m)
        if found["catalog"]:
            return ["catalog"], words
        if found["locate"]:
            return ["locate"], words
        if found["lookup"]:
            return ["lookup"], words
        count, values, group = found["count"], found["values"], found["count_by"]
        if found["count_values"] or (count and values and not group):
            return ["count_values"], words
        if count and group:
            return ["count_by"], words
        if count:
            return (["count", "count_by"] if maybe else ["count"]), words
        if values:
            return ["values"], words
        if group:
            return ["list", "count_by"], words
        return ["list"], words

    def group_words(self, question: str) -> List[Tuple[int, int]]:
        """Where the count_by wording (sure or maybe) sits — the group is the word right after."""
        spans = []
        for key in ("wording", "maybe_wording"):
            for r in self._compiled.get(("count_by", key), []):
                spans += [(m.start(), m.end()) for m in r.finditer(question)]
        return spans

    def matched_tokens(self, question: str) -> Set[str]:
        """Every word inside matched wording — never a value to filter on."""
        out: Set[str] = set()
        for (shape, key), regexes in self._compiled.items():
            for r in regexes:
                for m in r.finditer(question):
                    out.update(tokenize(m.group(0)))
        return out


def _compile(phrase: str, shape: str) -> re.Pattern:
    try:
        if phrase.startswith("re:"):
            return re.compile(phrase[3:], re.IGNORECASE)
        words = [re.escape(w) for w in phrase.split()]
        return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"answer_shapes[{shape!r}]: bad regex {phrase!r}: {exc}") from exc


def merge_answer_shapes(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Two ``answer_shapes`` override dicts as one: wording lists concatenated, ``extra`` wins otherwise."""
    out = {shape: dict(spec) if isinstance(spec, dict) else {"wording": list(spec)} for shape, spec in base.items()}
    for shape, spec in extra.items():
        spec = spec if isinstance(spec, dict) else {"wording": list(spec)}
        merged = out.setdefault(shape, {})
        for key, value in spec.items():
            if key in ("wording", "maybe_wording"):
                merged[key] = list(merged.get(key, [])) + [v for v in value or [] if v not in merged.get(key, [])]
            else:
                merged[key] = value
    return out


def load_answer_shapes(value: Any, base_dir: str = ".") -> Dict[str, Any]:
    """``semantic.answer_shapes``: the overrides inline, or the path of a JSON/YAML file holding them."""
    if value is None or isinstance(value, dict):
        return dict(value or {})
    import json
    import os

    path = value if os.path.isabs(value) else os.path.join(base_dir, value)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.lower().endswith((".yaml", ".yml")):
        import yaml

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"answer_shapes file {value!r} must hold an object: {{shape: {{wording: [...]}}}}")
    return data


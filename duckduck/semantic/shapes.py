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

from .text import stem, tokenize

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
    "small_talk": "a greeting, thanks or goodbye — nothing to look up",
    "browse": "the rows of the table the question names, every column (\"show me table owners\")",
    "out_of_scope": "nothing about the data in this catalog — a direct reply saying what can be asked instead",
}
#: The shapes that need to know which field they're about.
FIELD_SHAPES = ("values", "count_values", "count_by")
#: The shapes answered across every table holding the question's value (one query per table).
ACROSS_SHAPES = ("lookup", "locate")

DEFAULT_WORDING: Dict[str, Dict[str, List[str]]] = {
    # only when nothing else is asked ("hi, which hosts...?" is a real question) — see small_talk_kind()
    "small_talk": {"wording": [
        "hi", "hello", "hey", "good morning", "good afternoon", "good evening", "howdy", "how are you",
        "thanks", "thank you", "thx", "cheers", "bye", "goodbye", "see you",
        "oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "tudo bem", "tudo bom", "e aí", "e ai",
        "obrigado", "obrigada", "valeu", "brigado", "tchau", "até mais", "ate mais",
    ]},
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
        # the catalog's parts: entities, activities, fields, relationships
        "re:\\bcat[aá]log(o|ue)?\\b",
        "re:\\b(which|what|list( the| all)?|show( me)?( the| all)?) (entities|activities|relationships|joins)\\b",
        "re:\\b(which|what) (fields|columns|attributes)\\b",
        "re:\\bhow are (the )?tables (related|connected|linked|joined)\\b",
        "re:\\b(quais|que|liste|mostre)( as| os| me)? (entidades|atividades|relacionamentos|rela[cç][oõ]es|campos|colunas|atributos)\\b",
        "re:\\bcomo (as )?tabelas se (relacionam|conectam|ligam)\\b",
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
        found = {s: self._find(question, s) for s in DEFAULT_WORDING if s != "small_talk"}
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

    def small_talk_kind(self, question: str) -> Optional[str]:
        """``greeting`` / ``thanks`` / ``goodbye`` when the question is only small talk; else ``None``."""
        from .text import content_stems

        rest, hit = question, False
        for regex in self._compiled.get(("small_talk", "wording"), []):
            if regex.search(rest):
                hit = True
                rest = regex.sub(" ", rest)
        leftover = [w for w in content_stems(re.sub(r"[^\w\s]", " ", rest)) if w not in _SMALL_TALK_FILLER]
        if not hit or leftover:
            return None
        low = question.lower()
        if re.search(r"\b(thanks?|thank you|thx|cheers|obrigad[oa]|valeu|brigad[oa])\b", low):
            return "thanks"
        if re.search(r"\b(bye|goodbye|see you|tchau|at[eé] mais)\b", low):
            return "goodbye"
        return "greeting"

    @staticmethod
    def named_head(question: str) -> Optional[str]:
        """The phrase a "what are the …" / "list the …" / "quais são as …" question asks for, or ``None``."""
        for opener in _HEAD_OPENERS:
            m = opener.match(question)
            if m:
                phrase = _HEAD_END.split(m.group("p"), maxsplit=1)[0].strip()
                return phrase or None
        return None

    @staticmethod
    def asked_head(question: str) -> Optional[str]:
        """What a "which <X> …" / "what <X> …" question asks for (looser than ``named_head``: the caller
        checks the phrase *starts* with a field), or ``None``."""
        m = re.match(r"^\s*(?:and\s+)?(?:which|what)\s+(?!are\b|were\b|is\b|was\b)(?P<p>.+)", question, re.I)
        if not m:
            return None
        return _HEAD_END.split(m.group("p"), maxsplit=1)[0].strip() or None

    @staticmethod
    def counted_head(question: str) -> Optional[str]:
        """What a "how many <X> …" / "number of <X>" / "quantos <X>" question counts, or ``None``."""
        for opener in _COUNT_OPENERS:
            m = opener.search(question)
            if m:
                phrase = _HEAD_END.split(m.group("p"), maxsplit=1)[0].strip()
                return phrase or None
        return None

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


#: "What are the <X> of …", "list the <X>", "quais são as <X>": when <X> names a field (and not an entity),
#: the question asks for that field's values — see ``AnswerShapes.named_head``.
_HEAD_OPENERS = [re.compile(p, re.I) for p in (
    r"^\s*(?:and\s+)?(?:what|which)\s+(?:are|were|is|was)\s+(?:all\s+)?(?:the\s+)?(?P<p>.+)",
    r"^\s*(?:please\s+)?(?:list|show|give|get|display)(?:\s+me)?(?:\s+all)?(?:\s+the)?\s+(?P<p>.+)",
    r"^\s*(?:quais|qual)(?:\s+s[aã]o|\s+[eé])?(?:\s+(?:os|as|o|a))?\s+(?P<p>.+)",
    r"^\s*(?:me\s+)?(?:liste|listar|mostre|mostrar|traga|trazer)(?:\s+me)?(?:\s+(?:todos|todas))?(?:\s+(?:os|as))?\s+(?P<p>.+)",
)]
#: "how many <X> …", "number of <X>", "quantos <X> …": what a count counts.
_COUNT_OPENERS = [re.compile(p, re.I) for p in (
    r"\bhow\s+many\s+(?:different\s+|distinct\s+|unique\s+)?(?:of\s+)?(?:the\s+)?(?P<p>.+)",
    r"\b(?:the\s+)?(?:total\s+)?number\s+of\s+(?:different\s+|distinct\s+|unique\s+)?(?:the\s+)?(?P<p>.+)",
    r"^\s*count\s+(?:of\s+|all\s+)?(?:the\s+)?(?P<p>.+)",
    r"\bquant[oa]s\s+(?:diferentes\s+)?(?P<p>.+)",
    r"\bn[uú]mero\s+de\s+(?P<p>.+)",
)]
#: Where the head phrase ends ("the severities | of the events").
_HEAD_END = re.compile(r"\b(of|for|from|in|on|with|that|which|where|who|by|per|de|do|da|dos|das|em|no|na|nos|nas|"
                       r"para|que|com|por)\b|[?,.;:!]", re.I)


#: "the first 20 rows", "top 5", "10 linhas" — how many rows a ``browse`` question asks for.
_ROW_COUNT = re.compile(r"\b(?:first|top|last|primeir[oa]s|[uú]ltim[oa]s)\s+(\d{1,6})\b|"
                        r"\b(\d{1,6})\s+(?:rows?|lines?|records?|entries|linhas?|registros?)\b", re.I)


def browse_target(question: str, names: Dict[str, str]) -> Optional[Tuple[str, Optional[int]]]:
    """
    The table a "show me table owners" / "the alerts table" / "mostre a tabela
    owners" / "preview owners" question names — ``names`` maps every way to
    write a table (source name, its bound table, with spaces for ``_``;
    lowercase) to its source — and the row count it asked for.
    """
    if not names:
        return None
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    name = rf"[\"'`]?(?P<n>{alts})[\"'`]?"
    patterns = (
        rf"\b(?:table|tabela|dataset)\s+(?:de\s+|do\s+|da\s+|called\s+|named\s+|chamada\s+)?{name}(?![\w-])",
        rf"(?<![\w-]){name}\s+(?:table|tabela|dataset)\b",
        rf"^\s*(?:please\s+|me\s+)?(?:preview|browse|open|peek(?:\s+at)?|sample|abra|abrir|visualize|visualizar)"
        rf"(?:\s+me)?(?:\s+(?:the|a|o))?\s+{name}(?![\w-])",
    )
    for p in patterns:
        m = re.search(p, question, re.I)
        if m:
            count = _ROW_COUNT.search(question)
            rows = int(count.group(1) or count.group(2)) if count else None
            return names[m.group("n").lower()], rows or None
    return None


#: Words that don't turn small talk into a question ("thanks a lot", "valeu pessoal").
_SMALL_TALK_FILLER = {stem(w) for w in (
    "lot", "much", "very", "so", "again", "all", "everyone", "guys", "folks", "team", "friend", "mate", "bot",
    "duckduck", "there", "today", "muito", "mesmo", "pessoal", "gente", "todos", "amigo", "time", "hoje", "ai", "aí",
)}

#: What a ``catalog`` question is about, by its wording (first match wins; default: the tables).
CATALOG_TOPICS = [
    ("relationships", re.compile(r"\b(relationships?|joins?|related|connected|linked|relacionament\w*|rela[cç](ão|ao|ões|oes)"
                                 r"|relacionam|conectam|ligam)\b", re.I)),
    ("fields", re.compile(r"\b(fields?|columns?|attributes?|campos?|colunas?|atributos?)\b", re.I)),
    ("activities", re.compile(r"\b(activit(y|ies)|atividades?)\b", re.I)),
    ("entities", re.compile(r"\b(entit(y|ies)|entidades?|kinds? of things|tipos? de coisas?)\b", re.I)),
]


def catalog_topic(question: str) -> str:
    """``tables`` / ``entities`` / ``activities`` / ``fields`` / ``relationships`` — what a catalog question asks about."""
    return next((topic for topic, regex in CATALOG_TOPICS if regex.search(question)), "tables")


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


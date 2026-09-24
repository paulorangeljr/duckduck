"""
Tiny, dependency-free text helpers shared by the lexical baseline pieces
(retrieval, value extraction, ``LexicalDecisionEngine``).

Deliberately crude: the goal is a deterministic offline baseline good
enough to exercise the whole pipeline, not linguistic accuracy — that's
what JEV / embeddings replace.
"""

import re
from typing import Iterable, List, Set

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Words that never carry semantic evidence on their own. Note "who" is
#: deliberately absent — it's an entity keyword ("who accessed ...").
STOPWORDS: Set[str] = {
    "a", "about", "all", "an", "and", "any", "are", "as", "at", "be", "been",
    "by", "can", "did", "do", "does", "during", "every", "find", "for",
    "from", "generate", "generated", "get", "give", "had", "has", "have",
    "how", "i", "in", "into", "is", "it", "its", "list", "me", "my", "of",
    "on", "or", "our", "over", "please", "show", "that", "the", "their",
    "them", "there", "these", "this", "those", "to", "was", "were", "what",
    "when", "where", "which", "while", "with", "within", "you",
}


def stem(word: str) -> str:
    """
    Minimal suffix stripper so "machines"/"machine", "queried"/"query",
    "connection"/"connected"/"connect" land on the same token.
    """
    w = word.lower()
    if len(w) <= 3:
        return w
    if w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("ies"):
        w = w[:-3] + "i"
    elif w.endswith("s") and not w.endswith(("ss", "us")):
        w = w[:-1]
    for suffix in ("ion", "ing", "ed", "e"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            break
    if w.endswith("y") and len(w) > 3:
        w = w[:-1] + "i"
    return w


def tokenize(text: str) -> List[str]:
    """Lowercased alphanumeric tokens, in order, stopwords included."""
    return _TOKEN_RE.findall(text.lower())


def content_stems(text: str) -> List[str]:
    """Stemmed tokens with stopwords removed, in order."""
    return [stem(t) for t in tokenize(text) if t not in STOPWORDS]


def vocabulary(texts: Iterable[str]) -> Set[str]:
    """Set of stems appearing anywhere in ``texts``."""
    vocab: Set[str] = set()
    for text in texts:
        if text:
            vocab.update(content_stems(text))
    return vocab

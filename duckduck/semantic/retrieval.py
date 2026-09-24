"""
Semantic retrieval — narrows the catalog to the top-N candidate sources
*before* any decision-engine call, so JEV never sees the whole catalog.

``CatalogRetriever`` is the protocol; ``LexicalRetriever`` (BM25 over each
source's catalog text) is the offline baseline. Phase 3's Azure AI Search
/ embeddings / hybrid retriever just implements the same ``search()``.
"""

import math
from collections import Counter
from typing import Dict, List, Protocol, Tuple

from .catalog import Catalog
from .text import content_stems


class CatalogRetriever(Protocol):
    def search(self, query: str, top_k: int) -> List[Tuple[str, float]]:
        """Top ``top_k`` ``(source_name, score)`` pairs, best first."""


class LexicalRetriever:
    """BM25 over each source's description, fields, entities, activities and examples."""

    def __init__(self, catalog: Catalog, k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self._docs: Dict[str, Counter] = {
            name: Counter(content_stems(" ".join(catalog.source_texts(name))))
            for name in catalog.sources
        }
        self._avg_len = (
            sum(sum(d.values()) for d in self._docs.values()) / max(len(self._docs), 1)
        )
        n = len(self._docs)
        df = Counter(term for doc in self._docs.values() for term in doc)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        terms = content_stems(query)
        scored = []
        for name, doc in self._docs.items():
            length = sum(doc.values())
            score = 0.0
            for t in terms:
                tf = doc.get(t, 0)
                if tf:
                    denom = tf + self.k1 * (1 - self.b + self.b * length / self._avg_len)
                    score += self._idf.get(t, 0.0) * tf * (self.k1 + 1) / denom
            scored.append((name, score))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return scored[:top_k]

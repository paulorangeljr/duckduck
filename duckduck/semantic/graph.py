"""
Relationship Graph — how sources connect to each other.

Nodes are sources; an edge is a catalog relationship that means "these two
fields hold the same value" (``same_entity`` / ``references`` /
``parent_child``), i.e. an equality join, traversable in both directions.
``represents`` relationships don't join anything — they say a field
stands for an entity (see ``Catalog.entity_fields``). ``temporal_match``
and ``derived_from`` are kept in the catalog but not traversed yet (a
time-window join isn't an equality join).

Path search is Dijkstra over ``-log(confidence)`` (so the best path is the
one with the highest *product* of edge confidences) plus a small per-hop
penalty so fewer joins win ties — plain Python, no NetworkX needed.
"""

import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .catalog import JOINABLE_RELATIONSHIPS, Catalog, split_ref


@dataclass(frozen=True)
class JoinEdge:
    #: ``source.field`` on the side already in the plan (traversal origin).
    left: str
    #: ``source.field`` on the side being joined in.
    right: str
    type: str
    confidence: float

    @property
    def left_source(self) -> str:
        return split_ref(self.left)[0]

    @property
    def right_source(self) -> str:
        return split_ref(self.right)[0]


@dataclass
class Path:
    start: str
    edges: List[JoinEdge] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        return math.prod(e.confidence for e in self.edges) if self.edges else 1.0

    @property
    def end(self) -> str:
        return self.edges[-1].right_source if self.edges else self.start

    @property
    def sources(self) -> List[str]:
        return [self.start] + [e.right_source for e in self.edges]


class RelationshipGraph:
    HOP_PENALTY = 0.01

    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._adj: Dict[str, List[JoinEdge]] = {name: [] for name in catalog.sources}
        for rel in catalog.relationships:
            if rel.type not in JOINABLE_RELATIONSHIPS:
                continue
            a, b = rel.from_, rel.to
            self._adj[split_ref(a)[0]].append(JoinEdge(a, b, rel.type, rel.confidence))
            self._adj[split_ref(b)[0]].append(JoinEdge(b, a, rel.type, rel.confidence))

    def neighbors(self, source: str) -> List[JoinEdge]:
        return list(self._adj.get(source, []))

    def find_path(
        self,
        start: str,
        goal: Callable[[str], bool],
        max_hops: int = 3,
        allowed: Optional[Callable[[str], bool]] = None,
    ) -> Optional[Path]:
        """
        Best path from ``start`` to the nearest source satisfying ``goal``
        (``start`` itself counts — an empty path). ``allowed`` can exclude
        sources from being traversed (e.g. unauthorized ones).
        """
        counter = itertools.count()
        heap = [(0.0, next(counter), Path(start))]
        best_cost: Dict[str, float] = {}
        while heap:
            cost, _, path = heapq.heappop(heap)
            node = path.end
            if node in best_cost:
                continue
            best_cost[node] = cost
            if goal(node):
                return path
            if len(path.edges) >= max_hops:
                continue
            visited = set(path.sources)
            for edge in self._adj.get(node, []):
                nxt = edge.right_source
                if nxt in visited or nxt in best_cost:
                    continue
                if allowed is not None and not allowed(nxt):
                    continue
                step = -math.log(max(edge.confidence, 1e-9)) + self.HOP_PENALTY
                heapq.heappush(heap, (cost + step, next(counter), Path(start, path.edges + [edge])))
        return None

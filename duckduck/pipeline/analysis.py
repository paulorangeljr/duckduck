"""
What a pipeline's SQL says, read before anything runs: its views (``CREATE
VIEW name AS SELECT …``), what each reads, and where the key goes.

**Views are the steps.** A statement ``CREATE [OR REPLACE] [TEMP] VIEW x AS
…`` (or ``CREATE TABLE x AS …``) is a step named ``x``; later statements read
it by name. A bare ``SELECT`` is exploration — never run by the pipeline —
unless the SQL has no view at all, when the last one is the pipeline's
output. ``DESCRIBE``/``SHOW``/``SUMMARIZE``/``EXPLAIN``/``SET``/``DROP VIEW``
are ignored; anything that writes is refused (writes come from the JSON's
targets only).

**The key, followed by name** (the sip's lineage). Each view's key comes from
its *driving* table — the first table of its FROM (a CTE or subquery is
followed to its own first table):

- a pipeline view with a key → the same key, by name (``SELECT *``, ``t.*``,
  ``sys_id``, or renamed: ``sys_id AS id`` → ``id``);
- anything else (a source outside the pipeline) → the ``primary_key``, and
  the view is a *root*: the sip starts following there;
- ``GROUP BY`` (or ``SELECT DISTINCT`` without the key) → *group*: the key is
  the group columns, and each followed row is traced into its group;
- ``keys.<view>`` in the JSON → those columns, always.

A view on the way to a target whose key can't be followed (it drops the key,
aggregates everything into one row, groups by something it doesn't select)
fails the plan with what to write — nothing is guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import sqlglot
from sqlglot import exp

from .sources import Statement, strip_comments
from .spec import NAME_RE, PipelineError, PipelineSpec, Target

VIEW_PREFIX_RE = re.compile(
    r"^\s*create\s+(?:or\s+replace\s+)?(?:(?:temp|temporary)\s+)?(?:view|table)\s+(?:if\s+not\s+exists\s+)?"
    r"(?:\"(?:[^\"]|\"\")+\"|[^\s(]+)\s+as\s+", re.I)
IGNORED = {"Describe", "Show", "Summarize", "Pragma", "Set", "Use"}
IGNORED_COMMANDS = ("EXPLAIN", "SUMMARIZE", "DESCRIBE", "SHOW")


@dataclass
class KeyInfo:
    mode: str  # "row" | "group" | "none"
    columns: List[str] = field(default_factory=list)  # the key at this view's output
    upstream: Optional[str] = None  # the pipeline view the key comes from; None = a root
    upstream_columns: List[str] = field(default_factory=list)  # the key's names there
    alias: Optional[str] = None  # the upstream's name in this view's FROM (to qualify its columns)
    group_exprs: List[exp.Expression] = field(default_factory=list)  # group: the input-side expressions
    declared: bool = False  # keys.<view> in the JSON
    assumed: bool = False  # came through a *: checked against the view's columns once it exists
    reason: str = ""  # none: why; a root: what it starts from

    @property
    def root(self) -> bool:
        return self.upstream is None

    def describe(self) -> str:
        if self.mode == "none":
            return f"no key — {self.reason}"
        cols = ", ".join(self.columns)
        if self.mode == "group":
            if self.root:
                return f"groups ({cols}) — followed on their own{f': {self.reason}' if self.reason else ''}"
            return f"groups ({cols}) — each followed row of {self.upstream} traced into its group"
        if self.root:
            return f"key {cols} — the sip starts here"
        renamed = "" if [c.lower() for c in self.columns] == [c.lower() for c in self.upstream_columns] \
            else f" (was {', '.join(self.upstream_columns)})"
        return f"key {cols}{renamed} — from {self.upstream}"


@dataclass
class View:
    name: str
    sql: str  # the SELECT that makes it, as written
    origin: str
    ast: exp.Expression
    deps: List[str] = field(default_factory=list)  # pipeline views it reads
    external: List[str] = field(default_factory=list)  # anything else it reads
    driving: Optional[str] = None
    key: KeyInfo = field(default_factory=lambda: KeyInfo("none"))
    synthetic: bool = False  # the pipeline's lone query, not a CREATE VIEW


def parse(sql: str, origin: str) -> exp.Expression:
    try:
        parsed = sqlglot.parse(sql, dialect="duckdb")
    except sqlglot.errors.ParseError as exc:
        raise PipelineError(f"{origin}: can't read this SQL — {str(exc).splitlines()[0]}") from None
    parsed = [p for p in parsed if p is not None]
    if len(parsed) != 1:
        raise PipelineError(f"{origin}: one statement at a time")
    return parsed[0]


def leftmost(query: exp.Expression) -> Optional[exp.Select]:
    """The first SELECT of a query (the left side of a UNION, inside parentheses)."""
    while True:
        if isinstance(query, exp.Select):
            return query
        if isinstance(query, (exp.SetOperation, exp.Subquery)):
            query = query.this
            continue
        return None


def _ctes(query: exp.Expression) -> Dict[str, exp.Expression]:
    out = {}
    for cte in query.find_all(exp.CTE):
        out[cte.alias_or_name.lower()] = cte.this
    return out


def _from_sources(select: exp.Select) -> List[exp.Expression]:
    src = select.args.get("from_") or select.args.get("from")
    out = [src.this] if src is not None else []
    out += [j.this for j in select.args.get("joins") or []]
    return out


def table_name(t: exp.Table) -> str:
    """A table reference as the pipeline names it: a plain view name lowercased, else the reference as written."""
    if isinstance(t.this, exp.Anonymous):
        return t.this.sql(dialect="duckdb")
    if not t.args.get("db") and not t.args.get("catalog"):
        return t.name.lower()
    return ".".join(p.name for p in t.parts)


def _base_table(source: exp.Expression, ctes: Dict[str, exp.Expression], depth: int = 0) -> Optional[str]:
    """The table a FROM source comes from, through CTEs and subqueries (their first table)."""
    if depth > 20:
        return None
    if isinstance(source, exp.Table):
        name = table_name(source)
        if name in ctes:
            inner = leftmost(ctes[name])
            first = _from_sources(inner)[:1] if inner is not None else []
            return _base_table(first[0], ctes, depth + 1) if first else None
        return name
    if isinstance(source, exp.Subquery):
        inner = leftmost(source.this)
        first = _from_sources(inner)[:1] if inner is not None else []
        return _base_table(first[0], ctes, depth + 1) if first else None
    return None


def _has_aggregate(e: exp.Expression) -> bool:
    return any(not a.find_ancestor(exp.Window) for a in e.find_all(exp.AggFunc))


def _is_star(p: exp.Expression) -> bool:
    return isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))


def _excluded(p: exp.Expression, select: exp.Select) -> Set[str]:
    star = p if isinstance(p, exp.Star) else p.this
    names = set()
    for arg in ("except", "except_", "exclude"):
        for e in (star.args.get(arg) or []) + (select.args.get(arg) or []):
            names.add(e.name.lower())
    return names


def _same(a: exp.Expression, b: exp.Expression) -> bool:
    if isinstance(a, exp.Column) and isinstance(b, exp.Column):
        return a.name.lower() == b.name.lower() and (not a.table or not b.table or a.table.lower() == b.table.lower())
    return a.sql(dialect="duckdb").lower() == b.sql(dialect="duckdb").lower()


def follow_columns(select: exp.Select, upstream: List[str], alias: Optional[str]) -> Tuple[List[str], bool, List[str]]:
    """Where columns named ``upstream`` end up in this SELECT's output: (names, through a *?, missing)."""
    out, assumed, missing = [], False, []
    alias = (alias or "").lower()
    for u in upstream:
        hits = []
        through_star = False
        for p in select.expressions:
            inner = p.unalias() if isinstance(p, exp.Alias) else p
            if _is_star(inner):
                table = inner.table.lower() if isinstance(inner, exp.Column) else ""
                if (not table or table == alias) and u.lower() not in _excluded(inner, select):
                    through_star = True
            elif isinstance(inner, exp.Column) and inner.name.lower() == u.lower():
                hits.append((inner.table.lower(), p.alias_or_name))
        if hits:
            exact = [h for h in hits if h[0] in ("", alias)] or hits
            out.append(exact[0][1])
        elif through_star:
            out.append(u)
            assumed = True
        else:
            missing.append(u)
    return out, assumed, missing


def group_columns(select: exp.Select) -> Tuple[Optional[List[exp.Expression]], List[str], str]:
    """A grouping SELECT's group expressions (input side) and their output names; ``(None, [], why)`` when its
    groups can't be told apart in the output."""
    projections = select.expressions
    group = select.args.get("group")
    if group is not None:
        if group.args.get("grouping_sets") or group.args.get("cube") or group.args.get("rollup"):
            return None, [], "GROUPING SETS / CUBE / ROLLUP make several rows per group"
        if group.args.get("all"):
            exprs = [(p.unalias() if isinstance(p, exp.Alias) else p) for p in projections
                     if not _is_star(p) and not _has_aggregate(p)]
        else:
            exprs = []
            aliases = {p.alias.lower(): p.unalias() for p in projections if isinstance(p, exp.Alias)}
            for g in group.expressions:
                if isinstance(g, exp.Literal) and not g.is_string and g.this.isdigit() \
                        and 0 < int(g.this) <= len(projections):
                    p = projections[int(g.this) - 1]
                    exprs.append(p.unalias() if isinstance(p, exp.Alias) else p)
                elif isinstance(g, exp.Column) and not g.table and g.name.lower() in aliases \
                        and not (isinstance(aliases[g.name.lower()], exp.Column)
                                 and aliases[g.name.lower()].name.lower() == g.name.lower()):
                    exprs.append(aliases[g.name.lower()])
                else:
                    exprs.append(g)
    else:  # SELECT DISTINCT a, b: a group of its columns
        exprs = [(p.unalias() if isinstance(p, exp.Alias) else p) for p in projections]
        if any(_is_star(e) for e in exprs):
            return None, [], "SELECT DISTINCT * has no columns to tell its rows apart"
    if not exprs:
        return None, [], "it groups by nothing: every row becomes one"
    names = []
    for e in exprs:
        found = None
        for p in projections:
            inner = p.unalias() if isinstance(p, exp.Alias) else p
            if not _is_star(inner) and _same(inner, e):
                found = p.alias_or_name
                break
        if found is None:
            return None, [], f"it groups by {e.sql(dialect='duckdb')}, which isn't in its SELECT"
        names.append(found)
    return [e.copy() for e in exprs], names, ""


class Analyzer:
    """Reads statements one by one: views (with deps and key), queries (exploration), ignored ones."""

    def __init__(self, primary_key: Optional[List[str]] = None, keys: Optional[Dict[str, List[str]]] = None,
                 redefine: bool = False):
        self.primary_key = primary_key
        self.keys = {k.lower(): v for k, v in (keys or {}).items()}
        self.redefine = redefine  # a notebook session: running a cell again replaces its view
        self.views: Dict[str, View] = {}
        self.queries: List[Tuple[str, str]] = []
        self.ignored: List[Tuple[str, str]] = []

    def add(self, statement: Statement) -> Tuple[str, object]:
        clean = strip_comments(statement.sql).strip()
        tree = parse(statement.sql, statement.origin)
        kind = type(tree).__name__
        if isinstance(tree, exp.Create) and str(tree.args.get("kind") or "").upper() in ("VIEW", "TABLE"):
            return "view", self._view(tree, clean, statement)
        if isinstance(tree, (exp.Select, exp.SetOperation, exp.Subquery)) or (
                isinstance(tree, exp.Query) and leftmost(tree) is not None):
            self.queries.append((statement.sql, statement.origin))
            return "query", statement.sql
        if kind in IGNORED or (isinstance(tree, exp.Drop) and str(tree.args.get("kind") or "").upper() == "VIEW") \
                or (isinstance(tree, exp.Command) and str(tree.this).upper() in IGNORED_COMMANDS):
            self.ignored.append((statement.sql, statement.origin))
            return "ignored", kind
        if isinstance(tree, exp.Command):  # sqlglot couldn't read it: DuckDB's parser says why
            import duckdb

            try:
                duckdb.extract_statements(statement.sql)
            except Exception as exc:  # noqa: BLE001
                raise PipelineError(f"{statement.origin}: can't read this SQL — {str(exc).splitlines()[0]}") from None
            kind = str(tree.this)
        raise PipelineError(f"{statement.origin}: a pipeline only makes views (CREATE VIEW name AS SELECT …); "
                            f"{kind.upper()} isn't allowed — what gets written comes from the JSON's targets")

    def _view(self, tree: exp.Create, clean: str, statement: Statement) -> View:
        target = tree.this
        if isinstance(target, exp.Schema):
            raise PipelineError(f"{statement.origin}: name the columns in the SELECT (… AS name), not after the "
                                "view's name")
        if not isinstance(target, exp.Table) or target.args.get("db") or target.args.get("catalog"):
            raise PipelineError(f"{statement.origin}: a pipeline view has a plain name (CREATE VIEW enriched AS …); "
                                "where it's written comes from the JSON")
        name = target.name.lower()
        if not NAME_RE.match(name):
            raise PipelineError(f"{statement.origin}: {target.name!r} — a view's name is letters, digits and _")
        if name in self.views and not self.redefine:
            raise PipelineError(f"{statement.origin}: view {name} is made twice (also at {self.views[name].origin})")
        query = tree.expression
        if query is None or leftmost(query) is None:
            raise PipelineError(f"{statement.origin}: CREATE VIEW {name} AS needs a SELECT")
        m = VIEW_PREFIX_RE.match(clean)
        sql = clean[m.end():] if m else query.sql(dialect="duckdb")
        view = self.describe(name, sql, statement.origin, query)
        if name in self.views:  # redefined (a notebook cell run again): views after it keep their analysis
            del self.views[name]
        self.views[name] = view
        return view

    def query_view(self, name: str, sql: str, origin: str) -> View:
        """The pipeline's lone query (no CREATE VIEW anywhere) as its output view."""
        view = self.describe(name, sql, origin, parse(sql, origin))
        view.synthetic = True
        self.views[name] = view
        return view

    def describe(self, name: str, sql: str, origin: str, query: exp.Expression) -> View:
        ctes = _ctes(query)
        view = View(name=name, sql=sql, origin=origin, ast=query)
        for t in query.find_all(exp.Table):
            ref = table_name(t)
            if ref in ctes:
                continue
            if ref == name:
                raise PipelineError(f"{origin}: view {name} reads itself")
            if ref in self.views:
                if ref not in view.deps:
                    view.deps.append(ref)
            elif ref not in view.external:
                view.external.append(ref)
        select = leftmost(query)
        sources = _from_sources(select)
        if sources:
            view.driving = _base_table(sources[0], ctes)
        view.key = self._key(view, select, sources[0] if sources else None, isinstance(query, exp.SetOperation))
        return view

    def _key(self, view: View, select: exp.Select, first: Optional[exp.Expression], union: bool) -> KeyInfo:
        up_view = self.views.get(view.driving or "")
        if up_view is not None and up_view.key.mode != "none":
            upstream, up_cols = up_view.name, up_view.key.columns
        else:
            upstream, up_cols = None, self.primary_key or []
        alias = first.alias_or_name if first is not None and hasattr(first, "alias_or_name") else None
        declared = self.keys.get(view.name)
        if declared:
            return KeyInfo("row", columns=list(declared), upstream=upstream, upstream_columns=list(up_cols),
                           alias=alias, declared=True, assumed=True)
        if not up_cols:
            return KeyInfo("none", reason="there's no primary_key to start from")

        grouped = select.args.get("group") is not None
        if not grouped and any(_has_aggregate(p) for p in select.expressions):
            return KeyInfo("none", reason="it aggregates every row into one")
        if not grouped:
            out, assumed, missing = follow_columns(select, up_cols, alias)
            if not missing:
                return KeyInfo("row", columns=out, upstream=upstream, upstream_columns=list(up_cols), alias=alias,
                               assumed=assumed)
            if not select.args.get("distinct") or select.args["distinct"].args.get("on"):
                return KeyInfo("none", reason=f"it doesn't keep {', '.join(missing)}"
                                              f"{f' from {upstream}' if upstream else ''}")
        exprs, names, why = group_columns(select)
        if exprs is None:
            return KeyInfo("none", reason=why)
        info = KeyInfo("group", columns=names, upstream=upstream, upstream_columns=list(up_cols), alias=alias,
                       group_exprs=exprs)
        outside = [t for t in view.external]
        if upstream is None or union or outside:
            info.upstream = None
            info.reason = ("a UNION" if union else f"it reads {', '.join(outside)} directly" if outside
                           else "nothing before it has the key")
        return info


@dataclass
class Plan:
    spec: PipelineSpec
    views: Dict[str, View]
    needed: List[str]  # the views run, in order
    unused: List[str]  # views no target needs (exploration)
    targets: List[Target]
    sampled: List[str]  # the views the sip follows, in order
    keep: Set[str]  # views materialized (the sip reads them more than once)
    queries: List[Tuple[str, str]]
    ignored: List[Tuple[str, str]]

    def key_of(self, target: Target) -> List[str]:
        view = self.views[target.view]
        if target.key:
            return target.key
        if view.key.mode != "none":
            return view.key.columns
        return self.spec.primary_key or []

    def report(self) -> str:
        lines = [f"pipeline {self.spec.name} — {self.spec.engine}, SQL from {self.spec.source} "
                 f"({self.spec.source_value if self.spec.source != 'sql' else 'inline'})"]
        for name in self.needed:
            v = self.views[name]
            tags = []
            if name in self.sampled:
                tags.append("sip")
            if name in self.keep:
                tags.append("kept")
            lines.append(f"  {name}  ({v.origin}){'  [' + ', '.join(tags) + ']' if tags else ''}")
            reads = v.deps + v.external
            if reads:
                lines.append(f"      reads {', '.join(reads)}")
            if self.spec.sip.enabled:
                lines.append(f"      {v.key.describe()}")
            for t in self.targets:
                if t.view == name:
                    key = f" on {', '.join(self.key_of(t))}" if t.mode == "merge" else ""
                    lines.append(f"      writes: {t.describe()}{key}")
        if self.unused:
            lines.append(f"  not run (no target needs them): {', '.join(self.unused)}")
        if self.queries and not any(self.views[n].synthetic for n in self.needed):
            lines.append(f"  {len(self.queries)} exploration query(ies) not run")
        if self.spec.sip.enabled:
            store = self.spec.sip.store or "kept in memory (no sip.store)"
            lines.append(f"  sip: {self.spec.sip.rate:.4%} of keys, at most {self.spec.sip.max_rows} per view"
                         f"{f', watching {len(self.spec.sip.watch)}' if self.spec.sip.watch else ''} → {store}")
        return "\n".join(lines)


def build_plan(spec: PipelineSpec, statements: List[Statement]) -> Plan:
    analyzer = Analyzer(spec.primary_key, spec.keys)
    for s in statements:
        analyzer.add(s)
    views = analyzer.views
    if not views:
        if not analyzer.queries:
            raise PipelineError("the pipeline's SQL has no view (CREATE VIEW name AS SELECT …) and no query")
        sql, origin = analyzer.queries[-1]
        analyzer.query_view(spec.name.lower(), sql, origin)
    names = list(views)

    targets: List[Target] = []
    for t in spec.targets:
        view = t.view or spec.output or names[-1]
        if view not in views:
            raise PipelineError(f"the target names view {view!r}, which the SQL doesn't make (it makes "
                                f"{', '.join(names)})")
        targets.append(Target(**{**t.__dict__, "view": view}))
    if spec.output and spec.output not in views:
        raise PipelineError(f"'output' is {spec.output!r}, which the SQL doesn't make (it makes {', '.join(names)})")
    for k in spec.keys:
        if k not in views:
            raise PipelineError(f"keys.{k}: the SQL makes no view {k} (it makes {', '.join(names)})")

    wanted = [t.view for t in targets] or [spec.output or names[-1]]
    needed_set: Set[str] = set()
    stack = list(wanted)
    while stack:
        n = stack.pop()
        if n not in needed_set:
            needed_set.add(n)
            stack.extend(views[n].deps)
    needed = [n for n in names if n in needed_set]
    unused = [n for n in names if n not in needed_set]

    sampled: List[str] = []
    keep: Set[str] = set()
    if spec.sip.enabled:
        chosen: Set[str] = set()
        for start in wanted:
            n: Optional[str] = start
            while n is not None and n not in chosen:
                chosen.add(n)
                n = views[n].key.upstream if spec.sip.stages == "all" else None
        problems = [f"  {n} ({views[n].origin}): {views[n].key.reason}" for n in names
                    if n in chosen and views[n].key.mode == "none"]
        if problems:
            raise PipelineError("the sip can't follow the key through:\n" + "\n".join(problems) +
                                "\nKeep the key in the SELECT, or say which column(s) are the key: "
                                "\"keys\": {\"<view>\": [\"<column>\"]}")
        sampled = [n for n in names if n in chosen]
        keep = set(sampled)
        for n in sampled:
            v = views[n]
            if v.key.mode == "group" and not v.key.root:
                keep.update(v.deps)  # its trace query reads them again
    for t in targets:
        if t.mode == "merge" and not (t.key or views[t.view].key.mode != "none" or spec.primary_key):
            raise PipelineError(f"merge into {t.where}: view {t.view} has no key — set 'key' on the target")
    return Plan(spec=spec, views=views, needed=needed, unused=unused, targets=targets, sampled=sampled,
                keep=keep, queries=analyzer.queries, ignored=analyzer.ignored)

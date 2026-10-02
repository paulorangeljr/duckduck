"""
Where a pipeline's SQL comes from: a Jupyter notebook (its ``%%sql``
cells), a ``.sql`` file, or the query written in the pipeline file. All
three become the same list of statements.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, List, Tuple

from .spec import PipelineError, PipelineSpec

CELL_MAGICS = ("%%sql", "%%duckduck")


@dataclass
class Statement:
    sql: str
    origin: str  # "cell 3", "line 12", "sql statement 2" — for messages


def split_sql(text: str) -> List[Tuple[str, int]]:
    """Statements split on ``;`` outside quotes and comments, with the line each starts on."""
    parts: List[Tuple[str, int]] = []
    buf: List[str] = []
    line, start = 1, None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if text[j] == ch:
                    if j + 1 < n and text[j + 1] == ch:  # '' inside a string
                        j += 2
                        continue
                    break
                j += 1
            chunk = text[i:j + 1]
        elif text.startswith("--", i):
            j = text.find("\n", i)
            chunk = text[i:] if j < 0 else text[i:j]
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            chunk = text[i:] if j < 0 else text[i:j + 2]
        elif ch == ";":
            parts.append(("".join(buf), start or line))
            buf, start = [], None
            i += 1
            continue
        else:
            chunk = ch
        if start is None and chunk.strip() and not chunk.startswith(("--", "/*")):
            start = line + chunk[: len(chunk) - len(chunk.lstrip())].count("\n")
        buf.append(chunk)
        line += chunk.count("\n")
        i += len(chunk)
    parts.append(("".join(buf), start or line))
    return [(p, s) for p, s in parts if strip_comments(p).strip()]


def strip_comments(text: str) -> str:
    """The statement without ``--`` and ``/* */`` comments (strings untouched)."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            j = text.find(ch, i + 1)
            j = n - 1 if j < 0 else j
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def cell_sql(source: Any) -> Any:
    """A notebook cell's SQL when it's a ``%%sql`` / ``%%duckduck`` cell, else None."""
    text = "".join(source) if isinstance(source, list) else str(source or "")
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None
    first = lines[0].strip()
    if not any(first == m or first.startswith(m + " ") for m in CELL_MAGICS):
        return None
    return "\n".join(lines[1:])


def notebook_statements(path: str) -> List[Statement]:
    try:
        with open(path, encoding="utf-8") as f:
            nb = json.load(f)
    except FileNotFoundError:
        raise PipelineError(f"no notebook at {path}") from None
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{path} isn't a notebook (.ipynb JSON): {exc}") from None
    out: List[Statement] = []
    for number, cell in enumerate(nb.get("cells") or [], start=1):
        if cell.get("cell_type") != "code":
            continue
        sql = cell_sql(cell.get("source"))
        if sql is None:
            continue
        for text, _line in split_sql(sql):
            out.append(Statement(text, f"cell {number}"))
    return out


def file_statements(path: str) -> List[Statement]:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise PipelineError(f"no SQL file at {path}") from None
    name = os.path.basename(path)
    return [Statement(s, f"{name} line {line}") for s, line in split_sql(text)]


def inline_statements(value: Any) -> List[Statement]:
    texts = [value] if isinstance(value, str) else list(value)
    out: List[Statement] = []
    for text in texts:
        for s, _line in split_sql(text):
            out.append(Statement(s, f"sql statement {len(out) + 1}"))
    return out


def statements_of(spec: PipelineSpec) -> List[Statement]:
    """The pipeline's statements, in order, before parameters are filled in."""
    if spec.source == "notebook":
        found = notebook_statements(spec.resolve(spec.source_value))
        if not found:
            raise PipelineError(f"{spec.source_value} has no %%sql cells — a pipeline cell starts with %%sql")
        return found
    if spec.source == "sql_file":
        found = file_statements(spec.resolve(spec.source_value))
    else:
        found = inline_statements(spec.source_value)
    if not found:
        raise PipelineError("the pipeline's SQL is empty")
    return found


# ---------------------------------------------------------------------------
# A query's top-level WITH, as steps
# ---------------------------------------------------------------------------


def _skip(text: str, i: int) -> int:
    """Past spaces and comments."""
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    return i


def _word(text: str, i: int):
    """An identifier (plain or "quoted") at ``i``: (name, end) or (None, i)."""
    if i < len(text) and text[i] == '"':
        j = i + 1
        while j < len(text):
            if text[j] == '"':
                if text.startswith('""', j):
                    j += 2
                    continue
                return text[i + 1:j].replace('""', '"'), j + 1
            j += 1
        return None, i
    j = i
    while j < len(text) and (text[j].isalnum() or text[j] == "_"):
        j += 1
    return (text[i:j], j) if j > i else (None, i)


def _closing(text: str, i: int) -> int:
    """The index just past the ``)`` matching the ``(`` at ``i`` (strings and comments skipped); -1 if none."""
    depth, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n and not (text[j] == ch and not text.startswith(ch * 2, j)):
                j += 2 if text.startswith(ch * 2, j) else 1
            i = j + 1
            continue
        if text.startswith("--", i) or text.startswith("/*", i):
            i = _skip(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def split_ctes(sql: str):
    """``WITH a AS (…), b AS (…) SELECT …`` → ``([(a, …), (b, …)], "SELECT …")``, the text as written; None when
    the query has no top-level WITH, or one that can't be steps (RECURSIVE, a column list)."""
    i = _skip(sql, 0)
    word, j = _word(sql, i)
    if not word or word.lower() != "with":
        return None
    i = _skip(sql, j)
    ctes = []
    while True:
        name, j = _word(sql, i)
        if not name or name.lower() == "recursive":
            return None
        i = _skip(sql, j)
        if i < len(sql) and sql[i] == "(":
            return None  # WITH x(a, b) AS …: column names the step would need
        kw, j = _word(sql, i)
        if not kw or kw.lower() != "as":
            return None
        i = _skip(sql, j)
        for modifier in (("not", "materialized"), ("materialized",)):
            k, w = i, True
            for m in modifier:
                found, e = _word(sql, _skip(sql, k))
                if not found or found.lower() != m:
                    w = False
                    break
                k = e
            if w:
                i = _skip(sql, k)
                break
        if i >= len(sql) or sql[i] != "(":
            return None
        end = _closing(sql, i)
        if end < 0:
            return None
        ctes.append((name, sql[i + 1:end - 1].strip()))
        i = _skip(sql, end)
        if i < len(sql) and sql[i] == ",":
            i = _skip(sql, i + 1)
            continue
        break
    rest = sql[i:].strip()
    return (ctes, rest) if ctes and rest else None


_CLAUSES = ("group", "having", "qualify", "window", "order", "limit", "offset", "union", "except", "intersect")


def _top_level_words(sql: str):
    """(word lowercased, start, end) for each bare word outside strings, comments and parentheses."""
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n and not (sql[j] == ch and not sql.startswith(ch * 2, j)):
                j += 2 if sql.startswith(ch * 2, j) else 1
            i = j + 1
        elif sql.startswith("--", i) or sql.startswith("/*", i):
            i = _skip(sql, i)
        elif ch == "(":
            end = _closing(sql, i)
            i = n if end < 0 else end
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            yield sql[i:j].lower(), i, j
            i = j
        else:
            i += 1


def add_to_select(sql: str, expression: str) -> str:
    """The query with ``expression`` added to its (first) SELECT's list, right before its FROM — the text
    otherwise as written."""
    body = sql.rstrip().rstrip(";").rstrip()
    words = list(_top_level_words(body))
    start = next((e for w, s, e in words if w == "select"), None)
    at = next((s for w, s, _ in words if w == "from" and start is not None and s > start), None)
    if at is None:
        return sql
    head = body[:at].rstrip()
    return f"{head},\n  {expression}\n{body[at:]}"


def inject_where(sql: str, condition: str) -> str:
    """The query with ``condition`` ANDed into its (first) SELECT's WHERE — the text otherwise as written."""
    body = sql.rstrip().rstrip(";").rstrip()
    words = list(_top_level_words(body))
    where = next(((s, e) for w, s, e in words if w == "where"), None)
    if where is not None:
        end = next((s for w, s, _ in words if s > where[1] and w in _CLAUSES), len(body))
        existing = body[where[1]:end].strip()
        # new lines around what was written: a trailing -- comment can't swallow what follows
        return f"{body[:where[1]]} {condition} AND (\n{existing}\n)\n{body[end:]}".rstrip()
    at = next((s for w, s, _ in words if w in _CLAUSES), len(body))
    return f"{body[:at].rstrip()}\nWHERE {condition}\n{body[at:]}".rstrip()

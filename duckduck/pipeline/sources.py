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

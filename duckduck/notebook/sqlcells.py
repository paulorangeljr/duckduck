"""
SQL cells in a Glue PySpark notebook: what the duckduck kernel sends the session for a ``%%sql`` cell, and how the
answer comes back as a table.

A cell made SQL by the notebook toolbar's PySpark / SQL switch holds only its SQL; its metadata says so
(``"duckduck": {"language": "sql", "engine": "spark" | "duckduck", "name": "df"}``, sent with each execution, read by
``from_metadata``). Written by hand, ``%%sql [name] [--duckduck] [--show N]`` on the first line does the same. It
runs in the session as

    name = spark.sql(<the cell's SQL>)        # --duckduck: reader.sql(…), through duckduck's connectors
    _sqldf = name                             # the last SQL cell's DataFrame, whatever its name

so the next PySpark cell uses ``name`` (``df`` when none is given) as a DataFrame. The other way round, a DataFrame
variable named in the SQL (``SELECT * FROM assets`` with ``assets`` a DataFrame in the session) becomes a temporary
view of that name first — so a PySpark cell's result is a table for the next SQL cell. A variable shadows an
unqualified table of the same name; ``database.table`` still reaches the table.

The session's answer is printed between ``MARK`` and ``END`` (columns, types, the first ``show`` rows as JSON),
and the kernel shows it as an HTML table (``split_output`` / ``render``). Only this module's ``session_code`` runs
in the session, and it needs nothing there but PySpark (and ``reader`` for ``--duckduck``).
"""

import html
import json
import re
from typing import Any, Dict, List, Optional, Tuple

HEADER = "%%sql"
DEFAULT_NAME = "df"
DEFAULT_SHOW = 100
MARK = "__DUCKDUCK_SQL_RESULT__"
END = "__DUCKDUCK_SQL_END__"
_NAME = re.compile(r"^[A-Za-z_]\w*$")
_RESULT = re.compile(re.escape(MARK) + r"(.*?)" + re.escape(END) + r"\n?", re.S)


class SqlCellError(ValueError):
    """A ``%%sql`` header that can't be read — the message says how to write it."""


def parse_header(line: str) -> Dict[str, Any]:
    """``%%sql [name] [--duckduck] [--show N]`` → ``{name, engine, show}``."""
    words = line.strip()[len(HEADER):].split()
    out: Dict[str, Any] = {"name": DEFAULT_NAME, "engine": "spark", "show": DEFAULT_SHOW}
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--duckduck":
            out["engine"] = "duckduck"
        elif word == "--show":
            if i + 1 >= len(words) or not words[i + 1].isdigit():
                raise SqlCellError("--show takes a number of rows: %%sql df --show 20 (0: show nothing)")
            out["show"] = int(words[i + 1])
            i += 1
        elif _NAME.match(word) and i == 0:
            out["name"] = word
        else:
            raise SqlCellError(f"%%sql doesn't take {word!r} — it's %%sql [name] [--duckduck] [--show N]")
        i += 1
    if out["name"].startswith("__"):
        raise SqlCellError("the DataFrame's name can't start with __")
    return out


def split_cell(lines: List[str]) -> Tuple[str, str]:
    """A cell's lines → (its ``%%sql`` header, the SQL after it). Comments and blank lines may come first."""
    for i, line in enumerate(lines):
        if line.startswith(HEADER):
            return line, "\n".join(lines[i + 1:])
        if line.strip() and not line.lstrip().startswith("#"):
            break
    raise SqlCellError("an SQL cell starts with %%sql")


_SESSION_HELPER = '''
def __duckduck_sql(query, name, engine, show):
    import json as _json, re as _re
    from pyspark.sql import DataFrame as _DataFrame
    scope = globals()
    views = []
    if engine == "duckduck":
        if "reader" not in scope:
            raise NameError("reader isn't defined: run the cell with connect(...) first")
        result = scope["reader"].sql(query)
    else:
        for var, value in list(scope.items()):
            if (isinstance(value, _DataFrame) and not var.startswith("_")
                    and _re.search(r"(?<![\\w.`])" + _re.escape(var) + r"(?![\\w`])", query)):
                value.createOrReplaceTempView(var)
                views.append(var)
        result = scope["spark"].sql(query)
    scope[name] = result
    scope["_sqldf"] = result
    columns = [[f.name, f.dataType.simpleString()] for f in result.schema.fields]
    rows = []
    if show and columns:
        def cell(v):
            if v is None or isinstance(v, (bool, int, float)):
                return v
            if hasattr(v, "asDict"):
                v = v.asDict(recursive=True)
            if isinstance(v, (dict, list, tuple)):
                return _json.dumps(v, default=str)[:2000]
            return str(v)[:2000]
        rows = [[cell(v) for v in r] for r in result.limit(show + 1).collect()]
    print("{mark}" + _json.dumps({{"name": name, "engine": engine, "columns": columns, "rows": rows[:show],
                                   "more": len(rows) > show, "show": show, "views": views}}) + "{end}")
'''.format(mark=MARK, end=END)


def session_code(header: str, sql: str) -> str:
    """The Python the session runs for one ``%%sql`` cell (self-contained: PySpark and nothing else)."""
    options = parse_header(header)
    query = sql.strip().rstrip(";").strip()
    if not query:
        raise SqlCellError("the cell has no SQL after %%sql")
    return (_SESSION_HELPER
            + f"__duckduck_sql({query!r}, {options['name']!r}, {options['engine']!r}, {options['show']!r})\n")


def split_output(text: str) -> List[Tuple[str, Any]]:
    """The session's printed output → ``[("text", str) | ("result", dict)]``, in order (text pieces may be empty)."""
    out: List[Tuple[str, Any]] = []
    at = 0
    for match in _RESULT.finditer(text):
        out.append(("text", text[at:match.start()]))
        try:
            out.append(("result", json.loads(match.group(1))))
        except ValueError:
            out.append(("text", match.group(0)))
        at = match.end()
    out.append(("text", text[at:]))
    return out


def _value(v: Any) -> str:
    if v is None:
        return '<span class="duckduck-null">null</span>'
    return html.escape(str(v))


def render(result: Dict[str, Any]) -> Dict[str, str]:
    """One SQL cell's answer → ``{"text/html": …, "text/plain": …}``."""
    name, columns, rows = result.get("name", DEFAULT_NAME), result.get("columns") or [], result.get("rows") or []
    via = "reader.sql" if result.get("engine") == "duckduck" else "spark.sql"
    note = [f"<code>{html.escape(name)}</code> = {via}(…)" + ("" if name == "_sqldf" else " · also <code>_sqldf</code>")]
    if result.get("views"):
        note.append("temp views from DataFrames: " + ", ".join(f"<code>{html.escape(v)}</code>" for v in result["views"]))
    if not columns:
        note.append("no columns (a statement, not a query)")
    elif result.get("show"):
        note.append(f"first {len(rows)} rows" if result.get("more") else f"{len(rows)} row{'s' if len(rows) != 1 else ''}")
    head = "".join(f'<th title="{html.escape(t)}">{html.escape(c)}<div class="duckduck-type">{html.escape(t)}</div></th>'
                   for c, t in columns)
    body = "".join("<tr>" + "".join(f"<td>{_value(v)}</td>" for v in r) + "</tr>" for r in rows)
    style = ("<style>.duckduck-sqlres{font-size:12px}.duckduck-sqlres .duckduck-note{color:var(--jp-ui-font-color2);"
             "margin:2px 0 6px}.duckduck-sqlres table{border-collapse:collapse}.duckduck-sqlres th,.duckduck-sqlres td"
             "{border-bottom:1px solid var(--jp-border-color2);padding:3px 10px;text-align:left;white-space:nowrap;"
             "max-width:420px;overflow:hidden;text-overflow:ellipsis}.duckduck-type{font-weight:normal;"
             "color:var(--jp-ui-font-color2);font-size:11px}.duckduck-null{color:var(--jp-ui-font-color3);"
             "font-style:italic}.duckduck-scroll{max-height:420px;overflow:auto}</style>")
    table = (f'<div class="duckduck-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
             if columns and result.get("show") else "")
    markup = f'{style}<div class="duckduck-sqlres"><div class="duckduck-note">{" · ".join(note)}</div>{table}</div>'
    plain = [f"{name} = {via}(…)", "\t".join(c for c, _ in columns)]
    plain += ["\t".join("null" if v is None else str(v) for v in r) for r in rows]
    return {"text/html": markup, "text/plain": "\n".join(plain)}


#: the cell metadata key the notebook's PySpark / SQL switch writes: {"language": "sql", "engine", "name"}
METADATA_KEY = "duckduck"


def from_metadata(code: str, cell_meta: Optional[Dict[str, Any]]) -> str:
    """A cell made SQL by its metadata (the switch keeps only the SQL in the cell) → the cell as ``%%sql`` code;
    anything else as it is. JupyterLab sends a cell's metadata with its execution (``cell_meta``)."""
    options = (cell_meta or {}).get(METADATA_KEY)
    if not isinstance(options, dict) or options.get("language") != "sql":
        return code
    lines = code.splitlines()
    if any(line.startswith(HEADER) for line in lines[:1]):
        return code  # written as %%sql already
    name = options.get("name") if isinstance(options.get("name"), str) and _NAME.match(options["name"]) else None
    header = header_for(name, "duckduck" if options.get("engine") == "duckduck" else "spark")
    if isinstance(options.get("show"), int) and options["show"] != DEFAULT_SHOW:
        header += f" --show {options['show']}"
    return header + "\n" + code


def header_for(name: Optional[str] = None, engine: str = "spark") -> str:
    """The ``%%sql`` line for a DataFrame name and an engine (``spark`` | ``duckduck``)."""
    parts = [HEADER] + ([name] if name and name != DEFAULT_NAME else [])
    if engine == "duckduck":
        parts.append("--duckduck")
    return " ".join(parts)


__all__ = ["DEFAULT_NAME", "END", "HEADER", "MARK", "METADATA_KEY", "SqlCellError", "from_metadata", "header_for",
           "parse_header", "render", "session_code", "split_cell", "split_output"]

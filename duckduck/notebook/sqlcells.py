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
    via = {"duckduck": "reader.sql", "duckdb": "duck.sql"}.get(result.get("engine"), "spark.sql")
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
             "font-style:italic}.duckduck-scroll{max-height:420px;overflow:auto}"
             ".duckduck-bar{display:flex;gap:6px;margin:2px 0 6px}.duckduck-bar button{font:inherit;font-size:11px;"
             "padding:2px 8px;cursor:pointer;border:1px solid var(--jp-border-color2);border-radius:4px;"
             "background:var(--jp-layout-color1);color:var(--jp-ui-font-color1)}.duckduck-bar button:hover"
             "{border-color:var(--jp-brand-color1)}</style>")
    table = (f'<div class="duckduck-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
             if columns and result.get("show") else "")
    # the result data for the Expand modal / CSV / Excel buttons (the viewer script injects the toolbar). Only the
    # shown rows travel here (the full DataFrame lives in the kernel) — the buttons say so when there are more.
    extras = ""
    if columns and rows:
        data = {"name": name, "columns": columns, "rows": rows, "more": bool(result.get("more"))}
        data_json = json.dumps(data, default=str).replace("<", "\\u003c")
        extras = f'<script type="application/json" class="duckduck-rows">{data_json}</script><script>{_VIEWER_JS}</script>'
    markup = f'{style}<div class="duckduck-sqlres"><div class="duckduck-note">{" · ".join(note)}</div>{table}{extras}</div>'
    plain = [f"{name} = {via}(…)", "\t".join(c for c, _ in columns)]
    plain += ["\t".join("null" if v is None else str(v) for v in r) for r in rows]
    return {"text/html": markup, "text/plain": "\n".join(plain)}


# The Expand modal + CSV/Excel download, injected into a result's output by JupyterLab when the notebook is trusted
# (it evaluates <script> in trusted HTML outputs; untrusted → the plain table stays, the toolbar simply never
# appears). Self-contained, defensive, finds its own result via document.currentScript. Downloads cover the rows
# shown in the output (the full DataFrame lives in the kernel: `df.to_csv(...)` for everything). Excel is an
# HTML-table .xls (opens in Excel, no dependency). Theming uses JupyterLab's own CSS variables.
_VIEWER_JS = r"""
(function(){try{
var s=document.currentScript,box=s&&s.closest?s.closest('.duckduck-sqlres'):null;
if(!box){var a=document.querySelectorAll('.duckduck-sqlres:not([data-ddwired])');box=a[a.length-1];}
if(!box||box.getAttribute('data-ddwired'))return;box.setAttribute('data-ddwired','1');
var d=box.querySelector('script.duckduck-rows');if(!d)return;var data=JSON.parse(d.textContent);
var cols=(data.columns||[]).map(function(c){return c[0];}),rows=data.rows||[],name=data.name||'result';
if(!cols.length)return;
function h(v){return v===null||v===undefined?'':String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function dl(t,m,f){var b=new Blob([t],{type:m}),u=URL.createObjectURL(b),a=document.createElement('a');
a.href=u;a.download=f;document.body.appendChild(a);a.click();document.body.removeChild(a);
setTimeout(function(){URL.revokeObjectURL(u);},1000);}
function btn(l){var b=document.createElement('button');b.type='button';b.textContent=l;return b;}
var bar=document.createElement('div');bar.className='duckduck-bar';
var more=data.more?' (shown rows only — df.to_csv(...) for all)':'';
var eExp=btn('⤢ Expand'),eCsv=btn('⬇ CSV'),eXls=btn('⬇ Excel');
eCsv.title=eXls.title='Download the '+rows.length+' shown row'+(rows.length===1?'':'s')+more;
bar.appendChild(eExp);bar.appendChild(eCsv);bar.appendChild(eXls);box.insertBefore(bar,box.firstChild);
function cell(v){if(v===null||v===undefined)return '';v=String(v);return /[",\n\r]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}
eCsv.onclick=function(){var o=[cols.map(cell).join(',')];rows.forEach(function(r){o.push(r.map(cell).join(','));});
dl('﻿'+o.join('\r\n'),'text/csv;charset=utf-8',name+'.csv');};
eXls.onclick=function(){var t='<table><thead><tr>'+cols.map(function(c){return '<th>'+h(c)+'</th>';}).join('')+
'</tr></thead><tbody>'+rows.map(function(r){return '<tr>'+r.map(function(v){return '<td>'+h(v)+'</td>';}).join('')+
'</tr>';}).join('')+'</tbody></table>';
dl('<html><head><meta charset="utf-8"></head><body>'+t+'</body></html>','application/vnd.ms-excel',name+'.xls');};
eExp.onclick=function(){
var ov=document.createElement('div');
ov.style.cssText='position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.45);display:flex;'+
'align-items:center;justify-content:center;padding:24px';
var md=document.createElement('div');
md.style.cssText='background:var(--jp-layout-color1);color:var(--jp-ui-font-color1);border-radius:8px;'+
'max-width:95vw;max-height:90vh;min-width:320px;display:flex;flex-direction:column;box-shadow:0 10px 40px rgba(0,0,0,.4)';
var hd=document.createElement('div');
hd.style.cssText='display:flex;gap:8px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--jp-border-color2)';
var tt=document.createElement('strong');tt.style.cssText='flex:1;font-size:13px';
tt.textContent=name+' — '+rows.length+' row'+(rows.length===1?'':'s')+(data.more?' (shown)':'');
var q=document.createElement('input');q.type='search';q.placeholder='Filter';
q.style.cssText='font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--jp-border-color2);'+
'border-radius:4px;background:var(--jp-layout-color0);color:var(--jp-ui-font-color1)';
var cx=btn('✕ Close');cx.className='';cx.style.cssText='font:inherit;font-size:12px;padding:3px 8px;cursor:pointer;'+
'border:1px solid var(--jp-border-color2);border-radius:4px;background:var(--jp-layout-color1);color:var(--jp-ui-font-color1)';
hd.appendChild(tt);hd.appendChild(q);hd.appendChild(cx);
var sc=document.createElement('div');sc.style.cssText='overflow:auto;padding:0 12px 12px';
var tb=document.createElement('table');tb.style.cssText='border-collapse:collapse;font-size:12px;width:100%';
sc.appendChild(tb);md.appendChild(hd);md.appendChild(sc);ov.appendChild(md);
function draw(f){f=(f||'').toLowerCase();var b=rows;
if(f)b=rows.filter(function(r){return r.some(function(v){return v!==null&&v!==undefined&&String(v).toLowerCase().indexOf(f)>=0;});});
tb.innerHTML='<thead><tr>'+cols.map(function(c){return '<th style="position:sticky;top:0;background:var(--jp-layout-color1);'+
'border-bottom:1px solid var(--jp-border-color1);padding:4px 10px;text-align:left;white-space:nowrap">'+h(c)+'</th>';}).join('')+
'</tr></thead><tbody>'+b.map(function(r){return '<tr>'+r.map(function(v){return '<td style="border-bottom:1px solid '+
'var(--jp-border-color2);padding:3px 10px;white-space:nowrap;max-width:480px;overflow:hidden;text-overflow:ellipsis">'+
(v===null||v===undefined?'<span style="opacity:.5;font-style:italic">null</span>':h(v))+'</td>';}).join('')+'</tr>';}).join('')+
'</tbody>';}
draw('');q.addEventListener('input',function(){draw(q.value);});
function close(){document.removeEventListener('keydown',onkey);ov.remove();}
function onkey(e){if(e.key==='Escape')close();}
cx.onclick=close;ov.addEventListener('click',function(e){if(e.target===ov)close();});
document.addEventListener('keydown',onkey);document.body.appendChild(ov);q.focus();};
}catch(e){}})();
"""


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


def _frame_cell(value: Any) -> Any:
    """One pandas value coerced for the result JSON: scalars kept, NaN/NaT → None, dict/list/array → JSON text."""
    import json as _json

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and value != value else value  # NaN
    if isinstance(value, str):
        return value[:2000]
    try:
        import numpy as _np

        if isinstance(value, _np.generic):
            value = value.item()
            if isinstance(value, float) and value != value:
                return None
            if isinstance(value, (bool, int, float)):
                return value
            if isinstance(value, str):
                return value[:2000]
        elif isinstance(value, _np.ndarray):
            value = value.tolist()
    except Exception:  # noqa: BLE001 — numpy optional / an odd value
        pass
    if isinstance(value, (dict, list, tuple)):
        return _json.dumps(value, default=str)[:2000]
    try:
        import pandas as _pd

        if _pd.isna(value):  # a scalar NaT / NA
            return None
    except Exception:  # noqa: BLE001 — not a scalar pandas can test, or pandas missing
        pass
    return str(value)[:2000]


def frame_result(frame: Any, options: Dict[str, Any]) -> Dict[str, Any]:
    """A pandas DataFrame + a parsed ``%%sql`` header → the dict ``render`` shows. This is the local DuckDB
    kernel's counterpart of the Spark session's printed ``MARK…END`` (``engine`` ``"duckdb"`` → ``duck.sql``)."""
    name = options.get("name") or DEFAULT_NAME
    show = int(options.get("show", DEFAULT_SHOW))
    columns = [[str(c), str(dt)] for c, dt in zip(list(frame.columns), list(frame.dtypes))]
    rows: List[List[Any]] = []
    if show and columns:
        head = frame.head(show + 1)
        rows = [[_frame_cell(v) for v in row] for row in head.itertuples(index=False, name=None)]
    return {"name": name, "engine": "duckdb", "columns": columns, "rows": rows[:show],
            "more": len(rows) > show, "show": show, "views": list(options.get("views") or [])}


__all__ = ["DEFAULT_NAME", "END", "HEADER", "MARK", "METADATA_KEY", "SqlCellError", "frame_result", "from_metadata",
           "header_for", "parse_header", "render", "session_code", "split_cell", "split_output"]

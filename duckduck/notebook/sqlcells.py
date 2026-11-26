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
    if result.get("plain"):
        # a pandas DataFrame shown in the kernel (not an SQL cell): a neutral note, no "= duck.sql(…)".
        total = int(result.get("total", len(rows)))
        ncols = len(columns)
        note = [f"{total:,} row{'s' if total != 1 else ''} × {ncols} column{'s' if ncols != 1 else ''}"
                + (f" · first {len(rows)} shown" if result.get("more") else "")]
    else:
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
    plain_head = note[0] if result.get("plain") else f"{name} = {via}(…)"
    plain = [plain_head, "\t".join(c for c, _ in columns)]
    plain += ["\t".join("null" if v is None else str(v) for v in r) for r in rows]
    return {"text/html": markup, "text/plain": "\n".join(plain)}


# The result toolbar injected into a cell's output: Expand (a searchable modal), CSV, Excel, and a visual Chart
# builder (pick a type + X/Y columns → it draws the chart in plain inline SVG, no dependency, works offline).
# JupyterLab evaluates <script> only in trusted HTML outputs, so an untrusted notebook just shows the plain table
# (the toolbar never appears). Everything works off the rows shown in the output (the full DataFrame lives in the
# kernel: df.to_csv(...) for everything). Excel is an HTML-table .xls (opens in Excel, no dependency). Theming uses
# JupyterLab's own CSS variables. Self-contained and defensive; finds its own result via document.currentScript.
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
function btn(l){var b=document.createElement('button');b.type='button';b.textContent=l;
b.style.cssText='font:inherit;font-size:11px;padding:2px 8px;cursor:pointer;border:1px solid var(--jp-border-color2);'+
'border-radius:4px;background:var(--jp-layout-color1);color:var(--jp-ui-font-color1)';return b;}
function numCol(i){var any=false;for(var k=0;k<rows.length;k++){var v=rows[k][i];
if(v===null||v===undefined||v==='')continue;if(isNaN(Number(v)))return false;any=true;}return any;}
var bar=document.createElement('div');bar.className='duckduck-bar';
var more=data.more?' (shown rows only — df.to_csv(...) for all)':'';
var eExp=btn('⤢ Expand'),eChart=btn('▦ Chart'),eCsv=btn('⬇ CSV'),eXls=btn('⬇ Excel');
var eEdit=window.__duckduckEditGrid?btn('✎ Edit'):null;  // only in the embedded JupyterLab extension
eCsv.title=eXls.title='Download the '+rows.length+' shown row'+(rows.length===1?'':'s')+more;
bar.appendChild(eExp);bar.appendChild(eChart);if(eEdit)bar.appendChild(eEdit);bar.appendChild(eCsv);bar.appendChild(eXls);
box.insertBefore(bar,box.firstChild);
if(eEdit){eEdit.title='Edit these rows into a new DataFrame';
eEdit.onclick=function(){window.__duckduckEditGrid({name:name,columns:cols,rows:rows});};}
function cell(v){if(v===null||v===undefined)return '';v=String(v);return /[",\n\r]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}
eCsv.onclick=function(){var o=[cols.map(cell).join(',')];rows.forEach(function(r){o.push(r.map(cell).join(','));});
dl('﻿'+o.join('\r\n'),'text/csv;charset=utf-8',name+'.csv');};
eXls.onclick=function(){var t='<table><thead><tr>'+cols.map(function(c){return '<th>'+h(c)+'</th>';}).join('')+
'</tr></thead><tbody>'+rows.map(function(r){return '<tr>'+r.map(function(v){return '<td>'+h(v)+'</td>';}).join('')+
'</tr>';}).join('')+'</tbody></table>';
dl('<html><head><meta charset="utf-8"></head><body>'+t+'</body></html>','application/vnd.ms-excel',name+'.xls');};
// a modal shell (overlay + card + header with a Close button); the caller fills the header and the body
function modal(titleText){
var ov=document.createElement('div');
ov.style.cssText='position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.45);display:flex;'+
'align-items:center;justify-content:center;padding:24px';
var md=document.createElement('div');
md.style.cssText='background:var(--jp-layout-color1);color:var(--jp-ui-font-color1);border-radius:8px;'+
'max-width:95vw;max-height:90vh;min-width:320px;display:flex;flex-direction:column;box-shadow:0 10px 40px rgba(0,0,0,.4)';
var hd=document.createElement('div');
hd.style.cssText='display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 12px;'+
'border-bottom:1px solid var(--jp-border-color2)';
var tt=document.createElement('strong');tt.style.cssText='font-size:13px;margin-right:auto';tt.textContent=titleText;
var cx=btn('✕ Close');hd.appendChild(tt);
var bd=document.createElement('div');bd.style.cssText='overflow:auto;padding:12px';
md.appendChild(hd);md.appendChild(bd);ov.appendChild(md);
function close(){document.removeEventListener('keydown',onkey);ov.remove();}
function onkey(e){if(e.key==='Escape')close();}
cx.onclick=close;ov.addEventListener('click',function(e){if(e.target===ov)close();});
document.addEventListener('keydown',onkey);
return {ov:ov,head:hd,title:tt,body:bd,close:cx,open:function(){document.body.appendChild(ov);}};
}
eExp.onclick=function(){
var m=modal(name+' — '+rows.length+' row'+(rows.length===1?'':'s')+(data.more?' (shown)':''));
var q=document.createElement('input');q.type='search';q.placeholder='Filter';
q.style.cssText='font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--jp-border-color2);'+
'border-radius:4px;background:var(--jp-layout-color0);color:var(--jp-ui-font-color1)';
m.head.appendChild(q);m.head.appendChild(m.close);
var tb=document.createElement('table');tb.style.cssText='border-collapse:collapse;font-size:12px;width:100%';
m.body.appendChild(tb);
function draw(f){f=(f||'').toLowerCase();var b=rows;
if(f)b=rows.filter(function(r){return r.some(function(v){return v!==null&&v!==undefined&&String(v).toLowerCase().indexOf(f)>=0;});});
tb.innerHTML='<thead><tr>'+cols.map(function(c){return '<th style="position:sticky;top:0;background:var(--jp-layout-color1);'+
'border-bottom:1px solid var(--jp-border-color1);padding:4px 10px;text-align:left;white-space:nowrap">'+h(c)+'</th>';}).join('')+
'</tr></thead><tbody>'+b.map(function(r){return '<tr>'+r.map(function(v){return '<td style="border-bottom:1px solid '+
'var(--jp-border-color2);padding:3px 10px;white-space:nowrap;max-width:480px;overflow:hidden;text-overflow:ellipsis">'+
(v===null||v===undefined?'<span style="opacity:.5;font-style:italic">null</span>':h(v))+'</td>';}).join('')+'</tr>';}).join('')+
'</tbody>';}
draw('');q.addEventListener('input',function(){draw(q.value);});m.open();q.focus();};
eChart.onclick=function(){
var CJ=window.__duckduckChart;  // Chart.js if the extension bundled it (richer); else the inline-SVG fallback
var m=modal('Chart — '+name+(data.more?' (shown rows)':''));
var nums=cols.map(function(_,i){return numCol(i);});
function sel(){var s=document.createElement('select');
s.style.cssText='font:inherit;font-size:12px;padding:2px 4px;border:1px solid var(--jp-border-color2);'+
'border-radius:4px;background:var(--jp-layout-color0);color:var(--jp-ui-font-color1)';return s;}
function opt(s,v,t){var o=document.createElement('option');o.value=v;o.textContent=t;s.appendChild(o);}
function lab(t,el){var w=document.createElement('label');
w.style.cssText='font-size:11px;color:var(--jp-ui-font-color2);display:flex;gap:4px;align-items:center';
w.appendChild(document.createTextNode(t));w.appendChild(el);return w;}
var tSel=sel(),aSel=sel(),xSel=sel(),ySel=sel();
var TYPES=CJ?[['bar','Bar'],['hbar','Horizontal bar'],['line','Line'],['area','Area'],['time','Time series'],['scatter','Scatter'],
['pie','Pie'],['doughnut','Doughnut'],['radar','Radar'],['polarArea','Polar area']]
:[['bar','Bar'],['line','Line'],['area','Area'],['scatter','Scatter'],['pie','Pie']];
TYPES.forEach(function(t){opt(tSel,t[0],t[1]);});
[['count','Count'],['sum','Sum'],['avg','Average'],['none','None (raw)']].forEach(function(a){opt(aSel,a[0],a[1]);});
cols.forEach(function(c,i){opt(xSel,String(i),c);});
cols.forEach(function(c,i){if(nums[i])opt(ySel,String(i),c);});
if(!ySel.options.length){cols.forEach(function(c,i){opt(ySel,String(i),c);});}
var hasNum=nums.indexOf(true)>=0;
var firstCat=nums.indexOf(false);xSel.value=String(firstCat>=0?firstCat:0);
ySel.selectedIndex=0;aSel.value=hasNum?'none':'count';  // all-text data → count rows per category by default
function chk(on){var c=document.createElement('input');c.type='checkbox';c.checked=!!on;return c;}
var logCb=chk(false),valCb=chk(false),legCb=chk(true),gridCb=chk(true);
var dBtn=btn(CJ?'⬇ PNG':'⬇ SVG');
var ePin=window.__duckduckChartImage?btn('📌 Add to notebook'):null;  // persist as an image in a markdown cell
if(ePin)ePin.title='Save this exact chart as an image cell in the notebook';
m.head.appendChild(lab('Type',tSel));m.head.appendChild(lab('Count/agg',aSel));
m.head.appendChild(lab('X',xSel));m.head.appendChild(lab('Y',ySel));
if(CJ){m.head.appendChild(lab('Values',valCb));m.head.appendChild(lab('Legend',legCb));
m.head.appendChild(lab('Grid',gridCb));m.head.appendChild(lab('Log Y',logCb));}
m.head.appendChild(dBtn);if(ePin)m.head.appendChild(ePin);m.head.appendChild(m.close);
var cap=document.createElement('div');cap.style.cssText='font-size:11px;color:var(--jp-ui-font-color2);margin:0 0 6px';
var host=document.createElement('div');host.style.cssText='width:76vw;max-width:900px;min-width:280px';
m.body.appendChild(cap);m.body.appendChild(host);
var PAL=['#2563eb','#d97706','#059669','#db2777','#7c3aed','#0891b2','#ca8a04','#dc2626'];
function selY(){return [Number(ySel.value||(ySel.options[0]||{}).value||0)];}
// group the rows by the X value → one point per category: count rows, or sum/avg a numeric Y
function grouped(xi,yi,agg){var m2={},order=[];
rows.forEach(function(r){var k=String(r[xi]==null?'':r[xi]);
if(!Object.prototype.hasOwnProperty.call(m2,k)){m2[k]={s:0,c:0};order.push(k);}
m2[k].c++;if(agg==='sum'||agg==='avg'){var n=Number(r[yi]);if(!isNaN(n))m2[k].s+=n;}});
return order.map(function(k){var gg=m2[k];return {k:k,v:agg==='count'?gg.c:agg==='avg'?(gg.c?gg.s/gg.c:0):gg.s};});}
function fmt(v){if(v===null||!isFinite(v))return '';var a=Math.abs(v);
if(a!==0&&(a>=1e5||a<1e-3))return v.toExponential(1);return (Math.round(v*1000)/1000).toString();}
// parse a date/time value to a UTC epoch ms — deterministic, so Safari and pandas' space-separated,
// 6-digit-microsecond strings ('2026-11-26 10:30:00.123456') parse the same as Chrome. tz-aware
// strings (Z or ±hh:mm) fall through to the engine's own Date.parse so their offset is honoured.
function toTime(x){if(x==null||x==='')return NaN;
if(typeof x==='number'&&isFinite(x))return x<1e12?x*1000:x;  // epoch seconds vs ms (best effort)
var s=String(x).trim();
if(!/(Z|[+\-]\d{2}:?\d{2})$/.test(s)){
var m=s.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?$/);
if(m){var ms=m[7]?Number((m[7]+'000').slice(0,3)):0;
return Date.UTC(+m[1],+m[2]-1,+m[3],+(m[4]||0),+(m[5]||0),+(m[6]||0),ms);}}
var t=Date.parse(s);return isNaN(t)?NaN:t;}
function capText(type,agg,xi,yi){return type==='scatter'?'Raw '+cols[yi]+' vs '+cols[xi]
:type==='time'?cols[yi]+' over time ('+cols[xi]+')'
:agg==='none'?'Raw '+cols[yi]+' by '+cols[xi]
:(agg==='count'?'Count of rows':(agg==='avg'?'Average of ':'Sum of ')+cols[yi])+' by '+cols[xi];}
function themeVar(v,f){var c=getComputedStyle(document.body).getPropertyValue(v);return (c&&c.trim())||f;}
var chart=null;
// Chart.js render (when the extension bundled it): more types, native tooltips/legend, log scale, PNG export
function drawCjs(){try{
var type=tSel.value,xi=Number(xSel.value),yi=selY()[0],agg=aSel.value;
cap.textContent=capText(type,agg,xi,yi);
var grid=themeVar('--jp-border-color2','rgba(128,128,128,.3)');
var ink=themeVar('--jp-ui-font-color1','#333');CJ.defaults.color=ink;
var fam=themeVar('--jp-ui-font-family','sans-serif');
// draw each value onto the chart (bars, points, slices) — Chart.js shows them only on hover otherwise
var valuePlugin={id:'ddvals',afterDatasetsDraw:function(ch){var c=ch.ctx;c.save();
c.fillStyle=ink;c.textAlign='center';c.textBaseline='bottom';c.font='11px '+fam;
ch.data.datasets.forEach(function(ds,di){var meta=ch.getDatasetMeta(di);if(meta.hidden)return;
meta.data.forEach(function(el,i){var raw=ds.data[i];var v=(raw&&typeof raw==='object')?raw.y:raw;
if(v==null||!isFinite(v))return;var p=el.tooltipPosition();c.fillText(fmt(v),p.x,p.y-2);});});c.restore();}};
var isCat=(type==='pie'||type==='doughnut'||type==='polarArea'||type==='radar');
var cjsType=type==='hbar'?'bar':(type==='area'||type==='time')?'line':type;
function cart(){var o={responsive:true,maintainAspectRatio:false,plugins:{legend:{display:legCb.checked}},
scales:{x:{grid:{display:gridCb.checked,color:grid}},y:{grid:{display:gridCb.checked,color:grid}}}};
if(type==='hbar')o.indexAxis='y';
if(logCb.checked)o.scales[type==='hbar'?'x':'y']={type:'logarithmic',grid:{display:gridCb.checked,color:grid}};
return o;}
var cfg;
if(type==='time'){
// Y over a real, proportional time axis: parse X as dates, feed numeric timestamps to a linear scale
// and format the ticks ourselves — a true time axis with no Chart.js date-adapter dependency
function warn(msg){host.innerHTML='<div style="padding:18px;color:var(--jp-ui-font-color2);font-size:12px">'+
msg+'</div>';cap.textContent='';if(chart){chart.destroy();chart=null;}}
var xr=rows.map(function(r){return toTime(r[xi]);});
if(!xr.some(function(t){return !isNaN(t);})){
warn('Couldn’t read dates in <b>'+h(cols[xi])+'</b> — pick a date/time column for X.');return;}
var pts=rows.map(function(r,i){return {x:xr[i],y:Number(r[yi])};})
.filter(function(p){return !isNaN(p.x)&&!isNaN(p.y);}).sort(function(a,b){return a.x-b.x;});
if(!pts.length){warn('<b>'+h(cols[yi])+'</b> has no numbers to plot — pick a numeric column for Y.');return;}
var span=pts[pts.length-1].x-pts[0].x;
function tfmt(v){var d=new Date(v);if(isNaN(d))return '';
return span<=2*864e5?d.toLocaleString(undefined,{timeZone:'UTC',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})
:d.toLocaleDateString(undefined,{timeZone:'UTC',year:'numeric',month:'short',day:'numeric'});}
var o=cart();o.scales.x={type:'linear',grid:{display:gridCb.checked,color:grid},
ticks:{maxRotation:0,autoSkip:true,maxTicksLimit:8,callback:function(v){return tfmt(v);}}};
o.plugins.tooltip={callbacks:{title:function(it){return it.length?tfmt(it[0].parsed.x):'';}}};
cfg={type:'line',data:{datasets:[{label:cols[yi],data:pts,borderColor:PAL[0],
backgroundColor:'rgba(37,99,235,.18)',borderWidth:2,pointRadius:2,fill:false,tension:.25}]},options:o};
}else if(type==='scatter'){
var sp=rows.map(function(r){return {x:Number(r[xi]),y:Number(r[yi])};}).filter(function(p){return !isNaN(p.x)&&!isNaN(p.y);});
cfg={type:'scatter',data:{datasets:[{label:cols[yi]+' vs '+cols[xi],data:sp,backgroundColor:PAL[0]}]},options:cart()};
}else if(isCat){
var gp=grouped(xi,yi,agg==='none'?'count':agg).filter(function(o){return isFinite(o.v);});
if(type!=='radar')gp.sort(function(a,b){return b.v-a.v;});
if(gp.length>12){if(type==='radar'){gp=gp.slice(0,12);}
else{var rest=gp.slice(11).reduce(function(s,o){return s+o.v;},0);gp=gp.slice(0,11);gp.push({k:'(others)',v:rest});}}
var colors=gp.map(function(_,i){return PAL[i%PAL.length];});
cfg={type:cjsType,data:{labels:gp.map(function(o){return o.k;}),datasets:[{label:cols[yi],
data:gp.map(function(o){return o.v;}),backgroundColor:type==='radar'?'rgba(37,99,235,.2)':colors,
borderColor:type==='radar'?PAL[0]:colors,borderWidth:1}]},
options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:legCb.checked,position:'right'}}}};
}else{
var labels,vals;
if(agg!=='none'){var g2=grouped(xi,yi,agg);labels=g2.map(function(o){return o.k;});vals=g2.map(function(o){return o.v;});}
else{labels=rows.map(function(r){return String(r[xi]==null?'':r[xi]);});
vals=rows.map(function(r){var n=Number(r[yi]);return isNaN(n)?null:n;});}
var isBar=(type==='bar'||type==='hbar');
cfg={type:cjsType,data:{labels:labels,datasets:[{label:cols[yi],data:vals,
backgroundColor:isBar?labels.map(function(_,i){return PAL[i%PAL.length];}):'rgba(37,99,235,.2)',
borderColor:PAL[0],borderWidth:isBar?0:2,fill:type==='area',tension:.25,
pointRadius:(type==='line'||type==='area')?2:0}]},options:cart()};
}
if(valCb.checked)cfg.plugins=(cfg.plugins||[]).concat([valuePlugin]);
host.innerHTML='';host.style.position='relative';host.style.height='440px';
var cv=document.createElement('canvas');host.appendChild(cv);
if(chart)chart.destroy();
chart=new CJ(cv.getContext('2d'),cfg);
}catch(e){host.textContent='Couldn’t draw this — try other columns.';}}
function drawSvg(){try{
var type=tSel.value,xi=Number(xSel.value),yis=selY(),agg=aSel.value;
cap.textContent=type==='scatter'?'Raw '+cols[yis[0]]+' vs '+cols[xi]
:agg==='none'?'Raw '+cols[yis[0]]+' by '+cols[xi]
:(agg==='count'?'Count of rows':(agg==='avg'?'Average of ':'Sum of ')+cols[yis[0]])+' by '+cols[xi];
var W=760,H=420,L=56,R=16,T=16,B=78,pw=W-L-R,ph=H-T-B;
var ink='var(--jp-ui-font-color1)',mut='var(--jp-ui-font-color2)',ax='var(--jp-border-color1)',gd='var(--jp-border-color2)';
function ln(x1,y1,x2,y2,c){return '<line x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+'" style="stroke:'+c+'"/>';}
function tx(x,y,t,c,an,rot){return '<text x="'+x+'" y="'+y+'" style="fill:'+(c||ink)+'" text-anchor="'+(an||'middle')+'"'+
(rot?' transform="rotate('+rot+' '+x+' '+y+')"':'')+'>'+h(t)+'</text>';}
var g='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;font:11px var(--jp-ui-font-family,sans-serif)" '+
'xmlns="http://www.w3.org/2000/svg">';
if(type==='pie'){
var pv=grouped(xi,yis[0],agg==='none'?'count':agg).filter(function(o){return o.v>0;}).sort(function(a,b){return b.v-a.v;});
if(!pv.length){host.innerHTML='<div style="padding:18px;color:var(--jp-ui-font-color2);font-size:12px">Nothing to chart '+
'here. Text columns have no values to sum — set <b>Count/agg = Count</b> to chart how many rows per category.</div>';
cap.textContent='';return;}
if(pv.length>12){var rest=pv.slice(11).reduce(function(s,o){return s+o.v;},0);pv=pv.slice(0,11);pv.push({k:'(others)',v:rest});}
var tot=pv.reduce(function(s,o){return s+o.v;},0)||1,cx=L+ph/2,cy=T+ph/2,rr=ph/2-4,an=-Math.PI/2;
pv.forEach(function(o,i){var a2=an+o.v/tot*2*Math.PI,x1=cx+rr*Math.cos(an),y1=cy+rr*Math.sin(an),
x2=cx+rr*Math.cos(a2),y2=cy+rr*Math.sin(a2),big=(a2-an)>Math.PI?1:0;
g+='<path d="M'+cx+' '+cy+' L'+x1+' '+y1+' A'+rr+' '+rr+' 0 '+big+' 1 '+x2+' '+y2+' Z" fill="'+PAL[i%PAL.length]+
'" style="stroke:var(--jp-layout-color1);stroke-width:1"/>';an=a2;});
var lx=L+ph+24,ly=T+12;
pv.forEach(function(o,i){g+='<rect x="'+lx+'" y="'+(ly-9)+'" width="10" height="10" fill="'+PAL[i%PAL.length]+'"/>'+
tx(lx+15,ly,o.k+' ('+Math.round(o.v/tot*100)+'%)',ink,'start');ly+=16;});
host.innerHTML=g+'</svg>';return;}
var labels,series;
if(agg!=='none'&&type!=='scatter'){var gp=grouped(xi,yis[0],agg);
labels=gp.map(function(d){return d.k;});series=[gp.map(function(d){return d.v;})];}
else{labels=rows.map(function(r){return String(r[xi]==null?'':r[xi]);});
series=yis.map(function(yi){return rows.map(function(r){var n=Number(r[yi]);return isNaN(n)?null:n;});});}
var vals=[].concat.apply([],series).filter(function(v){return v!==null;});
var ymax=Math.max.apply(null,vals.concat([0])),ymin=Math.min.apply(null,vals.concat([0]));if(ymax===ymin)ymax=ymin+1;
function Y(v){return T+ph-(v-ymin)/(ymax-ymin)*ph;}
for(var t=0;t<=5;t++){var vv=ymin+(ymax-ymin)*t/5,yy=Y(vv);g+=ln(L,yy,L+pw,yy,gd)+tx(L-6,yy+3,fmt(vv),mut,'end');}
g+=ln(L,T,L,T+ph,ax)+ln(L,Y(0),L+pw,Y(0),ax);
var n=labels.length||1,band=pw/n,step=Math.ceil(n/18);
if(type==='scatter'){
var xn=rows.map(function(r){var x=Number(r[xi]);return isNaN(x)?null:x;}),xs=xn.filter(function(v){return v!==null;});
var xmax=Math.max.apply(null,xs),xmin=Math.min.apply(null,xs);if(xmax===xmin)xmax=xmin+1;
function X(v){return L+(v-xmin)/(xmax-xmin)*pw;}
for(var t2=0;t2<=5;t2++){var xv=xmin+(xmax-xmin)*t2/5;g+=tx(X(xv),T+ph+16,fmt(xv),mut);}
series.forEach(function(ser,si){ser.forEach(function(v,i){if(v===null||xn[i]===null)return;
g+='<circle cx="'+X(xn[i])+'" cy="'+Y(v)+'" r="3.2" fill="'+PAL[si%PAL.length]+'" fill-opacity="0.8"/>';});});
}else{
labels.forEach(function(lb,i){if(i%step)return;var xc=L+band*(i+0.5),sx=lb.length>12?lb.slice(0,11)+'…':lb;
g+=tx(xc,T+ph+14,sx,mut,'end',-40);});
series.forEach(function(ser,si){var c=PAL[si%PAL.length];
if(type==='bar'){var bw=band*0.8/series.length;ser.forEach(function(v,i){if(v===null)return;
var x0=L+band*i+band*0.1+si*bw,y1=Y(Math.max(v,0)),y2=Y(Math.min(v,0));
g+='<rect x="'+x0+'" y="'+y1+'" width="'+Math.max(1,bw-1)+'" height="'+Math.max(0,y2-y1)+'" fill="'+c+'"/>';});
}else{var pts=ser.map(function(v,i){return v===null?null:[L+band*(i+0.5),Y(v)];}).filter(function(p){return p;});
var pd=pts.map(function(p,i){return (i?'L':'M')+p[0]+' '+p[1];}).join(' ');
if(type==='area'&&pts.length)g+='<path d="'+pd+' L'+pts[pts.length-1][0]+' '+Y(0)+' L'+pts[0][0]+' '+Y(0)+
' Z" fill="'+c+'" fill-opacity="0.18"/>';
g+='<path d="'+pd+'" fill="none" stroke="'+c+'" stroke-width="2"/>';}});}
if(series.length>1){var lx=L+6;yis.forEach(function(yi,si){g+='<rect x="'+lx+'" y="'+(T+2)+'" width="10" height="10" fill="'+
PAL[si%PAL.length]+'"/>'+tx(lx+14,T+11,cols[yi],ink,'start');lx+=cols[yi].length*7+34;});}
host.innerHTML=g+'</svg>';
}catch(e){host.textContent='Couldn’t draw this — try other columns.';}}
function draw(){if(CJ){drawCjs();}else{drawSvg();}}
[tSel,aSel,xSel,ySel,logCb,valCb,legCb,gridCb].forEach(function(el){el.addEventListener('change',draw);el.addEventListener('input',draw);});
dBtn.onclick=function(){
if(CJ){if(chart){var a=document.createElement('a');a.href=chart.toBase64Image();a.download=name+'.png';
document.body.appendChild(a);a.click();document.body.removeChild(a);}}
else{var sv=host.querySelector('svg');if(sv)dl('<?xml version="1.0"?>\n'+sv.outerHTML,'image/svg+xml',name+'.svg');}};
if(ePin)ePin.onclick=function(){
var url=null;
if(CJ&&chart){try{url=chart.toBase64Image();}catch(e){}}  // Chart.js canvas → a PNG data URL
else{var sv=host.querySelector('svg');  // the SVG fallback → an svg data URL
if(sv)url='data:image/svg+xml;base64,'+btoa(unescape(encodeURIComponent(sv.outerHTML)));}
if(!url)return;
window.__duckduckChartImage(url);
var t0=ePin.textContent;ePin.textContent='✓ Added';setTimeout(function(){ePin.textContent=t0;},1400);};
m.close.addEventListener('click',function(){if(chart)chart.destroy();});
draw();m.open();};
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


def dataframe_display(frame: Any, rows: int = 200) -> Optional[Dict[str, str]]:
    """A pandas DataFrame shown in the kernel → the same HTML table + toolbar (Expand / Chart / CSV / Excel / Edit)
    that SQL-cell results get, instead of pandas' plain repr. Returns ``None`` for anything that isn't a
    DataFrame with columns, so the caller keeps pandas' own ``_repr_html_`` (never raises — display must not fail)."""
    try:
        columns = [[str(c), str(dt)] for c, dt in zip(list(frame.columns), list(frame.dtypes))]
    except Exception:  # noqa: BLE001 — not a DataFrame-shaped object
        return None
    if not columns:
        return None
    try:
        total = int(len(frame))
        head = frame.head(rows)
        data = [[_frame_cell(v) for v in row] for row in head.itertuples(index=False, name=None)]
    except Exception:  # noqa: BLE001 — an odd frame (object columns, a broken __len__…): leave it to pandas
        return None
    return render({"name": DEFAULT_NAME, "columns": columns, "rows": data, "show": len(data),
                   "more": total > len(data), "total": total, "plain": True})


__all__ = ["DEFAULT_NAME", "END", "HEADER", "MARK", "METADATA_KEY", "SqlCellError", "dataframe_display",
           "frame_result", "from_metadata", "header_for", "parse_header", "render", "session_code", "split_cell",
           "split_output"]

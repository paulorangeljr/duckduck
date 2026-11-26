"""A notebook's tables and charts as a presentation.

The labextension walks the open notebook and collects what it has *rendered* — every result table
(our ``duckduck-rows`` outputs) and every chart (a pinned chart's markdown image, a matplotlib
``image/png``) — in notebook order. Those items come here, one slide each, and ``present_html``
returns a **self-contained page**: the slides embedded as JSON plus its own editor, so the user can
write a short description above and below each one, add and remove slides, present it full screen,
print it and download it as a single HTML file that still works offline.

Nothing here reads data or talks to a kernel: it is a pure function of the collected items, which is
what makes it testable (``tests/test_notebook.py``). Values are embedded as JSON (never as HTML) and
the page writes every value with ``textContent``, so a cell holding ``<script>`` stays text.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Dict, List, Optional

#: a deck is capped so one accidental "present everything" can't build a 100 MB page
MAX_SLIDES = 60
#: rows kept per table slide (the notebook output itself only holds the shown rows)
MAX_ROWS = 200
#: columns kept per table slide
MAX_COLUMNS = 30
#: an embedded image (a pinned chart is a PNG data URL) over this is dropped with a note
MAX_IMAGE_CHARS = 4_000_000
#: rows a table slide shows by default — a slide is read, not scrolled
DEFAULT_ROWS_SHOWN = 12

KINDS = ("cover", "table", "image", "text")


class PresentError(Exception):
    """A deck that can't be built (nothing collected, or items of no known kind)."""


def _text(value: Any, limit: int = 2000) -> str:
    """Any value → a plain string, bounded. Never HTML: the page writes it with ``textContent``."""
    if value is None:
        return ""
    out = value if isinstance(value, str) else str(value)
    return out[:limit]


def _columns(value: Any) -> List[List[str]]:
    """``[[name, type], …]`` (what a result output carries) or ``[name, …]`` → ``[[name, type], …]``."""
    out: List[List[str]] = []
    for col in (value or [])[:MAX_COLUMNS]:
        if isinstance(col, (list, tuple)):
            name = _text(col[0] if col else "", 200)
            kind = _text(col[1] if len(col) > 1 else "", 60)
        else:
            name, kind = _text(col, 200), ""
        out.append([name, kind])
    return out


def _rows(value: Any, width: int) -> List[List[Any]]:
    """The shown rows, capped, each padded/truncated to the column count. JSON scalars are kept as
    they are (numbers stay numbers, so the page can right-align them); anything else becomes text."""
    out: List[List[Any]] = []
    for row in (value or [])[:MAX_ROWS]:
        cells = list(row) if isinstance(row, (list, tuple)) else [row]
        kept: List[Any] = []
        for cell in cells[:width]:
            kept.append(cell if cell is None or isinstance(cell, (int, float, bool, str)) else _text(cell, 400))
        kept += [None] * (width - len(kept))
        out.append(kept)
    return out


def _slide(item: Any, notes: List[str], index: int) -> Optional[Dict[str, Any]]:
    """One collected item → one slide, or None (with a note) when there's nothing to show."""
    if not isinstance(item, dict):
        notes.append(f"item {index + 1}: not an object")
        return None
    kind = _text(item.get("kind"), 20) or "text"
    if kind not in KINDS or kind == "cover":
        notes.append(f"item {index + 1}: unknown kind {kind!r}")
        return None
    slide: Dict[str, Any] = {"kind": kind, "title": _text(item.get("title"), 200),
                             "above": _text(item.get("above")), "below": _text(item.get("below"))}
    if kind == "table":
        columns = _columns(item.get("columns"))
        if not columns:
            notes.append(f"item {index + 1}: a table with no columns")
            return None
        rows = _rows(item.get("rows"), len(columns))
        slide.update(columns=columns, rows=rows, more=bool(item.get("more")),
                     shown=min(DEFAULT_ROWS_SHOWN, len(rows)) or DEFAULT_ROWS_SHOWN)
        slide["title"] = slide["title"] or "Table"
    elif kind == "image":
        src = _text(item.get("src"), MAX_IMAGE_CHARS + 1)
        # self-contained on purpose: only an embedded image, never a URL the deck would have to fetch
        if not src.startswith("data:image/"):
            notes.append(f"item {index + 1}: a chart that isn't an embedded image")
            return None
        if len(src) > MAX_IMAGE_CHARS:
            notes.append(f"item {index + 1}: a chart too large to embed ({len(src) // 1000} kB)")
            return None
        slide["src"] = src
        slide["title"] = slide["title"] or "Chart"
    return slide


def slides_from_items(items: Any, title: str = "") -> List[Dict[str, Any]]:
    """What the notebook collected → the deck's slides, a cover first. Raises ``PresentError`` when
    nothing usable came back, so the page can say so instead of opening an empty deck."""
    notes: List[str] = []
    slides: List[Dict[str, Any]] = [{"kind": "cover", "title": _text(title, 200) or "Presentation",
                                     "above": "", "below": ""}]
    for index, item in enumerate(items if isinstance(items, (list, tuple)) else []):
        if len(slides) > MAX_SLIDES:
            notes.append(f"stopped at {MAX_SLIDES} slides")
            break
        made = _slide(item, notes, index)
        if made is not None:
            slides.append(made)
    if len(slides) == 1:
        raise PresentError("nothing to present yet — run a cell that shows a table, or pin a chart "
                           "to the notebook with 📌, then try again"
                           + (" (" + "; ".join(notes[:3]) + ")" if notes else ""))
    return slides


def deck(items: Any, title: str = "", notebook: str = "") -> Dict[str, Any]:
    """The deck as data: what gets embedded in the page and what a download writes back."""
    name = _text(title, 200) or _text(notebook, 200) or "Presentation"
    return {"title": name, "notebook": _text(notebook, 400),
            "made": _dt.date.today().isoformat(), "slides": slides_from_items(items, name)}


def present_html(items: Any, title: str = "", notebook: str = "") -> str:
    """The whole presentation as one self-contained HTML page."""
    data = deck(items, title=title, notebook=notebook)
    # JSON, never HTML: `<` escaped so no value can close the script early
    embedded = json.dumps(data, default=str).replace("<", "\\u003c")
    return (_TEMPLATE.replace("__DECK_TITLE__", _html_escape(data["title"]))
            .replace("__DECK_JSON__", embedded))


def _html_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# The page: its own styles and editor, no dependency, works offline and after a download.
# Light and dark both come from tokens on :root. Slides are rendered from the embedded JSON every
# time, so a downloaded copy rebuilds itself from its own data.
_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__DECK_TITLE__</title>
<style>
:root{
  --bg:#f6f7f9; --surface:#ffffff; --ink:#1a1d21; --ink2:#5b6470; --ink3:#8b949e;
  --line:#e3e6ea; --accent:#2563eb; --accent-soft:#eaf1ff; --shadow:0 1px 3px rgba(16,24,40,.08),0 8px 24px rgba(16,24,40,.06);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#14171a; --surface:#1c2024; --ink:#e8eaed; --ink2:#a7b0ba; --ink3:#78818c;
  --line:#2c3238; --accent:#6ea0ff; --accent-soft:#1f2a3d; --shadow:0 1px 3px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3);
}}
:root[data-theme="dark"]{
  --bg:#14171a; --surface:#1c2024; --ink:#e8eaed; --ink2:#a7b0ba; --ink3:#78818c;
  --line:#2c3238; --accent:#6ea0ff; --accent-soft:#1f2a3d; --shadow:0 1px 3px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3);
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  display:flex;flex-direction:column}
button{font:inherit;font-size:12px;padding:5px 11px;border-radius:6px;cursor:pointer;
  border:1px solid var(--line);background:var(--surface);color:var(--ink)}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{filter:brightness(1.08);color:#fff}
button:disabled{opacity:.4;cursor:default}
button:disabled:hover{border-color:var(--line);color:var(--ink)}

/* top bar */
.top{display:flex;gap:8px;align-items:center;padding:9px 14px;border-bottom:1px solid var(--line);
  background:var(--surface);flex:0 0 auto;flex-wrap:wrap}
.top .name{font-weight:650;font-size:14px;margin-right:auto;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;max-width:46vw}
.top .sub{color:var(--ink3);font-size:11px;font-weight:400;margin-left:8px}

/* body: thumbnail rail + the slide */
.main{flex:1 1 auto;display:flex;min-height:0}
.rail{flex:0 0 190px;border-right:1px solid var(--line);background:var(--surface);overflow:auto;padding:10px}
.thumb{display:flex;gap:8px;align-items:flex-start;width:100%;text-align:left;margin:0 0 7px;padding:7px 8px;
  border:1px solid var(--line);border-radius:7px;background:var(--bg);cursor:pointer;color:var(--ink)}
.thumb:hover{border-color:var(--accent)}
.thumb.on{border-color:var(--accent);background:var(--accent-soft)}
.thumb .n{color:var(--ink3);font-size:10px;font-variant-numeric:tabular-nums;padding-top:1px;min-width:13px}
.thumb .tl{font-size:11.5px;line-height:1.35;overflow:hidden;display:block;
  text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto}
.thumb .ic{font-size:11px;opacity:.75}
.stagewrap{flex:1 1 auto;overflow:auto;padding:22px;display:flex;flex-direction:column;align-items:center}

/* a slide */
.slide{width:100%;max-width:940px;background:var(--surface);border:1px solid var(--line);border-radius:12px;
  box-shadow:var(--shadow);padding:34px 38px;display:flex;flex-direction:column;gap:14px;min-height:420px}
.slide h2{margin:0;font-size:25px;line-height:1.25;font-weight:650;letter-spacing:-.01em}
.slide .body{flex:1 1 auto;display:flex;flex-direction:column;justify-content:center;min-height:0}
.ed{outline:none;color:var(--ink2);font-size:14.5px;white-space:pre-wrap;word-wrap:break-word;
  border-radius:6px;padding:3px 5px;margin:0 -5px}
.ed:empty:before{content:attr(data-ph);color:var(--ink3);font-style:italic}
.ed[contenteditable="true"]:hover{background:var(--bg)}
.ed[contenteditable="true"]:focus{background:var(--bg);box-shadow:inset 0 0 0 1px var(--accent)}
h2.ed{color:var(--ink)}

/* a table on a slide */
.tw{overflow:auto;max-width:100%;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:6px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap;
  max-width:340px;overflow:hidden;text-overflow:ellipsis}
th{background:var(--bg);font-weight:620;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;
  color:var(--ink2);position:sticky;top:0}
tr:last-child td{border-bottom:none}
td.num{text-align:right}
td.null{color:var(--ink3);font-style:italic}
.note{color:var(--ink3);font-size:11.5px;margin-top:7px}
.slide img{max-width:100%;max-height:52vh;object-fit:contain;display:block;margin:0 auto;border-radius:6px}

/* cover */
.slide.cover{justify-content:center;align-items:center;text-align:center;gap:18px}
.slide.cover h2{font-size:38px;letter-spacing:-.02em}
.slide.cover .ed{text-align:center;font-size:16px}

/* under-slide controls */
.ctl{width:100%;max-width:940px;display:flex;gap:7px;align-items:center;margin:12px 0 4px;flex-wrap:wrap}
.ctl .count{color:var(--ink3);font-size:12px;margin-left:auto;font-variant-numeric:tabular-nums}
.ctl label{font-size:11.5px;color:var(--ink2);display:flex;gap:5px;align-items:center}
.ctl input[type=number]{font:inherit;font-size:12px;width:62px;padding:3px 5px;border-radius:5px;
  border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.empty{color:var(--ink3);text-align:center;padding:50px 20px;font-size:14px}

/* present mode: the slide alone, full screen */
body.show .top,body.show .rail,body.show .ctl{display:none}
body.show .stagewrap{padding:0;justify-content:center;height:100vh}
body.show .slide{max-width:none;width:100vw;height:100vh;min-height:0;border:none;border-radius:0;
  box-shadow:none;justify-content:center;padding:6vh 7vw}
body.show .slide h2{font-size:min(5.2vw,46px)}
body.show .slide .ed{font-size:min(2.4vw,21px)}
body.show .slide.cover h2{font-size:min(7vw,64px)}
body.show .slide img{max-height:62vh}
body.show .ed:empty{display:none}
body.show .showbar{display:flex}
.showbar{display:none;position:fixed;right:16px;bottom:14px;gap:8px;align-items:center;
  background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:5px 9px;box-shadow:var(--shadow)}
.showbar .count{color:var(--ink3);font-size:12px;font-variant-numeric:tabular-nums;padding:0 2px}

/* print: one slide per page */
@media print{
  body{background:#fff}
  .top,.rail,.ctl,.showbar{display:none!important}
  .stagewrap{padding:0;display:block;overflow:visible}
  .slide{box-shadow:none;border:none;max-width:none;width:100%;min-height:92vh;break-after:page;page-break-after:always}
  .ed:empty{display:none}
  .tw{overflow:visible}
}
</style>
</head>
<body>
<div class="top">
  <span class="name" id="deckname"></span>
  <button id="add">＋ Slide</button>
  <button id="prev">‹</button>
  <button id="next">›</button>
  <button class="primary" id="show">▶ Present</button>
  <button id="print">⎙ PDF</button>
  <button id="save">⬇ Download</button>
</div>
<div class="main">
  <div class="rail" id="rail"></div>
  <div class="stagewrap"><div id="stage"></div><div class="ctl" id="ctl"></div></div>
</div>
<div class="showbar">
  <button id="sprev">‹</button><span class="count" id="scount"></span><button id="snext">›</button>
  <button id="sexit">✕ Esc</button>
</div>
<script type="application/json" id="duckduck-deck">__DECK_JSON__</script>
<script>
(function(){
var DATA=document.getElementById('duckduck-deck');
var DECK={title:'Presentation',slides:[]};
try{DECK=JSON.parse(DATA.textContent)||DECK;}catch(e){}
if(!DECK.slides||!DECK.slides.length)DECK.slides=[{kind:'cover',title:DECK.title||'Presentation',above:'',below:''}];
var KEY='duckduck-deck:'+(DECK.notebook||DECK.title||'');
var at=0,showing=false;

// descriptions people typed are kept per deck in this browser, so reopening doesn't lose the story.
// Storage can be unavailable or throw (private window, blocked site data) — the deck works without it.
try{var kept=localStorage.getItem(KEY);
if(kept){var k=JSON.parse(kept);
if(k&&k.slides&&k.slides.length===DECK.slides.length){ // same notebook, same shape → take the words back
DECK.title=k.title||DECK.title;
k.slides.forEach(function(s,i){var d=DECK.slides[i];if(!d||!s)return;
d.above=s.above||d.above;d.below=s.below||d.below;d.title=s.title||d.title;
if(s.shown)d.shown=s.shown;if(s.kind==='text'&&d.kind==='text')d.kind='text';});}}}catch(e){}
function save(){try{localStorage.setItem(KEY,JSON.stringify(DECK));}catch(e){}}

function el(tag,cls,text){var n=document.createElement(tag);if(cls)n.className=cls;
if(text!==undefined&&text!==null)n.textContent=text;return n;}
function isNum(v){return typeof v==='number'&&isFinite(v);}

// an editable line: plain text only (paste is forced to text), written straight back into the slide
function edit(tag,cls,value,ph,set){var n=el(tag,'ed'+(cls?' '+cls:''),value||'');
n.setAttribute('data-ph',ph);n.setAttribute('contenteditable',showing?'false':'true');
n.addEventListener('input',function(){set(n.innerText.replace(/ /g,' '));save();
if(cls==='t')railDraw();});
n.addEventListener('paste',function(e){e.preventDefault();
var t=(e.clipboardData||window.clipboardData).getData('text/plain')||'';
document.execCommand('insertText',false,t);});
n.addEventListener('keydown',function(e){e.stopPropagation();});  // typing never drives the deck
return n;}

function tableOf(s){
var box=el('div','tw'),tb=el('table'),head=el('thead'),hr=el('tr');
(s.columns||[]).forEach(function(c){hr.appendChild(el('th',null,c[0]));});
head.appendChild(hr);tb.appendChild(head);
var body=el('tbody'),rows=s.rows||[],n=Math.max(1,Math.min(rows.length,s.shown||rows.length));
rows.slice(0,n).forEach(function(r){var tr=el('tr');
(s.columns||[]).forEach(function(_,i){var v=r[i];
var td=el('td',v===null||v===undefined?'null':(isNum(v)?'num':null),
v===null||v===undefined?'null':String(v));tr.appendChild(td);});
body.appendChild(tr);});
tb.appendChild(body);box.appendChild(tb);
var wrap=el('div');wrap.appendChild(box);
if(rows.length>n||s.more){wrap.appendChild(el('div','note',
'Showing '+n+' of '+rows.length+(s.more?'+':'')+' row'+(rows.length===1?'':'s')));}
return wrap;}

function stageDraw(){
var stage=document.getElementById('stage');stage.innerHTML='';
var s=DECK.slides[at];if(!s){stage.appendChild(el('div','empty','No slides left — ＋ Slide adds one.'));ctlDraw();return;}
var slide=el('div','slide'+(s.kind==='cover'?' cover':''));
var isCover=(s.kind==='cover');
slide.appendChild(edit('h2','t',isCover?DECK.title:s.title,isCover?'Presentation title':'Slide title',
function(v){if(isCover){DECK.title=v;nameDraw();}else{s.title=v;}}));
slide.appendChild(edit('div','a',s.above,isCover?'Who it is for, when, by whom':'What this shows — the point of it',
function(v){s.above=v;}));
if(!isCover){var body=el('div','body');
if(s.kind==='table'){body.appendChild(tableOf(s));}
else if(s.kind==='image'){var img=document.createElement('img');img.src=s.src;img.alt=s.title||'chart';
body.appendChild(img);}
slide.appendChild(body);
slide.appendChild(edit('div','b',s.below,'What to take away from it',function(v){s.below=v;}));}
stage.appendChild(slide);ctlDraw();}

function railDraw(){
var rail=document.getElementById('rail');rail.innerHTML='';
DECK.slides.forEach(function(s,i){
var b=el('button','thumb'+(i===at?' on':''));
b.appendChild(el('span','n',String(i+1)));
b.appendChild(el('span','ic',s.kind==='table'?'▦':s.kind==='image'?'▣':s.kind==='cover'?'★':'✎'));
b.appendChild(el('span','tl',(i===0&&s.kind==='cover'?DECK.title:s.title)||'Untitled'));
b.onclick=function(){go(i);};rail.appendChild(b);});}

function ctlDraw(){
var c=document.getElementById('ctl');c.innerHTML='';
var s=DECK.slides[at];
var up=el('button',null,'↑');up.title='Move this slide earlier';up.disabled=at<=0;up.onclick=function(){move(-1);};
var dn=el('button',null,'↓');dn.title='Move this slide later';dn.disabled=at>=DECK.slides.length-1;
dn.onclick=function(){move(1);};
var rm=el('button',null,'✕ Remove');rm.title='Remove this slide from the deck';
rm.disabled=DECK.slides.length<=1;rm.onclick=remove;
c.appendChild(up);c.appendChild(dn);c.appendChild(rm);
if(s&&s.kind==='table'&&(s.rows||[]).length>1){
var inp=document.createElement('input');inp.type='number';inp.min='1';inp.max=String(s.rows.length);
inp.value=String(Math.min(s.shown||s.rows.length,s.rows.length));
inp.onchange=function(){var v=parseInt(inp.value,10);
s.shown=Math.max(1,Math.min(isNaN(v)?1:v,s.rows.length));save();stageDraw();};
var l=el('label');l.appendChild(el('span',null,'Rows'));l.appendChild(inp);c.appendChild(l);}
c.appendChild(el('span','count',(at+1)+' / '+DECK.slides.length));}

function nameDraw(){
var n=document.getElementById('deckname');n.innerHTML='';
n.appendChild(el('span',null,DECK.title||'Presentation'));
var bits=[];if(DECK.notebook)bits.push(DECK.notebook);if(DECK.made)bits.push(DECK.made);
if(bits.length)n.appendChild(el('span','sub',bits.join(' · ')));
document.title=DECK.title||'Presentation';}

function draw(){nameDraw();railDraw();stageDraw();countDraw();}
function countDraw(){document.getElementById('scount').textContent=(at+1)+' / '+DECK.slides.length;}
function go(i){at=Math.max(0,Math.min(i,DECK.slides.length-1));railDraw();stageDraw();countDraw();}
function move(d){var j=at+d;if(j<0||j>=DECK.slides.length)return;
var s=DECK.slides.splice(at,1)[0];DECK.slides.splice(j,0,s);at=j;save();draw();}
function remove(){if(DECK.slides.length<=1)return;
DECK.slides.splice(at,1);if(at>=DECK.slides.length)at=DECK.slides.length-1;save();draw();}
function add(){DECK.slides.splice(at+1,0,{kind:'text',title:'',above:'',below:''});save();go(at+1);
var f=document.querySelector('.slide .ed');if(f&&f.focus)f.focus();}

function present(on){showing=!!on;document.body.classList.toggle('show',showing);
if(showing&&document.documentElement.requestFullscreen){
try{document.documentElement.requestFullscreen();}catch(e){}}
else if(!showing&&document.fullscreenElement&&document.exitFullscreen){
try{document.exitFullscreen();}catch(e){}}
stageDraw();countDraw();}

// a download is this very page with the current slides written back into its data block, so the
// saved file opens offline and rebuilds itself — the one copy that survives closing the tab
function download(){
var was=showing;if(was)present(false);
DATA.textContent=JSON.stringify(DECK).replace(/</g,'\\u003c');
var html='<!doctype html>\n'+document.documentElement.outerHTML;
var a=document.createElement('a'),url=URL.createObjectURL(new Blob([html],{type:'text/html'}));
a.href=url;a.download=(DECK.title||'presentation').replace(/[^A-Za-z0-9._-]+/g,'_').slice(0,80)+'.html';
document.body.appendChild(a);a.click();document.body.removeChild(a);
setTimeout(function(){URL.revokeObjectURL(url);},4000);
if(was)present(true);}

document.getElementById('add').onclick=add;
document.getElementById('prev').onclick=function(){go(at-1);};
document.getElementById('next').onclick=function(){go(at+1);};
document.getElementById('show').onclick=function(){present(true);};
document.getElementById('print').onclick=function(){var w=showing;if(w)present(false);window.print();};
document.getElementById('save').onclick=download;
document.getElementById('sprev').onclick=function(){go(at-1);};
document.getElementById('snext').onclick=function(){go(at+1);};
document.getElementById('sexit').onclick=function(){present(false);};
document.addEventListener('keydown',function(e){
var typing=document.activeElement&&document.activeElement.isContentEditable;
if(e.key==='Escape'){if(showing)present(false);return;}
if(typing)return;  // while writing a description the arrows belong to the text
if(e.key==='ArrowRight'||e.key==='PageDown'||e.key===' '){go(at+1);e.preventDefault();}
else if(e.key==='ArrowLeft'||e.key==='PageUp'){go(at-1);e.preventDefault();}
else if(e.key==='Home'){go(0);}else if(e.key==='End'){go(DECK.slides.length-1);}});
document.addEventListener('fullscreenchange',function(){
if(!document.fullscreenElement&&showing)present(false);});
draw();
})();
</script>
</body>
</html>
"""

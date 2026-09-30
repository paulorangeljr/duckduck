"""The web app's single page (served by ``server.create_app`` at ``/``). Plain HTML/CSS/JS, no build step."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duckduck Ask</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='7' fill='%232a78d6'/%3E%3C/svg%3E">
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --surface-2: #f1f0ec; --border: rgba(11,11,11,0.10);
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781; --grid: #e1e0d9;
  --accent: #2a78d6; --accent-ink: #ffffff; --bar: #2a78d6;
  --good: #0ca30c; --good-ink: #006300; --warn: #fab219; --bad: #d03b3b;
  --radius: 10px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --surface-2: #232321; --border: rgba(255,255,255,0.10);
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781; --grid: #2c2c2a;
    --accent: #3987e5; --bar: #3987e5; --good-ink: #0ca30c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --surface-2: #232321; --border: rgba(255,255,255,0.10);
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781; --grid: #2c2c2a;
  --accent: #3987e5; --bar: #3987e5; --good-ink: #0ca30c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; align-items: center; gap: 16px; padding: 12px 20px; background: var(--surface);
         border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 2; flex-wrap: wrap; }
header h1 { font-size: 17px; margin: 0; font-weight: 650; }
nav { display: flex; gap: 4px; flex-wrap: wrap; }
nav button { background: none; border: 0; padding: 6px 12px; border-radius: 8px; color: var(--ink-2);
             font: inherit; cursor: pointer; }
nav button[aria-selected="true"] { background: var(--surface-2); color: var(--ink); font-weight: 600; }
.user { margin-left: auto; display: flex; gap: 8px; align-items: center; color: var(--muted); font-size: 13px; }
.user input { width: 140px; }
main { max-width: 1100px; margin: 0 auto; padding: 20px 16px 60px; }
[hidden] { display: none !important; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
        padding: 16px 18px; margin-bottom: 14px; }
.ask { display: flex; gap: 8px; }
input, textarea, select { font: inherit; color: var(--ink); background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.ask input { flex: 1; font-size: 16px; padding: 10px 12px; }
.ask .reader { align-self: center; flex: none; }
.ask .reader button[disabled] { opacity: .45; cursor: not-allowed; }
#askbtn { min-width: 92px; }
#sample { width: auto; flex: none; }
.morebar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 8px 0; padding: 8px 12px;
  border-radius: 10px; background: var(--surface-2); font-size: 14px; }
.colstatus { font-size: 13px; color: var(--muted); display: flex; align-items: center; gap: 6px; min-height: 20px; }
button.help { flex: none; align-self: center; width: 26px; height: 26px; border-radius: 50%; padding: 0;
              border: 1px solid var(--border); background: var(--surface-2); color: var(--ink-2); cursor: pointer;
              font: inherit; font-size: 13px; font-weight: 600; }
button.help[aria-expanded="true"] { outline: 2px solid var(--accent); }
.readerhelp .modes { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px 24px; }
.readerhelp p { margin: 6px 0 0; font-size: 13.5px; color: var(--ink-2); line-height: 1.45; }
.readerhelp p b { color: var(--ink); }
@media (max-width: 700px) { .readerhelp .modes { grid-template-columns: 1fr; } }
button.primary, button.secondary, button.option, button.verdict {
  font: inherit; border-radius: 8px; padding: 8px 14px; cursor: pointer; border: 1px solid var(--border); }
button.primary { background: var(--accent); color: var(--accent-ink); border-color: transparent; font-weight: 600; }
button.secondary, button.option, button.verdict { background: var(--surface-2); color: var(--ink); }
button.option { display: block; width: 100%; text-align: left; margin: 6px 0; }
button.option .detail { color: var(--muted); font-size: 13px; }
button:disabled { opacity: .5; cursor: default; }
.feedback .fbhead { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin-bottom: 10px; }
.feedback .fbhead h3 { margin: 0; }
.fbverdicts { gap: 10px; }
button.verdict { display: inline-flex; align-items: center; gap: 8px; padding: 10px 18px; border-radius: 999px;
                 font-weight: 600; transition: transform .12s ease, background-color .15s ease, border-color .15s ease; }
button.verdict .e { font-size: 18px; line-height: 1; }
button.verdict:hover { transform: translateY(-1px); border-color: var(--accent); }
button.verdict[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
button.verdict.pop { animation: fbpop .28s ease; }
@keyframes fbpop { 0% { transform: scale(1); } 45% { transform: scale(1.12); } 100% { transform: scale(1); } }
@media (prefers-reduced-motion: reduce) { button.verdict, button.verdict.pop { transition: none; animation: none; } }
.fblabel { margin: 14px 0 6px; } .fblabel span { opacity: .85; }
.fbchips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.fbchip { font: inherit; font-size: 13px; padding: 5px 12px; border-radius: 999px; cursor: pointer;
          border: 1px solid var(--border); background: var(--surface-2); color: var(--ink-2); }
.fbchip:hover { border-color: var(--accent); }
.fbchip[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
.colchip { font: inherit; font-size: 13px; padding: 4px 10px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface-2); color: var(--text); }
.colchip:hover { border-color: var(--accent); }
.colchip[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
.colchip[disabled] { cursor: default; opacity: .7; }
.colchip .why { opacity: .75; font-size: 12px; }
.jobhead { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.jobhead .grow { flex: 1; }
.steps { list-style: none; margin: 10px 0 0; padding: 0; }
.steps li { display: flex; gap: 8px; align-items: baseline; padding: 3px 0; font-size: 14px; }
.steps li .mark { width: 16px; flex: none; text-align: center; color: var(--muted); }
.steps li.done { color: var(--muted); }
.steps li.done .mark { color: var(--good, var(--accent)); }
.steps li.now { font-weight: 600; }
.steps li.now .spin { display: inline-block; vertical-align: -2px; }
.steps li .at { margin-left: auto; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.jobnote { font-size: 13px; color: var(--muted); margin-top: 8px; }
.conntable td { vertical-align: top; }
.connok { color: var(--good-ink); font-weight: 600; white-space: nowrap; }
.connbad { color: var(--bad); font-weight: 600; white-space: nowrap; }
.connwhy { color: var(--bad); font-size: 12.5px; margin-top: 2px; }
.conncheck { font-size: 12.5px; margin-top: 2px; }
.colpanel { border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; margin: 8px 0; }
.colpanel h4 { margin: 0 0 6px; font-size: 13px; font-weight: 600; }
.fbok { color: var(--good-ink); font-weight: 600; } .fberr { color: var(--bad); }
button.verdict.sm { padding: 4px 9px; gap: 0; } button.verdict.sm .e { font-size: 15px; }
.row.fbmini { gap: 4px; flex-wrap: nowrap; } td.hfb { min-width: 136px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.hopen { all: unset; cursor: pointer; color: var(--ink); }
.hopen::before { content: "▸ "; color: var(--muted); }
.hopen[aria-expanded="true"]::before { content: "▾ "; }
.hopen:hover, .hopen:focus-visible { text-decoration: underline; }
.hopen:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
tr.hrow.open td { border-bottom-color: transparent; }
tr.hdetail > td { background: var(--surface-2); }
.hdgrid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 18px; padding: 6px 2px; }
.hdgrid h4 { margin: 0 0 6px; font-size: 13px; }
.hdgrid pre { margin: 0 0 8px; max-height: 260px; overflow: auto; }
.hmore:has(.fbmore:not([hidden])) .hmorehint { display: none; }
@media (max-width: 860px) { .hdgrid { grid-template-columns: 1fr; } }
.muted { color: var(--muted); } .small { font-size: 13px; }
.q { font-weight: 600; font-size: 16px; }
.thread .turn { margin: 6px 0; color: var(--ink-2); font-size: 14px; }
.context { color: var(--ink-2); margin: 4px 0 8px; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; background: var(--surface-2);
        color: var(--ink-2); font-size: 12px; margin-right: 4px; }
.status { display: inline-flex; gap: 4px; align-items: center; font-size: 13px; font-weight: 600; }
.status.good { color: var(--good-ink); } .status.bad { color: var(--bad); } .status.mid { color: var(--ink-2); }
.status i { font-style: normal; width: 16px; height: 16px; border-radius: 50%; display: inline-grid;
            place-items: center; font-size: 11px; color: #fff; }
.status.good i { background: var(--good); } .status.bad i { background: var(--bad); }
.status.mid i { background: var(--warn); color: #0b0b0b; }
.tablewrap { overflow-x: auto; margin-top: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink-2); font-weight: 600; font-size: 13px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
details { margin-top: 10px; } summary { cursor: pointer; color: var(--ink-2); font-size: 14px; }
pre { background: var(--surface-2); padding: 10px; border-radius: 8px; overflow-x: auto; font-size: 12.5px; }
.feedback h3, .card h3 { margin: 0 0 8px; font-size: 15px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; margin-top: 10px; }
label.check { display: flex; gap: 6px; align-items: center; font-size: 14px; }
textarea { width: 100%; min-height: 60px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.kpi { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px; }
.kpi .v { font-size: 28px; font-weight: 650; } .kpi .l { color: var(--ink-2); font-size: 13px; }
.bar { position: relative; height: 12px; min-width: 120px; }
.bar span { position: absolute; left: 0; top: 0; bottom: 0; background: var(--bar); border-radius: 0 4px 4px 0; }
.sugg .kind { font-size: 12px; color: var(--ink-2); text-transform: uppercase; letter-spacing: .04em; }
.sugg .change { font-weight: 600; margin: 2px 0; }
/* source icons */
.ico { width: 16px; height: 16px; flex: none; vertical-align: -3px; fill: none; stroke: currentColor;
       stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; color: var(--ink-2); }
.pill .ico, .chip .ico { width: 13px; height: 13px; vertical-align: -2px; margin-right: 3px; }
.srcrow > span:first-of-type .ico { margin-right: 4px; }
/* SQL and Config tabs */
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; }
.sqlgrid { display: grid; grid-template-columns: minmax(300px, 340px) minmax(0, 1fr); gap: 14px; align-items: start; }
aside.tables { position: sticky; top: 76px; display: flex; flex-direction: column; max-height: calc(100vh - 96px); padding: 14px; }
aside.tables .thead { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
aside.tables .thead h3 { margin: 0; }
aside.tables .tlist { flex: 1; min-height: 120px; max-height: none; }
.tgroup { margin-top: 8px; }
.ghead { display: flex; align-items: center; gap: 6px; padding: 4px 2px; border-bottom: 1px solid var(--grid); position: sticky; top: 0;
         background: var(--surface); z-index: 1; }
.ghead .gname { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; color: var(--ink-2); }
.gacts { margin-left: auto; display: inline-flex; gap: 4px; align-items: center; }
.gacts .spin { width: 11px; height: 11px; }
.pillbtn { font: inherit; font-size: 11.5px; padding: 2px 9px; border-radius: 999px; border: 1px solid var(--border);
           background: var(--surface); color: var(--ink-2); cursor: pointer; white-space: nowrap; }
.pillbtn:hover { border-color: var(--accent); color: var(--accent); }
.pillbtn.on { background: var(--surface-2); color: var(--ink); }
.pillbtn.accent { border-color: var(--accent); color: var(--accent); font-weight: 600; }
.pillbtn.accent:hover { background: var(--accent); color: #fff; }
.pillbtn:disabled { opacity: .6; cursor: default; }
.iconbtn { background: none; border: 0; color: var(--muted); cursor: pointer; font-size: 13px; padding: 2px 5px; border-radius: 6px; }
.iconbtn:hover { color: var(--accent); background: var(--surface-2); }
.trow { display: flex; align-items: center; gap: 4px; border-radius: 8px; }
.trow:hover { background: var(--surface-2); }
.trow .titem { flex: 1; min-width: 0; }
.trow .titem:hover { background: none; }
.trow.saved { box-shadow: inset 3px 0 0 var(--accent); }
.titem .tname { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.titem .ktag, .titem .kcount { flex: none; font-size: 10.5px; padding: 0 6px; border-radius: 999px; background: var(--surface-2);
                               color: var(--muted); margin-left: 4px; }
.trow:hover .titem .ktag, .trow:hover .titem .kcount { background: var(--surface); }
.titem .kcount { margin-left: auto; }
.editbtn { flex: none; font: inherit; font-size: 12px; font-weight: 600; padding: 2px 9px; margin-right: 4px; border-radius: 999px;
           border: 1px solid var(--accent); background: var(--surface); color: var(--accent); cursor: pointer; white-space: nowrap; }
.editbtn:hover { background: var(--accent); color: #fff; }
.nested .nhead { display: flex; align-items: center; gap: 8px; justify-content: space-between; padding: 4px 4px 6px; }
.nested .okc { color: var(--good-ink); }
.nested .nnote { padding: 4px; }
.nitem.saved .lbl { color: var(--ink-2); }
.nitem.just { background: rgba(46,160,67,.10); animation: justsaved 1.6s ease-out 1; }
@keyframes justsaved { from { background: rgba(46,160,67,.32); } }
@media (prefers-reduced-motion: reduce) { .nitem.just { animation: none; } }
.savedok { flex: none; margin-left: auto; font-size: 11.5px; color: var(--good-ink); font-weight: 600; white-space: nowrap; overflow: hidden;
           text-overflow: ellipsis; max-width: 55%; }
button.mini { flex: none; font: inherit; font-size: 11.5px; padding: 1px 8px; border-radius: 6px; border: 1px solid var(--border);
              background: var(--surface); color: var(--ink-2); cursor: pointer; }
button.mini:hover { border-color: var(--accent); color: var(--accent); }
.nitem button.mini:first-of-type { margin-left: auto; }
.connbox .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; }
.connbox .dot.ok { background: var(--good-ink); } .connbox .dot.bad { background: var(--bad); }
.connbox details { margin-top: 4px; } .connbox summary { cursor: pointer; color: var(--bad); }
textarea.editor { width: 100%; min-height: 180px; resize: vertical; tab-size: 2; line-height: 1.45; }
#cfgtext { min-height: 520px; }
.tlist { max-height: 70vh; overflow: auto; margin-top: 8px; }
.tlist h4 { margin: 10px 0 4px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.titem { display: flex; gap: 6px; align-items: center; width: 100%; background: none; border: 0; padding: 4px 6px;
         border-radius: 6px; color: var(--ink); font: inherit; font-size: 13.5px; text-align: left; cursor: pointer; }
.titem:hover { background: var(--surface-2); }
.titem .kind { margin-left: auto; font-size: 11px; color: var(--muted); }
.enablebar { display: flex; gap: 12px; align-items: center; justify-content: center; flex-wrap: wrap; padding: 8px 16px;
  background: rgba(42,120,214,.09); border-bottom: 1px solid var(--border); font-size: 14px; }
.enablelink { font-weight: 650; color: var(--accent); text-decoration: none; }
.enablelink:hover { text-decoration: underline; }
#enablecard, #setupcard { scroll-margin-top: 90px; }
.enable ol { margin: 8px 0 0; padding-left: 20px; } .enable li { margin: 6px 0; }
.enable .ok { color: var(--good-ink); font-weight: 600; } .enable .todo { color: var(--bad); font-weight: 600; }
.nested { margin: 0 0 4px 14px; border-left: 2px solid var(--grid); padding-left: 6px; }
.nitem { display: flex; gap: 6px; align-items: center; padding: 2px 4px; border-radius: 6px; font-size: 13px; }
.nitem:hover { background: var(--surface-2); }
.nitem .lbl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.tlisttools { display: flex; gap: 10px; align-items: center; margin-top: 6px; }
.tlisttools .grow { flex: 1; }

.gtoggle { display: inline-flex; align-items: center; gap: 5px; background: none; border: 0; padding: 2px 4px 2px 0; cursor: pointer;
           font: inherit; color: inherit; text-transform: inherit; letter-spacing: inherit; }
.gtoggle:hover { color: var(--ink); }
.gtoggle .chev { display: inline-block; font-size: 9px; transition: transform .12s; }
.gtoggle[aria-expanded="true"] .chev { transform: rotate(90deg); }
.gtoggle .gcount { font-weight: 400; color: var(--muted); letter-spacing: 0; }
.trow { display: flex; align-items: center; }
.trow .titem { flex: 1; min-width: 0; }
.tedit { background: none; border: 0; color: var(--muted); cursor: pointer; padding: 2px 6px; border-radius: 6px; font-size: 13px; }
.tedit:hover { color: var(--accent); background: var(--surface-2); }
.tlist h4 .expsvc { float: right; display: inline-flex; gap: 6px; align-items: center; text-transform: none; letter-spacing: 0; font-weight: 400; }
.tlist h4 .expsvc .spin { width: 11px; height: 11px; }
button.linkish { background: none; border: 0; padding: 0; font: inherit; font-size: 12px; color: var(--accent); cursor: pointer; }
button.linkish:hover { text-decoration: underline; }
button.linkish[aria-pressed="true"] { color: var(--ink-2); }
.jcell { font-size: 12px; color: var(--ink-2); white-space: pre-wrap; word-break: break-word; }

.connbox { font-size: 12.5px; margin: 0 0 8px; padding: 8px 10px; border-radius: 8px; background: var(--surface-2); }
.connbox.bad { background: rgba(208,59,59,.08); }
.connbox ul { margin: 6px 0 0; padding-left: 16px; } .connbox li { margin: 2px 0; }
.connbox .err { color: var(--bad); word-break: break-word; }
.log { max-height: 240px; overflow: auto; white-space: pre-wrap; }
.msg { font-size: 13.5px; margin: 6px 0 0; } .msg.bad { color: var(--bad); } .msg.good { color: var(--good-ink); }
.msg.warn { color: var(--ink-2); }
.opt { border-bottom: 1px solid var(--grid); padding: 6px 0; font-size: 13.5px; }
.opt .n { font-weight: 600; } .opt .t { color: var(--muted); font-size: 12px; margin-left: 6px; }
.opt .d { color: var(--ink-2); font-size: 13px; }
.opt .def { color: var(--muted); font-size: 12px; }
.optref details > summary { font-weight: 600; color: var(--ink); padding: 4px 0; }
.optref details details { margin-left: 12px; }
.badge { font-size: 11px; padding: 0 6px; border-radius: 999px; border: 1px solid var(--border); color: var(--ink-2); margin-left: 4px; }
@media (max-width: 860px) { .sqlgrid { grid-template-columns: 1fr; } aside.tables { position: static; max-height: none; } }
/* Config form */
.catcard { margin-bottom: 14px; }
.catbar { gap: 10px; align-items: center; }
.catprog { gap: 10px; align-items: center; margin-top: 12px; } .catprog progress { flex: 1; min-width: 120px; height: 8px; }
.catlog { max-height: 260px; overflow: auto; background: var(--surface-2); border: 1px solid var(--border);
          border-radius: 8px; padding: 10px 12px; margin: 8px 0 0; font-size: 12.5px; line-height: 1.5; white-space: pre-wrap; }
.spin { width: 14px; height: 14px; border-radius: 50%; border: 2px solid var(--border); border-top-color: var(--accent);
        animation: spin .8s linear infinite; flex: none; }
.spin[hidden] { display: none; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
#catlist table td .mono { font-size: 12.5px; }
.cfghead { display: flex; gap: 10px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
.cfghead h3 { margin: 0; }
.cfgbar { position: sticky; bottom: 0; background: var(--surface); padding: 10px 0 4px; margin-top: 8px;
          border-top: 1px solid var(--grid); z-index: 1; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.seg button { font: inherit; font-size: 13px; border: 0; background: none; color: var(--ink-2); padding: 5px 12px; cursor: pointer; }
.seg button[aria-pressed="true"] { background: var(--surface-2); color: var(--ink); font-weight: 600; }
#cfgform h4 { margin: 18px 0 6px; font-size: 14px; display: flex; gap: 8px; align-items: center; }
#cfgform h4:first-child { margin-top: 4px; }
#cfgform h4 .muted { font-weight: 400; font-size: 12.5px; }
.fgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px 12px; }
.fld { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.fld label { font-size: 12.5px; font-weight: 600; color: var(--ink-2); }
.fld .req { color: var(--bad); }
.fld input, .fld select, .fld textarea { width: 100%; padding: 6px 8px; font-size: 13.5px; }
.fld .hint { font-size: 12px; color: var(--muted); line-height: 1.35; }
.fld.wide { grid-column: 1 / -1; }
textarea.fjson { min-height: 58px; resize: vertical; }
.invalid { border-color: var(--bad) !important; outline-color: var(--bad); }
.entry { border: 1px solid var(--border); border-radius: 10px; padding: 12px; margin: 8px 0; background: var(--surface); }
.entry .head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
.entry .head .name { font-weight: 600; width: 180px; }
.entry .head .grow { flex: 1; }
.entry .sub { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--grid); }
.entry .sub > .t { font-size: 12.5px; font-weight: 600; color: var(--ink-2); margin-bottom: 6px; }
.kv { display: grid; grid-template-columns: minmax(120px, 200px) minmax(0, 1fr) auto; gap: 6px; align-items: center; margin: 4px 0; }
.kv input { padding: 6px 8px; font-size: 13.5px; width: 100%; }
button.x { background: none; border: 1px solid var(--border); border-radius: 6px; color: var(--ink-2); cursor: pointer;
           font: inherit; font-size: 12.5px; padding: 3px 8px; }
button.x:hover { color: var(--bad); border-color: var(--bad); }
button.add { background: none; border: 1px dashed var(--border); border-radius: 6px; color: var(--accent); cursor: pointer;
             font: inherit; font-size: 12.5px; padding: 3px 9px; margin: 4px 4px 0 0; }
#cfgform details.sect { margin: 6px 0; border: 1px solid var(--border); border-radius: 10px; padding: 8px 12px; }
#cfgform details.sect > summary { font-weight: 600; color: var(--ink); }
#cfgform details.sect > .hint { margin: 4px 0 8px; }
/* Config: a page per kind of setting */
#tab-config { overflow-x: clip; }
.cfglayout { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 16px; align-items: start; }
.cfgnav { position: sticky; top: 76px; display: flex; flex-direction: column; gap: 1px; padding: 8px;
          background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
.cfgnav .navgroup { font-size: 11px; font-weight: 650; color: var(--muted); text-transform: uppercase; letter-spacing: .05em;
                    padding: 10px 10px 4px; }
.cfgnav .navgroup:first-child { padding-top: 4px; }
.cfgnav button { display: flex; align-items: center; gap: 6px; background: none; border: 0; border-radius: 8px; padding: 6px 10px;
                 font: inherit; font-size: 14px; color: var(--ink-2); text-align: left; cursor: pointer; }
.cfgnav button:hover { background: var(--surface-2); color: var(--ink); }
.cfgnav button[aria-current="page"] { background: var(--surface-2); color: var(--ink); font-weight: 600;
                                      box-shadow: inset 3px 0 0 var(--accent); }
.cfgnav .count { margin-left: auto; font-size: 11.5px; color: var(--muted); font-weight: 400; }
.cfgnav .count.bad { color: var(--bad); font-weight: 600; }
.cfgmain > * + *, .cfgpage > * + * { margin-top: 14px; }
.cfgmain a { color: var(--accent); }
.cfgmain .card { margin-bottom: 0; }
.cfgintro { color: var(--ink-2); font-size: 14px; margin: 6px 0 4px; max-width: 72ch; }
.dirty { color: var(--accent); font-size: 13px; font-weight: 600; margin-left: auto; }
@media (max-width: 860px) {
  .cfglayout { grid-template-columns: 1fr; }
  .cfgnav { position: static; flex-direction: row; flex-wrap: wrap; }
  .cfgnav .navgroup { width: 100%; }
}
/* overview */
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; }
.tile { border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; background: var(--surface); text-align: left;
        font: inherit; color: var(--ink); cursor: pointer; display: flex; flex-direction: column; gap: 2px; }
.tile:hover { border-color: var(--accent); }
.tile .k { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.tile .v { font-size: 22px; font-weight: 650; }
.tile .s { font-size: 12.5px; color: var(--ink-2); }
.tile .s.bad { color: var(--bad); }
.problems { margin: 12px 0 0; padding: 0; list-style: none; }
.problems li { padding: 8px 10px; border-radius: 8px; background: rgba(208,59,59,.07); margin: 6px 0; font-size: 13.5px; }
.problems li .err { color: var(--bad); word-break: break-word; }
/* groups of settings on a page */
.group { border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; margin: 12px 0; background: var(--surface); }
.group > .gt { font-weight: 650; font-size: 14.5px; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.group > .gd { color: var(--muted); font-size: 12.5px; margin: 2px 0 10px; }
.fld label .key { font-weight: 400; font-size: 11px; color: var(--muted); margin-left: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
/* collapsible cards: connectors, AI providers */
details.ccard { border: 1px solid var(--border); border-radius: 12px; margin: 10px 0; background: var(--surface); overflow: hidden; }
details.ccard[open] { box-shadow: 0 4px 16px rgba(0,0,0,.06); }
details.ccard > summary { list-style: none; display: flex; gap: 10px; align-items: center; padding: 12px 14px; cursor: pointer; }
details.ccard > summary::-webkit-details-marker { display: none; }
details.ccard > summary:hover { background: var(--surface-2); }
details.ccard > summary .ico { width: 22px; height: 22px; color: var(--accent); }
details.ccard > summary .who { display: flex; flex-direction: column; min-width: 0; }
details.ccard > summary .who b { font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
details.ccard > summary .who span { font-size: 12.5px; color: var(--muted); }
details.ccard > summary .grow { flex: 1; }
details.ccard > summary .chev { color: var(--muted); transition: transform .15s; font-size: 12px; }
details.ccard[open] > summary .chev { transform: rotate(90deg); }
details.ccard > .body { padding: 4px 14px 14px; border-top: 1px solid var(--grid); }
.status { font-size: 12px; padding: 2px 9px; border-radius: 999px; border: 1px solid var(--border); white-space: nowrap; }
.status.ok { color: var(--good-ink); border-color: currentColor; }
.status.bad { color: var(--bad); border-color: currentColor; }
.status.new { color: var(--ink-2); border-style: dashed; }
.ccard .sec { margin-top: 14px; }
.ccard .sec > .st { font-size: 12px; font-weight: 650; color: var(--ink-2); text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px; }
.ccard .sec > .sd { font-size: 12.5px; color: var(--muted); margin: -2px 0 8px; }
.ccard details.adv { margin-top: 14px; }
.ccard details.adv > summary { font-size: 12px; font-weight: 650; color: var(--ink-2); text-transform: uppercase; letter-spacing: .04em; cursor: pointer; }
.ccard .errbox { margin-top: 10px; padding: 8px 10px; border-radius: 8px; background: rgba(208,59,59,.07); font-size: 13px; }
.ccard .errbox .err { color: var(--bad); word-break: break-word; }
.ccard .foot { display: flex; justify-content: flex-end; margin-top: 14px; }
button.danger { background: none; border: 1px solid var(--border); border-radius: 8px; color: var(--bad); cursor: pointer; font: inherit; font-size: 13px; padding: 5px 12px; }
button.danger:hover { border-color: var(--bad); }
.listtools { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 4px 0 0; }
.listtools .grow { flex: 1; }
/* add a connector: pick its kind */
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; margin-top: 10px; }
.gallery button { display: flex; gap: 10px; align-items: flex-start; text-align: left; border: 1px solid var(--border); border-radius: 10px;
                  background: var(--surface); padding: 10px 12px; font: inherit; color: var(--ink); cursor: pointer; }
.gallery button:hover { border-color: var(--accent); }
.gallery button[aria-pressed="true"] { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(42,120,214,.2); }
.gallery button .ico { width: 20px; height: 20px; color: var(--accent); margin-top: 2px; }
.gallery button b { display: block; font-size: 14px; }
.gallery button span.d { display: block; font-size: 12px; color: var(--muted); line-height: 1.35; }
.addbox { border: 1px dashed var(--border); border-radius: 12px; padding: 12px 14px; margin: 12px 0; }
.savedtag { font-size: 10.5px; padding: 0 6px; border-radius: 999px; border: 1px solid var(--accent); color: var(--accent); margin-left: 4px; }
.modal .vkind { background: var(--surface-2); border-radius: 10px; padding: 10px 12px; margin: 8px 0 14px; font-size: 13.5px; }
.modal .vkind div { color: var(--ink-2); font-size: 13px; margin-top: 2px; }
.modal .vkind .bad { color: var(--bad); }
.modal textarea.vsql { width: 100%; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12.5px;
               min-height: 84px; max-height: 40vh; resize: vertical; background: var(--surface); color: var(--ink); line-height: 1.45; }
/* result viewer */
dialog.viewer { width: calc(100vw - 32px); height: calc(100vh - 32px); max-width: none; max-height: none; padding: 0;
                border: 1px solid var(--border); border-radius: 14px; background: var(--surface); color: var(--ink);
                box-shadow: 0 24px 60px rgba(0,0,0,.25); overflow: hidden; }
dialog.viewer[open] { display: flex; flex-direction: column; }
dialog.viewer::backdrop { background: rgba(10,10,10,.45); }
.vwhead { display: flex; gap: 10px; align-items: center; padding: 12px 56px 12px 16px; border-bottom: 1px solid var(--grid);
          flex-wrap: wrap; position: relative; }
#vwclose { position: absolute; top: 14px; right: 14px; }
.vwtitle { display: flex; flex-direction: column; min-width: 0; flex: 1 1 220px; }
.vwtitle h2 { margin: 0; font-size: 16px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#vwsearch { width: min(260px, 100%); padding: 6px 10px; }
.vwhead > button, .vwhead > .vwcols > summary { white-space: nowrap; }
.vwcols { position: relative; }
.vwcols > summary { list-style: none; cursor: pointer; font-size: 13.5px; padding: 6px 12px; border: 1px solid var(--border); border-radius: 8px; }
.vwcols > summary::-webkit-details-marker { display: none; }
.vwcolpanel { position: absolute; right: 0; top: calc(100% + 6px); z-index: 3; background: var(--surface); border: 1px solid var(--border);
              border-radius: 10px; padding: 10px 12px; width: 260px; max-height: 50vh; overflow: auto; box-shadow: 0 10px 30px rgba(0,0,0,.15); }
.vwcolpanel label { display: flex; gap: 6px; align-items: center; font-size: 13px; padding: 2px 0; }
.vwmain { flex: 1; min-height: 0; display: flex; }
.vwgrid { flex: 1; min-width: 0; overflow: auto; }
table.vwtable { width: max-content; min-width: 100%; font-size: 13.5px; }
table.vwtable th { position: sticky; top: 0; z-index: 1; background: var(--surface); box-shadow: inset 0 -1px 0 var(--border);
                   cursor: pointer; user-select: none; white-space: nowrap; }
table.vwtable th:hover { color: var(--ink); }
table.vwtable th .arrow { color: var(--accent); margin-left: 4px; }
table.vwtable td { max-width: 360px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }
table.vwtable.wrap td { white-space: pre-wrap; word-break: break-word; max-width: 480px; }
table.vwtable td:hover { background: var(--surface-2); }
table.vwtable td.sel { outline: 2px solid var(--accent); outline-offset: -2px; }
table.vwtable .rn { width: 1%; white-space: nowrap; position: sticky; left: 0; background: var(--surface); color: var(--muted); font-size: 12px; text-align: right;
                    cursor: default; z-index: 0; }
table.vwtable th.rn { z-index: 2; }
table.vwtable tbody tr:hover td.rn { background: var(--surface-2); }
.vwcell { width: min(420px, 40vw); border-left: 1px solid var(--grid); padding: 12px 14px; overflow: auto; display: flex; flex-direction: column; gap: 6px; }
.vwcell pre { margin: 6px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12.5px; background: var(--surface-2);
              border-radius: 8px; padding: 10px; flex: 1; overflow: auto; }
.vwcell .grow, .vwfoot .grow { flex: 1; }
.vwfoot { display: flex; gap: 10px; align-items: center; padding: 10px 16px; border-top: 1px solid var(--grid); flex-wrap: wrap; }
.vwempty { padding: 40px; text-align: center; color: var(--muted); }
.resbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.resbar .grow { flex: 1; }
.sqlres .tablewrap { max-height: 480px; overflow: auto; }
.sqlres .tablewrap th { position: sticky; top: 0; background: var(--surface); box-shadow: inset 0 -1px 0 var(--border); }
@media (max-width: 700px) { .vwcell { position: absolute; inset: auto 0 0 0; width: auto; height: 45%; background: var(--surface);
                                      border-left: 0; border-top: 1px solid var(--border); } .vwmain { position: relative; } }
/* take-over dialog */
dialog.modal { border: 1px solid var(--border); border-radius: 14px; padding: 0; width: min(560px, calc(100vw - 32px));
               background: var(--surface); color: var(--ink); box-shadow: 0 24px 60px rgba(0,0,0,.25); }
dialog.modal::backdrop { background: rgba(10,10,10,.45); backdrop-filter: blur(2px); }
dialog.modal[open] { animation: pop .18s ease-out both; }
.modal .mhead { display: flex; gap: 12px; align-items: flex-start; padding: 20px 22px 6px; }
.modal .mhead .ico { width: 28px; height: 28px; color: var(--accent); flex: none; margin-top: 2px; }
.modal h2 { margin: 0; font-size: 18px; }
.modal .sub { margin: 4px 0 0; color: var(--ink-2); font-size: 14px; }
.modal .mbody { padding: 10px 22px 4px; }
.modal .facts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 10px 0 14px; }
.modal .fact { background: var(--surface-2); border-radius: 10px; padding: 10px 12px; }
.modal .fact .v { font-size: 20px; font-weight: 650; } .modal .fact .l { font-size: 12px; color: var(--ink-2); }
.modal label.name { display: block; font-size: 13px; font-weight: 600; color: var(--ink-2); margin-bottom: 4px; }
.modal .namebox { display: flex; align-items: center; border: 1px solid var(--border); border-radius: 8px;
                  background: var(--surface); padding-left: 10px; }
.modal .namebox:focus-within { outline: 2px solid var(--accent); outline-offset: 1px; }
.modal .namebox span { color: var(--muted); font-size: 13px; white-space: nowrap; }
.modal .namebox input { border: 0; outline: 0; flex: 1; min-width: 0; padding: 9px 10px 9px 4px; background: none; }
.modal .hint { font-size: 12.5px; color: var(--muted); margin-top: 5px; min-height: 18px; }
.modal .hint.bad { color: var(--bad); }
.modal .cols { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0 4px; max-height: 96px; overflow: auto; }
.modal .cols span { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: var(--surface-2); color: var(--ink-2); }
.modal .capped { margin-top: 12px; padding: 10px 12px; border-radius: 10px; border: 1px dashed var(--border); }
.modal .err { color: var(--bad); font-size: 13.5px; margin: 10px 0 0; }
.modal .mfoot { display: flex; gap: 8px; justify-content: flex-end; padding: 16px 22px 18px; flex-wrap: wrap; }
.modal .mfoot .note { flex-basis: 100%; font-size: 12.5px; color: var(--muted); margin: 0 0 2px; }
@media (max-width: 520px) { .modal .facts { grid-template-columns: 1fr 1fr; } }
.toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: var(--ink);
         color: var(--page); padding: 8px 14px; border-radius: 8px; font-size: 14px; opacity: 0;
         transition: opacity .2s; pointer-events: none; }
.toast.show { opacity: 1; }
.error { color: var(--bad); }
/* while typing: what the question seems to be about */
.chips { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.chips:empty { margin-top: 0; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 11px; border-radius: 999px;
        border: 1px solid var(--border); background: var(--surface-2); color: var(--ink); font: inherit;
        font-size: 13px; cursor: pointer; animation: pop .22s ease-out both; }
.chip:disabled { cursor: default; }
.chip .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); flex: none; }
.chip.mine .dot { background: var(--good); }
.chip .k { color: var(--ink-2); }
.chip[aria-expanded="true"] { outline: 2px solid var(--accent); }
.chip.thinking { color: var(--muted); border-style: dashed; }
.chip.thinking .dot { background: var(--muted); animation: blink 1s ease-in-out infinite; }
@keyframes pop { from { opacity: 0; transform: translateY(4px) scale(.96); } to { opacity: 1; transform: none; } }
@keyframes blink { 50% { opacity: .3; } }
.panel { margin-top: 10px; border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px;
         background: var(--surface); animation: pop .18s ease-out both; }
.panel h4 { margin: 10px 0 4px; font-size: 14px; display: flex; gap: 8px; align-items: center; }
.panel h4:first-child { margin-top: 0; }
.panel h4 .label { color: var(--muted); font-weight: 400; font-size: 12px; }
.srcrow { display: grid; grid-template-columns: auto 1fr auto; gap: 4px 10px; align-items: center;
          padding: 5px 0 5px 22px; font-size: 14px; }
.srcrow .desc { grid-column: 2; color: var(--muted); font-size: 12.5px; margin-top: -2px; }
.tag { font-size: 11px; padding: 0 7px; border-radius: 999px; border: 1px solid var(--accent); color: var(--accent); }
.rel { width: 64px; height: 6px; background: var(--grid); border-radius: 3px; position: relative; }
.rel span { position: absolute; inset: 0 auto 0 0; background: var(--bar); border-radius: 0 3px 3px 0; }
.panel .foot { display: flex; gap: 8px; justify-content: flex-end; margin-top: 10px; flex-wrap: wrap; }
@media (prefers-reduced-motion: reduce) { .chip, .panel { animation: none; } .chip.thinking .dot { animation: none; } }
@media (max-width: 600px) { .user { margin-left: 0; width: 100%; } .ask { flex-direction: column; } }
</style>
</head>
<body>
<header>
  <h1>Duckduck Ask</h1>
  <nav role="tablist">
    <button role="tab" data-tab="ask" aria-selected="true">Ask</button>
    <button role="tab" data-tab="sql" aria-selected="false">SQL</button>
    <button role="tab" data-tab="history" aria-selected="false">History</button>
    <button role="tab" data-tab="dashboard" aria-selected="false">Dashboard</button>
    <button role="tab" data-tab="suggestions" aria-selected="false">Suggestions</button>
    <button role="tab" data-tab="config" aria-selected="false" hidden>Config</button>
  </nav>
  <div class="user"><label for="user">You</label><input id="user" placeholder="your name (optional)"></div>
</header>
<div class="enablebar" id="enablebar" hidden>
  <span><b>Ask in plain language is off</b> — there's no semantic catalog yet. SQL works meanwhile.</span>
  <a href="#enable-ask" class="enablelink" id="enablelink">Enable it →</a>
</div>
<main>
  <section id="tab-ask">
    <div class="card setupcard" id="setupcard" hidden>
      <h3 style="margin:0 0 6px">Ask needs a semantic catalog first</h3>
      <p class="context" style="margin:0 0 6px">It's what your tables and fields mean — how a question in plain language
        finds the right table and field. <span class="muted small mono" id="setuppath"></span></p>
      <p style="margin:0 0 10px">Your systems are connected already: the <b>SQL</b> tab queries every table now.
        <span id="setuphow"></span></p>
      <div class="row"><button class="primary" type="button" id="setupsql">Open the SQL tab</button>
        <button class="secondary" type="button" id="setupconfig">Set up the catalog</button></div>
    </div>
    <div class="card">
      <form class="ask" id="askform">
        <div class="seg reader" role="group" aria-label="How the question is read">
          <button type="button" data-reader="auto" aria-pressed="false"
            title="Auto: picks Paddle, Dive or Fly per question from how similar questions went (click ? for more)">🧭 Auto</button>
          <button type="button" data-reader="rules" aria-pressed="true"
            title="Paddle: fast, predictable, no LLM — reads the wording (click ? for more)">🦆 Paddle</button>
          <button type="button" data-reader="llm" aria-pressed="false"
            title="Dive: an LLM reads what you meant first — better with free phrasing, slower (click ? for more)">🤿 Dive</button>
          <button type="button" data-reader="llm_decides" aria-pressed="false"
            title="Fly: the LLM reads the question and makes every decision — no Jev (click ? for more)">🪽 Fly</button>
        </div>
        <button type="button" class="help" id="readerhelpbtn" aria-expanded="false" aria-controls="readerhelp"
          title="What do Paddle and Dive do?">?</button>
        <input id="question" placeholder="Ask about your data — e.g. Which hosts have critical alerts?" autocomplete="off">
        <select id="sample" aria-label="Rows to show first" title="How many rows an answer shows at first — a quick sample. You can get every row afterwards.">
          <option value="10">10 rows</option><option value="50">50 rows</option><option value="100" selected>100 rows</option>
          <option value="500">500 rows</option><option value="1000">1,000 rows</option><option value="all">All rows</option>
        </select>
        <button class="primary" type="submit" id="askbtn">Ask</button>
      </form>
      <div class="panel readerhelp" id="readerhelp" hidden>
        <div class="modes">
          <section>
            <h4>🧭 Auto <span class="label">picks one of the three</span></h4>
            <p><b>How it picks.</b> Looks up questions like yours in the history and how each mode did on them — a
              run counts as a success unless someone rated it <i>not answered</i> (<i>partly</i> counts half). Jev
              weighs that per mode (success rate, how often it asked back, time, calls) and picks; when it isn't sure,
              Auto uses Paddle.</p>
            <p><b>Good at.</b> Paying for Dive or Fly only where they did better before; it learns as you use the
              others and rate answers.</p>
            <p><b>Limits.</b> One extra Jev question per question. With little history it has little to go on — and
              it only knows modes that were used on similar questions, so try Dive and Fly now and then.</p>
          </section>
          <section>
            <h4>🦆 Paddle <span class="label">rules + Jev</span></h4>
            <p><b>How it reads.</b> Skims the words on the surface: wording rules spot what you ask for ("how many",
              "per", "the different…", "show me table…"), and the decision engine (Jev) settles only what the wording
              leaves open.</p>
            <p><b>Good at.</b> Fast and cheap: no LLM call — only Jev, one small batch per pause while you type
              and a few more when you ask (Jev is very cheap, not free). Predictable: the wording is always read the
              same way. With the offline engine instead of Jev, nothing leaves your machine and nothing is paid.</p>
            <p><b>Limits.</b> It only knows the phrasings it has rules for. Unusual wording may come back as a plain
              list, or it asks you what you meant. Other languages depend on the translation step, if one is
              configured.</p>
          </section>
          <section>
            <h4>🤿 Dive <span class="label">LLM reads first + Jev</span></h4>
            <p><b>How it reads.</b> Goes under the surface for what you meant: an LLM reads the question (the kind of
              answer, what it's about, the grouping) using only names from your catalog. Jev then decides, weighing
              that reading against the rules'; a doubt is still asked back, never guessed.</p>
            <p><b>Good at.</b> Free phrasing, other languages, indirect questions ("the departments I have"), and
              picking the field or table you mean.</p>
            <p><b>Limits.</b> Slower and costs more: an LLM call per pause while typing (usually a second or two; the
              reading is kept for 5 minutes, so asking doesn't call it again) — plus the same Jev calls as Paddle.
              Needs an LLM configured. It can still misread: the catalog and Jev check it, and <i>How it was
              decided</i> shows what it read.</p>
          </section>
          <section>
            <h4>🪽 Fly <span class="label">the LLM reads and decides</span></h4>
            <p><b>How it reads.</b> Flies the whole route alone: the LLM reads the question as in Dive, then makes
              every decision Jev would — what it's about, which tables, which fields, the joins, the kind of answer —
              each with a probability. The same safety rails: a doubt is asked back, and SQL is only ever written by
              the compiler from a checked plan.</p>
            <p><b>Good at.</b> Questions that need judgment and context more than calibrated yes/no answers; trying
              whether the LLM alone decides better than Jev on your data.</p>
            <p><b>Limits.</b> The slowest and priciest: the reading plus one LLM call per batch of decisions (usually
              two or three per question, and a batch per pause while typing). Its probabilities are less calibrated
              than Jev's, so thresholds tuned for Jev may ask back more or less often. Needs an LLM configured.</p>
          </section>
        </div>
        <p class="muted small" style="margin:10px 0 0">Tip: Paddle for everyday questions, Dive when Paddle misreads
          one, Fly to see if the LLM alone does better. Every search records its mode, time and calls — compare them
          on the Dashboard, filter the History by mode, or evaluate every mode on your rated questions (Suggestions).
          Your choice is remembered in this browser.</p>
      </div>
      <div class="chips" id="chips" aria-live="polite"></div>
      <div class="panel" id="chippanel" hidden></div>
    </div>
    <div id="conversation"></div>
  </section>
  <section id="tab-history" hidden><div class="card">
    <div class="row" style="margin-bottom:8px"><label class="small muted" for="historymode">Mode</label>
      <select id="historymode"><option value="">all</option><option value="rules">🦆 Paddle</option>
        <option value="llm">🤿 Dive</option><option value="llm_decides">🪽 Fly</option></select>
      <span class="muted small">🧭→ marks the ones Auto picked</span></div>
    <div id="history"></div></div></section>
  <section id="tab-dashboard" hidden><div id="dashboard"></div></section>
  <section id="tab-sql" hidden>
    <div class="card" id="sqloff" hidden><h3>The SQL console is off</h3>
      <p class="muted">This server was started with <span class="mono">--no-sql</span>
        (<span class="mono">serve(allow_sql=False)</span>). Restart it without that flag to query the registered
        tables here — read queries only, with no file or network access.</p></div>
    <div class="sqlgrid" id="sqlon">
      <aside class="card tables"><div class="thead"><h3>Tables</h3><span class="muted small" id="tablecount"></span></div>
        <div id="connstatus"></div>
        <input id="tablefilter" type="search" placeholder="Filter tables…" style="width:100%" aria-label="Filter tables">
        <div class="tlisttools"><span class="muted small">Connectors</span><span class="grow"></span>
          <button type="button" class="linkish" id="groupsopen" title="Show every connector's tables">Expand all</button>
          <button type="button" class="linkish" id="groupsclose" title="Show only the connectors' names">Collapse all</button></div>
        <div class="tlist" id="tablelist"></div></aside>
      <div>
        <div class="card">
          <textarea class="editor mono" id="sqltext" spellcheck="false" aria-label="SQL">SHOW TABLES</textarea>
          <div class="row" style="margin-top:8px"><button class="primary" id="sqlrun">Run</button>
            <button class="secondary" type="button" id="sqlsave" title="Keep this query as a table with a name — in duckduck.json, for SQL and Ask">Save as table</button>
            <label class="check small" title="Log at DEBUG: request bodies, bound parameters, every page (secrets stay masked)">
              <input type="checkbox" id="sqldebug"> Debug log</label>
            <span class="muted small">Ctrl+Enter · read queries only (SELECT, WITH, SHOW, DESCRIBE…) · no file or network access</span></div>
        </div>
        <div id="sqlresult"></div>
      </div>
    </div>
  </section>
  <section id="tab-config" hidden>
    <div class="cfglayout">
      <aside class="cfgnav" id="cfgnav" role="navigation" aria-label="Configuration pages">
        <div class="navgroup">Start</div>
        <button type="button" data-cfgpage="overview">Overview</button>
        <div class="navgroup">Connect</div>
        <button type="button" data-cfgpage="connectors">Connectors <span class="count" id="navc-connectors"></span></button>
        <button type="button" data-cfgpage="ai">AI providers <span class="count" id="navc-ai"></span></button>
        <button type="button" data-cfgpage="saved">Saved tables <span class="count" id="navc-saved"></span></button>
        <div class="navgroup">Ask in plain language</div>
        <button type="button" data-cfgpage="catalog">Semantic catalog</button>
        <button type="button" data-cfgpage="connections">Catalog connections</button>
        <button type="button" data-cfgpage="reading">How questions are read</button>
        <button type="button" data-cfgpage="answers">Answers &amp; data</button>
        <button type="button" data-cfgpage="drafting">Catalog drafting</button>
        <button type="button" data-cfgpage="thresholds">Confidence thresholds</button>
        <button type="button" data-cfgpage="learning">Feedback &amp; learning</button>
        <div class="navgroup">Advanced</div>
        <button type="button" data-cfgpage="json">duckduck.json</button>
        <button type="button" data-cfgpage="reference">Every option</button>
      </aside>
      <div class="cfgmain">
        <div class="cfgpage" data-page="overview">
        <div class="card enable" id="enablecard" hidden>
          <h3 style="margin:0">Enable Ask</h3>
          <p class="muted small" style="margin:4px 0 0">Ask reads a semantic catalog: what each table and field means. Three steps:</p>
          <ol id="enablesteps"></ol>
        </div>
          <div id="cfgoverview"></div>
        </div>
        <div class="cfgpage" data-page="catalog" hidden>
        <div class="card catcard" id="catcard">
          <div class="cfghead"><h3>Semantic catalog <span class="muted small mono" id="catpath"></span></h3>
            <span class="muted small" id="catllm"></span></div>
          <p class="muted small" style="margin:6px 0 10px">What questions are answered from: each table, its fields and what they mean.
            The LLM drafts it from the columns and a few sample rows; tables you wrote by hand and your <em>notes</em> are kept.</p>
          <div class="row catbar">
            <button class="primary" type="button" id="catupdate" title="Draft the tables that are new or older than max_age">Update catalog</button>
            <button class="secondary" type="button" id="catall" title="Draft every generated table again (one LLM call each)">Redraft all generated</button>
            <span class="small" id="catnote"></span></div>
          <div id="catjob" hidden>
            <div class="row catprog"><span class="spin" id="catspin" aria-hidden="true"></span><strong id="catstate"></strong>
              <progress id="catbar" max="1" value="0"></progress><span class="muted small" id="catelapsed"></span></div>
            <pre class="catlog mono" id="catlog" aria-live="polite"></pre>
            <div id="catresult"></div>
          </div>
          <details id="cattables"><summary id="catcount">Tables</summary><div id="catlist"></div></details>
        </div>
        </div>
        <div class="cfgpage" data-page="connections" hidden>
        <div class="card" id="conncard">
          <div class="cfghead"><h3>Catalog connections</h3><span class="muted small" id="conncount"></span></div>
          <p class="muted small" style="margin:6px 0 10px">Each table of the catalog and whether its system is connected.
            A table that isn't can't be used in answers until the reason below is fixed. <em>Test</em> reads one row now.</p>
          <div class="row"><button class="secondary" type="button" id="conntestall" title="Read one row of every connected table, one after another">Test all</button>
            <span class="small muted" id="connnote"></span></div>
          <div id="connlist"></div>
        </div>
        </div>
        <div class="cfgpage" data-page="reference" hidden>
          <div class="card optref"><h3>Every option</h3>
            <p class="muted small" style="margin:4px 0 10px">Everything duckduck.json can hold, with its type and default.</p>
            <input id="optfilter" placeholder="search options" style="width:100%">
            <div id="optref" style="margin-top:8px"></div></div>
        </div>
        <div class="card" id="cfgeditor">
          <div class="cfghead"><h3 id="cfgtitle">Settings</h3><span class="muted small mono" id="cfgpath"></span></div>
          <p class="cfgintro" id="cfgintro"></p>
          <p class="muted small" id="cfgnote"></p>
          <div id="cfgform"></div>
          <textarea class="editor mono" id="cfgtext" spellcheck="false" aria-label="duckduck.json" hidden></textarea>
          <div class="row cfgbar"><button class="secondary" id="cfgvalidate">Validate</button>
            <button class="primary" id="cfgsave">Save and reload</button>
            <button class="secondary" id="cfgreset">Discard changes</button>
            <span class="dirty" id="cfgdirty" hidden>● Unsaved changes</span></div>
          <div id="cfgmsgs"></div>
        </div>
      </div>
    </div>
  </section>
  <section id="tab-suggestions" hidden>
    <div class="card">
      <h3>Evaluation</h3>
      <p class="muted small">Replays every rated question (planning only, no data read) and suggests decision
        thresholds from what users said. <a href="#" id="evaljson">Download the evaluation set</a>.</p>
      <div class="row"><button class="secondary" id="evalbtn">Evaluate and calibrate</button>
        <label class="check"><input type="checkbox" id="evalmodes"> also compare the modes (Paddle, Dive, Fly) on the
          same questions — Dive and Fly call the LLM for every question</label></div>
      <div id="evaluation"></div>
    </div>
    <div class="card">
      <h3>For developers</h3>
      <p class="muted small">What suggestions can't fix — questions users still rate as not answered, with what the
        system did and what they expected — as a document to hand to whoever changes the code.</p>
      <div class="row"><button class="secondary" id="exportbtn">Download the brief</button>
        <label class="check"><input type="checkbox" id="exportredact"> hide the questions' values and the SQL</label></div>
    </div>
    <div class="row" style="margin: 4px 0 10px"><label class="check"><input type="checkbox" id="showall">
      show accepted and dismissed too</label></div>
    <div id="suggestions"></div>
  </section>
</main>
<dialog class="modal" id="takeover" aria-labelledby="takeovertitle">
  <form method="dialog" id="takeoverform">
    <div class="mhead">
      <svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M2 6h12M6 6v7.5"/><path d="M9.5 9.5l1.5 1.5 2.5-3"/></svg>
      <div><h2 id="takeovertitle">Take over from here</h2>
        <p class="sub">These rows become a table of their own — query it in the SQL tab: filter, aggregate, join it
          with the other tables.</p></div>
    </div>
    <div class="mbody">
      <div class="facts">
        <div class="fact"><div class="v" id="tofrows">—</div><div class="l">rows</div></div>
        <div class="fact"><div class="v" id="tofcols">—</div><div class="l">columns</div></div>
        <div class="fact"><div class="v" id="tofwhere">memory</div><div class="l">kept until the server restarts</div></div>
      </div>
      <label class="name" for="toname">Table name</label>
      <div class="namebox"><span>SELECT * FROM</span><input id="toname" autocomplete="off" spellcheck="false"
        pattern="[a-z_][a-z0-9_]{0,62}" required></div>
      <div class="hint" id="tohint">Letters, digits and _, starting with a letter. The same name replaces an earlier take-over.</div>
      <div class="cols" id="tocols"></div>
      <label class="check capped" id="tocapped" hidden><input type="checkbox" id="tofull" checked>
        <span>Fetch every matching row — the answer stopped at <b id="tolimit"></b> rows; this runs its query again
          without that cap</span></label>
      <p class="err" id="toerr" hidden></p>
    </div>
    <div class="mfoot"><span class="note">It's a snapshot: querying it never calls the sources again.</span>
      <button class="secondary" type="button" id="tocancel">Cancel</button>
      <button class="primary" type="submit" id="togo">Create table &amp; open SQL</button></div>
  </form>
</dialog>
<dialog class="modal" id="viewdlg" aria-labelledby="viewtitle">
  <form method="dialog" id="viewform">
    <div class="mhead">
      <svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M2 6h12M6 6v7.5"/><path d="M11 9v4M9 11h4"/></svg>
      <div><h2 id="viewtitle">Save as a table</h2>
        <p class="sub">This query gets a name of its own, kept in duckduck.json: query it like any table — in the SQL tab,
          and in Ask once the semantic catalog describes it.</p></div>
    </div>
    <div class="mbody">
      <div class="vkind" id="vkind"></div>
      <label class="name" for="vname">Table name</label>
      <div class="namebox"><span>SELECT * FROM</span><input id="vname" autocomplete="off" spellcheck="false"
        pattern="[a-z_][a-z0-9_]{0,62}" required></div>
      <div class="hint" id="vhint"></div>
      <label class="name" for="vdesc" style="margin-top:10px">Description <span class="muted" style="font-weight:400">— optional; helps people and Ask</span></label>
      <input id="vdesc" autocomplete="off" style="width:100%" placeholder="e.g. ServiceNow incidents">
      <label class="name" for="vstmt" style="margin-top:12px">Statement <span class="muted" style="font-weight:400">— what the table reads; edit it here</span></label>
      <textarea class="mono vsql" id="vstmt" spellcheck="false" rows="4"></textarea>
      <p class="err" id="verr" hidden></p>
    </div>
    <div class="mfoot"><span class="note" id="vnote"></span>
      <button class="danger" type="button" id="vdelete" hidden style="margin-right:auto">Remove</button>
      <button class="secondary" type="button" id="vopen" hidden title="Put the statement in the SQL editor, to run or change it there">Open in the editor</button>
      <button class="secondary" type="button" id="vcancel">Cancel</button>
      <button class="primary" type="submit" id="vgo">Save table</button></div>
  </form>
</dialog>
<dialog class="viewer" id="viewer" aria-labelledby="vwtitle">
  <div class="vwhead">
    <div class="vwtitle"><h2 id="vwtitle">Result</h2><span class="muted small" id="vwcount"></span></div>
    <input type="search" id="vwsearch" placeholder="Search every column…" aria-label="Search the rows" autocomplete="off">
    <details class="vwcols" id="vwcols"><summary>Columns</summary><div class="vwcolpanel">
      <div class="row"><button type="button" class="linkish" id="vwallcols">All</button><button type="button" class="linkish" id="vwnocols">None</button></div>
      <div id="vwcollist"></div></div></details>
    <label class="check small" title="Show long values in full, on several lines"><input type="checkbox" id="vwwrap"> Wrap</label>
    <button class="secondary" type="button" id="vwcopy" title="Copy the rows on this page (tab-separated — pastes into a spreadsheet)">Copy</button>
    <button class="secondary" type="button" id="vwcsv" title="Every row (searched and sorted as shown), the columns shown">Download CSV</button>
    <button class="x" type="button" id="vwclose" aria-label="Close" title="Close (Esc)">✕</button>
  </div>
  <div class="vwmain">
    <div class="vwgrid" id="vwgrid"></div>
    <aside class="vwcell" id="vwcell" hidden>
      <div class="row"><strong id="vwcellname"></strong><span class="grow"></span>
        <button class="secondary" type="button" id="vwcellcopy">Copy</button><button class="x" type="button" id="vwcellclose" aria-label="Close the value">✕</button></div>
      <div class="muted small" id="vwcellwhere"></div>
      <pre class="mono" id="vwcellvalue"></pre>
    </aside>
  </div>
  <div class="vwfoot"><span id="vwrange" class="small"></span><span class="grow"></span>
    <label class="small">Rows per page <select id="vwsize"><option>100</option><option selected>200</option><option>500</option><option>1000</option></select></label>
    <button class="secondary" type="button" id="vwprev">‹ Previous</button>
    <button class="secondary" type="button" id="vwnext">Next ›</button></div>
</dialog>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script>
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let META = null;
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch {} } };

async function api(path, body, signal) {
  const headers = {"Content-Type": "application/json"};
  const token = store.get("duckduck-token"); if (token) headers["X-Duckduck-Token"] = token;
  const r = await fetch(path, body === undefined ? {headers, signal} : {method: "POST", headers, signal, body: JSON.stringify(body)});
  if (r.status === 401) {
    const t = prompt("This server needs its access token:");
    if (t) { store.set("duckduck-token", t); return api(path, body, signal); }
  }
  const text = await r.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; }
  catch { throw new Error(`the server sent a response the page can't read (${path}, HTTP ${r.status})`); }
  if (!r.ok) throw new Error(data.detail || data.error || r.statusText);
  return data;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2200); }
const user = () => $("#user").value.trim() || null;

// ---- tabs --------------------------------------------------------------
document.querySelectorAll("nav button").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll("nav button").forEach(x => x.setAttribute("aria-selected", x === b));
  document.querySelectorAll("main > section").forEach(s => s.hidden = s.id !== "tab-" + b.dataset.tab);
  if (b.dataset.tab !== "config" && location.hash.startsWith("#config/")) history.replaceState(null, "", location.pathname);
  ({history: loadHistory, dashboard: loadDashboard, suggestions: loadSuggestions,
    sql: () => loadTables(),  // never waits on META: an empty list always says why
    config: async () => {  // loaded once, so switching tabs keeps edits
      loadCatalog(); await loadCfgStatus();
      if (CFG) showCfgPage(CFGPAGE); else await loadConfig();
    }})[b.dataset.tab]?.();
}));
$("#user").value = store.get("duckduck-user") || "";
$("#user").addEventListener("change", () => store.set("duckduck-user", $("#user").value));

// ---- while typing: what the question seems to be about --------------------
// A pause in typing asks /api/preview (one decision-engine batch, nothing run). The chips show the systems
// and the entity it points at; clicking one lets the user choose. Their choice goes with the question:
// only_sources (nothing else is used, joins included) and entity (pinned, never asked).
// A choice belongs to the question it was made for (pre.forQ): small edits keep it, a new question starts
// from the new suggestions.
const pre = {data: null, chosen: null, entity: null, field: null, blocked: new Set(), forQ: null, open: null, seq: 0, timer: null,
             ctrl: null, thinking: false, error: null, pendingSubmit: false};
// How the question is read: "rules" = Paddle (the wording + Jev) or "llm" = Dive (an LLM reads it, then Jev) — remembered.
const READER_NAMES = {rules: "Paddle", llm: "Dive", llm_decides: "Fly", auto: "Auto"};
const READER_ICONS = {rules: "🦆", llm: "🤿", llm_decides: "🪽", auto: "🧭"};
const READER_BUSY = {rules: "Paddling…", llm: "Diving…", llm_decides: "Flying…", auto: "Routing…"};
const LLM_READERS = ["llm", "llm_decides"];
// What the duck says while it reads — Dive's rotate every couple of seconds (an LLM takes a moment).
const QUIPS = {
  rules: ["paddling through your words…"],
  llm: ["diving for what you meant…", "holding breath, reading between the lines…",
        "rubber-duck debugging your question…", "asking the fish what you meant…",
        "untangling it one feather at a time…", "quack… thinking… quack…",
        "looking for meaning under the surface…", "almost there — duck still underwater…"],
  auto: ["checking how questions like this went…", "reading the compass…", "asking Jev which way to go…",
         "picking paddle, dive or fly…"],
  llm_decides: ["flying solo today — Jev has the day off…", "the LLM has the controls…",
                "checking the map from up here…", "circling the catalog…", "deciding everything, one flap at a time…",
                "cruising altitude, weighing the options…", "wind in the feathers, tables in sight…",
                "almost there — preparing to land…"],
};
let quipAt = 0, quipTimer = null;
function quip() { const q = QUIPS[READER] || QUIPS.rules; return q[quipAt % q.length]; }
function startQuips() {
  const n = (QUIPS[READER] || [1]).length;  // any opener but the last ("almost there…" only after a while)
  clearInterval(quipTimer); quipAt = Math.floor(Math.random() * Math.max(1, n - 1));
  if ((QUIPS[READER] || []).length < 2) return;
  quipTimer = setInterval(() => {
    quipAt++; const el = document.querySelector("#chips .chip.thinking .txt"); if (el) el.textContent = quip();
  }, 2200);
}
function stopQuips() { clearInterval(quipTimer); quipTimer = null; }
let READER = store.get("duckduck-reader") || "rules";
const previewable = (q) => q.length >= 8 && q.split(/\s+/).length >= 2;
// Ask waits for the question to be read: disabled while the preview is pending or running (Enter queues the ask).
const busy = () => !!pre.timer || pre.thinking;
function updateAsk() {
  const b = $("#askbtn"); b.disabled = busy() || !!META?.setup; b.textContent = busy() ? (READER_BUSY[READER] || "Reading…") : "Ask";
  b.title = busy() ? "Reading the question — press Enter and it's asked as soon as the duck surfaces" : "";
}
function setReader(r, rerun = true) {
  const available = META?.readers?.available || ["rules"];
  READER = available.includes(r) ? r : (META?.readers?.default || "rules");
  store.set("duckduck-reader", READER);
  document.querySelectorAll("[data-reader]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.reader === READER)));
  if (rerun && previewable($("#question").value.trim())) { pre.data = null; clearTimeout(pre.timer); pre.timer = null; runPreview(); }
}
document.querySelectorAll("[data-reader]").forEach(b => b.addEventListener("click", () => setReader(b.dataset.reader)));
$("#readerhelpbtn").addEventListener("click", () => {
  const open = $("#readerhelp").hidden; $("#readerhelp").hidden = !open;
  $("#readerhelpbtn").setAttribute("aria-expanded", String(open));
});
function showReaders() {
  const why = META?.readers?.llm_unavailable;
  [...LLM_READERS, "auto"].forEach(r => { const b = document.querySelector(`[data-reader="${r}"]`);
    b.disabled = !!why; if (why) b.title = `${READER_NAMES[r]} is unavailable: ${why}`; });
  setReader(READER, false);
}
function resetChoices() { Object.assign(pre, {chosen: null, entity: null, field: null, blocked: new Set(), forQ: null}); }
function chose() { pre.forQ = $("#question").value.trim(); }
const words = (t) => new Set((t || "").toLowerCase().match(/[\p{L}\p{N}_.:-]+/gu) || []);
function sameQuestion(a, b) {
  const A = words(a), B = words(b); if (!A.size || !B.size) return false;
  let both = 0; A.forEach(w => { if (B.has(w)) both++; });
  return both / Math.max(A.size, B.size) >= 0.5;
}
$("#question").addEventListener("input", () => {
  clearTimeout(pre.timer); pre.timer = null;
  const q = $("#question").value.trim();
  if (!q) { Object.assign(pre, {data: null, open: null, error: null}); resetChoices(); drawChips(); return; }
  if (previewable(q)) pre.timer = setTimeout(runPreview, 600);
  else { pre.ctrl?.abort(); pre.seq++; pre.thinking = false; stopQuips(); }
  updateAsk();
});
$("#question").addEventListener("keydown", (e) => {  // Enter while the question is being read: ask once it's read
  if (e.key !== "Enter" || !busy()) return;
  e.preventDefault(); pre.pendingSubmit = true;
  if (pre.timer) { clearTimeout(pre.timer); pre.timer = null; runPreview(); }
  toast("Got it — asking as soon as the duck surfaces 🦆");
});
async function runPreview() {
  pre.timer = null;
  const q = $("#question").value.trim();
  if (!previewable(q)) { updateAsk(); return; }
  pre.ctrl?.abort(); pre.ctrl = new AbortController();
  const ctrl = pre.ctrl, mine = ++pre.seq; pre.thinking = true; startQuips(); drawChips();
  const giveUp = setTimeout(() => ctrl.abort("timeout"), 60000);  // never keep Ask disabled forever
  try {
    const d = await api("/api/preview", {question: q, reader: READER}, ctrl.signal);
    if (mine !== pre.seq) return;
    pre.error = d.error || null;
    pre.data = d.error ? null : d;
    if (pre.forQ !== null && !sameQuestion(pre.forQ, q)) resetChoices();  // a new question: fresh suggestions
  } catch (err) {
    if (mine !== pre.seq) return;
    pre.error = ctrl.signal.reason === "timeout" ? "reading the question took too long" : err.message; pre.data = null;
  } finally { clearTimeout(giveUp); }
  if (mine === pre.seq) {
    pre.thinking = false; stopQuips(); drawChips();
    if (pre.pendingSubmit) { pre.pendingSubmit = false; $("#askform").requestSubmit(); }
  }
}
const joinKey = (j) => `${j.left}=${j.right}`;
const allSources = () => (pre.data?.systems || []).flatMap(g => g.sources.map(s => s.source));
const suggested = () => { const rel = (pre.data?.systems || []).flatMap(g => g.sources.filter(s => s.relevant).map(s => s.source));
                          return new Set(rel.length ? rel : allSources()); };
function drawChips() {
  const box = $("#chips"), d = pre.data, chips = [];
  if (d) {
    const shape = d.answer_shape?.choice;
    if (shape === "catalog") chips.push(`<button class="chip" type="button" disabled><span class="dot"></span><span class="k">About</span> the catalog itself</button>`);
    if (shape !== "catalog" && (d.systems || []).length) {
      let label;
      if (pre.chosen) {
        const whole = d.systems.filter(g => g.sources.every(s => pre.chosen.has(s.source)));
        const covered = whole.reduce((n, g) => n + g.sources.length, 0) === pre.chosen.size;
        label = covered && whole.length ? `only ${whole.map(g => g.system).join(", ")}` : `${pre.chosen.size} table${pre.chosen.size === 1 ? "" : "s"} chosen`;
      }
      else {
        const sys = d.systems.filter(g => g.relevant).map(g => g.system);
        label = sys.length ? sys.join(", ") : "any";
      }
      const icons = [...new Set(d.systems.filter(g => pre.chosen ? g.sources.some(s => pre.chosen.has(s.source)) : g.relevant).map(g => g.icon))];
      const used = (d.joins || []).filter(j => !pre.blocked.has(joinKey(j))).length;
      const joins = (d.joins || []).length ? ` · ${used} join${used === 1 ? "" : "s"}` : "";
      chips.push(`<button class="chip ${pre.chosen || pre.blocked.size ? "mine" : ""}" type="button" data-open="systems" aria-expanded="${pre.open === "systems"}"><span class="dot"></span><span class="k">Systems</span> ${icons.map(icon).join("")}${esc(label)}${joins}</button>`);
    }
    const e = d.entity;
    if (d.field) {  // the answer is a field's values: that field is what it's about
      const [src, fld] = (pre.field || d.field).split(".");
      chips.push(`<button class="chip ${pre.field ? "mine" : ""}" type="button" data-open="field" aria-expanded="${pre.open === "field"}"><span class="dot"></span><span class="k">About</span> ${esc(fld.replace(/_/g, " "))} <span class="k">(${esc(src)})</span></button>`);
    } else if (shape !== "catalog" && e && (pre.entity || e.sure)) {
      const name = (pre.entity || e.choice).replace(/_/g, " ");
      chips.push(`<button class="chip ${pre.entity ? "mine" : ""}" type="button" data-open="entity" aria-expanded="${pre.open === "entity"}"><span class="dot"></span><span class="k">About</span> ${esc(name)}</button>`);
    }
    if (shape && !["list", "catalog"].includes(shape) && d.answer_shape.sure) {
      chips.push(`<button class="chip" type="button" disabled><span class="dot"></span><span class="k">Answer</span> ${esc(SHAPE_WORDS[shape] || shape)}</button>`);
    }
  }
  if (d && d.route) {  // Auto: which mode it picked, and why
    const rt = d.route;
    chips.push(`<span class="chip" title="${esc(rt.why || "")}"><span class="dot"></span><span class="k">Auto picked</span> ${modeName(rt.reader)}${rt.probability < 1 ? ` <span class="k">${Math.round(rt.probability * 100)}%</span>` : ""}</span>`);
  }
  if (d && d.reading && LLM_READERS.includes(d.reader || READER)) {  // only what survived the catalog check; nothing left → no chip
    const r = d.reading;
    const parts = [r.answer ? esc(SHAPE_WORDS[r.answer] || r.answer) : "",
                   r.about ? `about ${esc(r.about_kind)} ${esc(r.about)}` : "", r.group_by ? `per ${esc(r.group_by)}` : ""].filter(Boolean);
    if (parts.length) chips.push(`<span class="chip" title="${esc(d.english_question ? "read as: " + d.english_question : "")}"><span class="dot"></span><span class="k">${READER_NAMES[d.reader || READER]} found</span> ${parts.join(" · ")}</span>`);
  }
  if (pre.thinking) chips.push(`<span class="chip thinking"><span class="dot"></span><span class="txt">${esc(quip())}</span></span>`);
  else if (pre.error) chips.push(`<span class="chip thinking" title="${esc(pre.error)}"><span class="dot"></span>couldn't read the question yet — it will still be answered</span>`);
  box.innerHTML = chips.join("");
  updateAsk();
  box.querySelectorAll("[data-open]").forEach(b => b.addEventListener("click", () => {
    pre.open = pre.open === b.dataset.open ? null : b.dataset.open; drawChips();
  }));
  drawPanel();
}
function drawPanel() {
  const panel = $("#chippanel"), d = pre.data;
  if (!d || !pre.open) { panel.hidden = true; return; }
  panel.hidden = false;
  if (pre.open === "systems") {
    const picked = pre.chosen || suggested();
    panel.innerHTML = `<p class="muted small" style="margin:0 0 6px">Which systems and tables should answer this? ${pre.chosen ? "" : "Suggested ones are ticked."}</p>` +
      d.systems.map(g => `<h4><label class="check"><input type="checkbox" data-system="${esc(g.system)}"
          ${g.sources.every(s => picked.has(s.source)) ? "checked" : ""}
          data-some="${g.sources.some(s => picked.has(s.source)) && !g.sources.every(s => picked.has(s.source)) ? 1 : 0}"> ${icon(g.icon)} ${esc(g.system)}</label>
          ${g.label ? `<span class="label">${esc(g.label)}</span>` : ""}
          <button class="secondary small" type="button" data-only="${esc(g.system)}" style="padding:2px 8px;margin-left:auto;font-size:12px;white-space:nowrap">only this</button></h4>` +
        g.sources.map(s => `<label class="srcrow"><input type="checkbox" data-source="${esc(s.source)}" ${picked.has(s.source) ? "checked" : ""}>
          <span>${icon(s.icon)}${esc(s.source)} ${s.joined ? `<span class="tag" title="needed to join">joined</span>` : s.relevant ? `<span class="tag">suggested</span>` : ""}</span>
          ${s.probability === null || s.probability === undefined ? "<span></span>" : `<span class="rel" title="relevance ${Math.round(s.probability * 100)}%"><span style="width:${Math.round(s.probability * 100)}%"></span></span>`}
          ${s.description ? `<span class="desc">${esc(s.description)}</span>` : ""}</label>`).join("")).join("") +
      ((d.joins || []).length ? `<h4>Joins <span class="label">untick one to keep the answer from using it</span></h4>` +
        d.joins.map(j => `<label class="srcrow"><input type="checkbox" data-join="${esc(joinKey(j))}" ${pre.blocked.has(joinKey(j)) ? "" : "checked"}>
          <span class="mono">${esc(j.left)} = ${esc(j.right)}</span>
          <span class="rel" title="confidence ${Math.round(j.confidence * 100)}%"><span style="width:${Math.round(j.confidence * 100)}%"></span></span>
          <span class="desc">to find the ${esc(String(j.for).replace(/_/g, " "))} · ${esc(j.type.replace(/_/g, " "))}</span></label>`).join("") : "") +
      `<div class="foot"><button class="secondary" type="button" id="useall">Let it choose</button>
        <button class="primary" type="button" id="paneldone">Done</button></div>`;
    panel.querySelectorAll("[data-source]").forEach(cb => cb.addEventListener("change", () => {
      const set = new Set(pre.chosen || suggested());
      cb.checked ? set.add(cb.dataset.source) : set.delete(cb.dataset.source);
      pre.chosen = set; chose(); drawChips();
    }));
    panel.querySelectorAll("[data-join]").forEach(cb => cb.addEventListener("change", () => {
      cb.checked ? pre.blocked.delete(cb.dataset.join) : pre.blocked.add(cb.dataset.join); chose(); drawChips();
    }));
    panel.querySelectorAll("[data-system]").forEach(cb => { cb.indeterminate = cb.dataset.some === "1"; });
    panel.querySelectorAll("[data-only]").forEach(b => b.addEventListener("click", () => {
      pre.chosen = new Set(d.systems.find(g => g.system === b.dataset.only).sources.map(s => s.source)); chose(); drawChips();
    }));
    panel.querySelectorAll("[data-system]").forEach(cb => cb.addEventListener("change", () => {
      const set = new Set(pre.chosen || suggested());
      d.systems.find(g => g.system === cb.dataset.system).sources.forEach(s => cb.checked ? set.add(s.source) : set.delete(s.source));
      pre.chosen = set; chose(); drawChips();
    }));
    $("#useall").addEventListener("click", () => { pre.chosen = null; pre.blocked = new Set(); pre.open = null; drawChips(); });
  } else if (pre.open === "field") {
    const current = pre.field || d.field;
    panel.innerHTML = `<p class="muted small" style="margin:0 0 6px">Which field's values should the answer list?</p>` +
      (d.field_options || []).map(o => { const [src, fld] = o.field.split(".");
        return `<label class="srcrow"><input type="radio" name="fld" value="${esc(o.field)}" ${o.field === current ? "checked" : ""}>
        <span>${esc(fld.replace(/_/g, " "))} <span class="muted small">(${esc(src)})</span>${o.field === d.field ? ` <span class="tag">in the question</span>` : ""}</span><span></span>
        ${o.description ? `<span class="desc">${esc(o.description)}</span>` : ""}</label>`; }).join("") +
      `<div class="foot"><button class="secondary" type="button" id="useall">Let it decide</button>
        <button class="primary" type="button" id="paneldone">Done</button></div>`;
    panel.querySelectorAll("input[name=fld]").forEach(r => r.addEventListener("change", () => {
      pre.field = r.value === d.field ? null : r.value; chose(); drawChips(); }));
    $("#useall").addEventListener("click", () => { pre.field = null; pre.open = null; drawChips(); });
  } else {
    const e = d.entity, desc = e.descriptions || {};
    panel.innerHTML = `<p class="muted small" style="margin:0 0 6px">What should the answer be about?</p>` +
      e.ranked.map(([name, p]) => `<label class="srcrow"><input type="radio" name="ent" value="${esc(name)}" ${(pre.entity || (e.sure ? e.choice : "")) === name ? "checked" : ""}>
        <span>${esc(name.replace(/_/g, " "))}</span>
        <span class="rel" title="${Math.round(p * 100)}%"><span style="width:${Math.round(p * 100)}%"></span></span>
        ${desc[name] ? `<span class="desc">${esc(desc[name])}</span>` : ""}</label>`).join("") +
      `<div class="foot"><button class="secondary" type="button" id="useall">Let it decide</button>
        <button class="primary" type="button" id="paneldone">Done</button></div>`;
    panel.querySelectorAll("input[name=ent]").forEach(r => r.addEventListener("change", () => { pre.entity = r.value; chose(); drawChips(); }));
    $("#useall").addEventListener("click", () => { pre.entity = null; pre.open = null; drawChips(); });
  }
  $("#paneldone").addEventListener("click", () => { pre.open = null; drawChips(); });
}

// ---- ask ---------------------------------------------------------------
$("#askform").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#question").value.trim(); if (!q) return;
  clearTimeout(pre.timer); pre.open = null;
  if (pre.forQ !== null && !sameQuestion(pre.forQ, q)) resetChoices();  // chosen for another question
  if (pre.chosen && pre.chosen.size === 0) { drawChips(); toast("Choose at least one table, or let it choose."); return; }
  const stale = pre.data?.question !== q;
  if (stale) { pre.data = null; pre.error = null; }
  drawChips();
  ask(q);
  if (stale) runPreview();  // asked before the pause: the box catches up with this question
});
async function ask(q, extra = {}) {
  const body = {question: q, user: user(), reader: READER, sample: sampleSize(), ...extra};
  if (pre.chosen) body.only_sources = [...pre.chosen];
  if (pre.entity) body.entity = pre.entity;
  if (pre.field) body.values_field = pre.field;
  if (pre.blocked.size) body.blocked_joins = [...pre.blocked].map(k => k.split("="));
  runJob("/api/ask", body, q);
}

// ---- the sample: how many rows an answer shows first (every row afterwards, on request) ----
function sampleSize() { const v = $("#sample").value; return v === "all" ? null : +v; }
try { const v = localStorage.getItem("duckduck-sample"); if (v && [...$("#sample").options].some(o => o.value === v)) $("#sample").value = v; } catch {}
$("#sample").addEventListener("change", () => { try { localStorage.setItem("duckduck-sample", $("#sample").value); } catch {} });

function moreBar(c) {
  const r = c.result, n = (r.results || []).length;
  if (!r.has_more) return "";
  const what = r.sample ? `Showing a sample of ${n.toLocaleString()} rows — there are more.`
                        : `Stopped at ${n.toLocaleString()} rows — there may be more.`;
  const how = r.data_complete ? "instant: every row was already read" : "reads the data again, in the background — you can pause or cancel";
  return `<div class="morebar"><span>${what}</span><button class="secondary" type="button" data-fetchall>Get all rows</button>
    <span class="muted small">${how}</span></div>`;
}

// ---- live progress: what the question is doing now; pause to look, cancel if it makes no sense ----
let JOB = null;  // {id, question, events, state, partial, elapsed, timer}
const JOB_STATE = {running: "Working on it…", pausing: "Pausing after the current step…", paused: "Paused",
                   cancelled: "Cancelled"};

async function runJob(path, body, question) {
  if (JOB) { const old = JOB; JOB = null; clearTimeout(old.timer);  // a new question replaces the one running
             api(`/api/jobs/${old.id}/cancel`, {}).catch(() => {}); }
  const job = {id: null, question, events: [], state: "running", partial: {}, elapsed: 0, timer: null};
  JOB = job; drawJob(job);
  let started;
  try { started = await api(path, {...body, background: true}); }
  catch (err) { if (JOB === job) { JOB = null; $("#conversation").innerHTML = `<div class="card error">${esc(err.message)}</div>`; } return; }
  if (JOB !== job) { api(`/api/jobs/${started.job_id}/cancel`, {}).catch(() => {}); return; }
  job.id = started.job_id; pollAskJob(job);
}

function takeJob(job, v) {
  for (const e of v.events || []) job.events[e.i] = e;  // the step still running comes back with new counts
  job.state = v.state; job.partial = v.partial || {}; job.elapsed = v.elapsed_ms || 0;
}

async function pollAskJob(job) {
  let v;
  try { v = await api(`/api/jobs/${job.id}?since=${job.events.length}`); }
  catch (err) { if (JOB === job) { JOB = null; $("#conversation").innerHTML = `<div class="card error">Lost track of the question: ${esc(err.message)}</div>`; } return; }
  if (JOB !== job) return;  // cancelled or replaced meanwhile
  takeJob(job, v);
  if (v.state === "done") { JOB = null; render(v.result); return; }
  if (v.state === "failed") { JOB = null; $("#conversation").innerHTML = `<div class="card error">${esc(v.error?.message || "it failed")}</div>`; return; }
  if (v.state === "cancelled") { JOB = null; drawJob(job); return; }
  drawJob(job);
  job.timer = setTimeout(() => pollAskJob(job), v.state === "paused" ? 1000 : 350);
}

async function jobAction(action) {
  const job = JOB; if (!job || !job.id) return;
  if (action === "cancel") { JOB = null; clearTimeout(job.timer); }
  try { takeJob(job, await api(`/api/jobs/${job.id}/${action}`, {})); }
  catch (err) { toast(err.message); }
  if (action === "cancel") job.state = "cancelled";
  drawJob(job);
}

function jobSoFar(job) {
  const p = job.partial || {}, parts = [];
  if (p.sql) parts.push(`<details open><summary>SQL so far</summary><pre>${esc(p.sql)}</pre>${META?.features?.sql
    ? `<button class="secondary" type="button" data-jobsql>Open in the SQL tab</button>` : ""}</details>`);
  const read = Object.entries(p.fetched || {});
  if (read.length) parts.push(`<details open><summary>Tables read</summary>${table(read.map(([s, f]) => ({
    table: s, rows: f.rows, "rows read": f.rows_scanned, pages: f.pages || null, "filters sent to the source": (f.pushed || []).join(", ") || "—"})))}</details>`);
  if ((p.decisions || []).length) parts.push(`<details${p.sql ? "" : " open"}><summary>Decided so far</summary>${table(p.decisions.map(d => ({
    decision: d.kind, about: d.subject, answer: typeof d.answer === "object" ? JSON.stringify(d.answer) : d.answer,
    probability: Math.round(d.probability * 100) / 100, by: d.decided_by})))}</details>`);
  return parts.length ? parts.join("") : `<p class="jobnote">Nothing decided yet — it was still reading the question.</p>`;
}

function drawJob(job) {
  const st = job.state, live = st === "running" || st === "pausing", steps = job.events.filter(Boolean);
  const secs = (ms) => ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
  const html = `<div class="card"><div class="q">${esc(job.question || "")}</div></div>
  <div class="card" aria-live="polite">
    <div class="jobhead">${live ? `<span class="spin" aria-hidden="true"></span>` : ""}
      <strong>${esc(JOB_STATE[st] || st)}</strong><span class="muted small">${job.elapsed ? secs(job.elapsed) : ""}</span>
      <span class="grow"></span>
      ${st === "running" ? `<button class="secondary" type="button" data-job="pause" title="Stop at the next step and show what it has so far">⏸ Pause</button>` : ""}
      ${st === "paused" || st === "pausing" ? `<button class="secondary" type="button" data-job="resume">▶ Continue</button>` : ""}
      ${st !== "cancelled" && job.id ? `<button class="secondary" type="button" data-job="cancel" title="Stop here: this isn't going where you meant">✕ Cancel</button>` : ""}
    </div>
    <ol class="steps">${steps.map((e, k) => {
      const now = k === steps.length - 1 && live;
      return `<li class="${now ? "now" : "done"}"><span class="mark">${now ? `<span class="spin" aria-hidden="true"></span>` : "✓"}</span>
        <span>${esc(e.text)}</span><span class="at">${secs(e.ms)}</span></li>`; }).join("") || `<li class="now"><span class="mark"><span class="spin"></span></span><span>Starting…</span></li>`}</ol>
    ${st === "pausing" ? `<p class="jobnote">A call already in flight (to the decision engine or a data source) finishes first — then it stops.</p>` : ""}
    ${st === "paused" || st === "cancelled" ? `<div style="margin-top:10px">${st === "cancelled"
      ? `<p class="jobnote">Stopped. This is what it had done — edit the SQL in the SQL tab, or rephrase the question.</p>` : ""}${jobSoFar(job)}</div>` : ""}
  </div>`;
  const box = $("#conversation"); box.innerHTML = html;
  box.querySelectorAll("[data-job]").forEach(b => b.addEventListener("click", () => jobAction(b.dataset.job)));
  box.querySelector("[data-jobsql]")?.addEventListener("click", () => { $("#sqltext").value = job.partial.sql; openTab("sql"); });
}

// a question put in the box by a click (a suggested question): its own suggestions, not the last one's
function askFresh() {
  Object.assign(pre, {data: null, open: null, error: null}); resetChoices(); drawChips();
  $("#askform").requestSubmit();
}

// "Take over from here": the answer's rows become a table (in memory, on the server), then the SQL tab opens on it
let TAKEOVER = null;  // {convId, proposal}
async function takeOver(convId) {
  let p;
  try { p = await api("/api/takeover/proposal", {conversation_id: convId}); }
  catch (err) { toast(err.message); return; }
  TAKEOVER = {convId, proposal: p};
  $("#toname").value = p.name; $("#tofrows").textContent = p.rows.toLocaleString();
  $("#tofcols").textContent = p.columns.length;
  $("#tocols").innerHTML = p.columns.slice(0, 40).map(c => `<span class="mono">${esc(c)}</span>`).join("") +
    (p.columns.length > 40 ? `<span>+${p.columns.length - 40} more</span>` : "");
  $("#tocapped").hidden = !p.capped; $("#tolimit").textContent = (p.limit || 0).toLocaleString(); $("#tofull").checked = true;
  $("#toerr").hidden = true; $("#togo").disabled = false; $("#togo").textContent = "Create table & open SQL";
  checkTakeoverName();
  $("#takeover").showModal(); $("#toname").select();
}
function checkTakeoverName() {
  const v = $("#toname").value.trim().toLowerCase(), ok = /^[a-z_][a-z0-9_]{0,62}$/.test(v);
  const replaces = ok && (TAKEOVER?.proposal.taken || []).includes(v);
  $("#tohint").className = "hint" + (ok || !v ? "" : " bad");
  $("#tohint").textContent = !v ? "Letters, digits and _, starting with a letter." :
    !ok ? "Only letters, digits and _, starting with a letter (no spaces or dashes)." :
    replaces ? `Replaces your earlier “${v}” take-over.` : "Letters, digits and _, starting with a letter. The same name replaces an earlier take-over.";
  $("#togo").disabled = !ok;
  return ok;
}
$("#toname").addEventListener("input", () => { $("#toerr").hidden = true; checkTakeoverName(); });
$("#tocancel").addEventListener("click", () => $("#takeover").close());
$("#takeover").addEventListener("click", (e) => { if (e.target === $("#takeover")) $("#takeover").close(); });  // backdrop
$("#takeoverform").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!TAKEOVER || !checkTakeoverName()) return;
  const full = !$("#tocapped").hidden && $("#tofull").checked;
  $("#togo").disabled = true; $("#togo").textContent = full ? "Fetching every row…" : "Creating…";
  try {
    const t = await api("/api/takeover", {conversation_id: TAKEOVER.convId, name: $("#toname").value.trim().toLowerCase(), full, user: user()});
    $("#takeover").close();
    fbTakenOver(t.feedback);
    toast(`${t.name}: ${t.rows.toLocaleString()} row${t.rows === 1 ? "" : "s"} — ${t.how}${t.feedback ? " · marked as answered 👍" : ""}`);
    $("#sqltext").value = `SELECT * FROM ${t.name} LIMIT 100`;
    openTab("sql"); await loadTables(); runSql();
  } catch (err) {
    $("#toerr").textContent = err.message; $("#toerr").hidden = false;
    $("#togo").disabled = false; $("#togo").textContent = "Create table & open SQL";
  }
});

// "Save as table": a query kept as a table (duckduck.json → views) — bound when it's just a table function's arguments.
// The same dialog edits one: its statement, name and description (VIEWDLG.previous = the name it has now).
let VIEWDLG = null;  // {previous, check, timer}
const sqlLiteral = (v) => typeof v === "string" ? `'${v.replace(/'/g, "''")}'` : String(v);
function saveAsTable(sql) { return openViewDialog({sql}); }
async function editSavedTable(name) {
  let v, all;
  try { all = (await api("/api/views")).views; v = all.find(x => x.name === name); } catch (err) { toast(err.message); return; }
  if (!v) { toast(`“${name}” isn't a saved table any more`); return; }
  openViewDialog({sql: v.statement, previous: name, description: v.description || ""});
  VIEWDLG.readers = readersOf(name, all);
}
// the other saved tables that read this one — renaming or removing it leaves them reading a name that's gone
function readersOf(name, views) {
  const word = new RegExp(`\\b${name}\\b`, "i");
  return views.filter(v => v.name !== name && word.test(v.statement || "")).map(v => v.name);
}
async function openViewDialog({sql, previous = null, description = ""}) {
  sql = (sql || "").trim();
  if (!sql) { toast("Write a query first"); return; }
  VIEWDLG = {previous, check: null, timer: null, readers: []};
  const editing = !!previous;
  $("#viewtitle").textContent = editing ? `Saved table “${previous}”` : "Save as a table";
  $("#vstmt").value = sql; $("#vdesc").value = description;
  $("#vname").value = previous || "";
  $("#verr").hidden = true;
  $("#vdelete").hidden = $("#vopen").hidden = !editing;
  $("#vgo").textContent = editing ? "Save changes" : "Save table";
  $("#vkind").innerHTML = `<span class="muted">Reading the statement…</span>`;
  $("#viewdlg").showModal();
  await checkStatement(!editing);
  (editing ? $("#vstmt") : $("#vname")).focus();
  if (!editing) $("#vname").select();
}
// what the statement makes (a table over a function, or a saved query) — asked again as it's edited
async function checkStatement(nameFromIt) {
  const dlg = VIEWDLG; if (!dlg) return;
  let c;
  try { c = await api("/api/views/check", {sql: $("#vstmt").value}); } catch (err) { c = {error: err.message}; }
  if (VIEWDLG !== dlg) return;
  dlg.check = c;
  if (c.error) {
    $("#vkind").innerHTML = `<b class="bad">Can't be saved as it is</b><div>${esc(c.error)}</div>`;
  } else {
    const call = c.kind === "bound" ? `${c.table}(${Object.entries(c.args || {}).map(([k, v]) => `${k}=${sqlLiteral(v)}`).join(", ")})` : "";
    $("#vkind").innerHTML = c.kind === "bound"
      ? `<b>A table over <span class="mono">${esc(call)}</span></b><div>Filters and LIMIT on it still go to the source, as they do on ${esc(c.table)}.</div>`
      : `<b>A saved query</b><div>It runs each time the table is read; filters on it are applied after it runs.</div>`;
    if (nameFromIt && !$("#vname").value) $("#vname").value = c.name;
  }
  $("#vnote").textContent = c.off ? `Can't save here: ${c.off}.` : "Kept in duckduck.json (a .bak is kept) — it's there after a restart.";
  checkViewName();
}
function checkViewName() {
  const n = $("#vname").value.trim().toLowerCase(), hint = $("#vhint"), known = TABLES.find(t => t.name === n);
  const previous = VIEWDLG?.previous;
  let bad = "";
  if (!/^[a-z_][a-z0-9_]{0,62}$/.test(n)) bad = "Letters, digits and _, starting with a letter.";
  else if (known && !known.saved) bad = `“${n}” is already a table (${known.service || "registered"}) — pick another name.`;
  else if (previous && known && n !== previous) bad = `“${n}” is another saved table — pick another name.`;
  const readers = VIEWDLG?.readers || [];
  hint.textContent = bad || (previous && n !== previous ? `Renames “${previous}” to “${n}”.` + (readers.length
      ? ` ${readers.join(", ")} read${readers.length === 1 ? "s" : ""} “${previous}” — change ${readers.length === 1 ? "it" : "them"} too, or ${readers.length === 1 ? "it stops" : "they stop"} working.` : "")
    : known && !previous ? `Replaces the saved table “${n}”.` : "Letters, digits and _, starting with a letter.");
  hint.classList.toggle("bad", !!bad || !!(previous && n !== previous && readers.length));
  const c = VIEWDLG?.check;
  $("#vgo").disabled = !!bad || !c || !!c.error || !!c.off;
  return !bad;
}
$("#vname").addEventListener("input", checkViewName);
$("#vstmt").addEventListener("input", () => {
  $("#vgo").disabled = true;
  clearTimeout(VIEWDLG?.timer);
  if (VIEWDLG) VIEWDLG.timer = setTimeout(() => checkStatement(false), 400);
});
$("#vcancel").addEventListener("click", () => $("#viewdlg").close());
$("#viewdlg").addEventListener("click", (e) => { if (e.target === $("#viewdlg")) $("#viewdlg").close(); });  // backdrop
$("#vopen").addEventListener("click", () => {
  $("#viewdlg").close(); openTab("sql");
  $("#sqltext").value = $("#vstmt").value; $("#sqltext").focus();
});
// the Config tab's copy of the file follows, so a later Save there keeps what was done here
function draftViews(change) { if (CFG) for (const o of [CFG.config, DRAFT]) { o.views = {...(o.views || {})}; change(o.views); if (!Object.keys(o.views).length) delete o.views; } }
function afterSavedTables() {
  loadTables(); loadCfgStatus();
  if (CFG && !$("#tab-config").hidden && CFGPAGE === "saved") renderForm();
}
$("#vdelete").addEventListener("click", async () => {
  const name = VIEWDLG?.previous; if (!name) return;
  const readers = VIEWDLG.readers || [];
  if (!confirm(`Remove the saved table “${name}”? It's taken out of duckduck.json (a .bak is kept).` +
    (readers.length ? `\n\n${readers.join(", ")} read${readers.length === 1 ? "s" : ""} it and will stop working.` : ""))) return;
  try { await fetchDelete(`/api/views/${encodeURIComponent(name)}`); }
  catch (err) { $("#verr").textContent = err.message; $("#verr").hidden = false; return; }
  draftViews(v => delete v[name]);
  $("#viewdlg").close(); toast(`Removed “${name}”`); afterSavedTables();
});
async function fetchDelete(path) {
  const headers = {}; const t = store.get("duckduck-token"); if (t) headers["X-Duckduck-Token"] = t;
  const r = await fetch(path, {method: "DELETE", headers});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d;
}
$("#viewform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const dlg = VIEWDLG;
  if (!dlg || !checkViewName() || !dlg.check || dlg.check.error) return;
  const name = $("#vname").value.trim().toLowerCase(), previous = dlg.previous;
  const body = {name, sql: $("#vstmt").value.trim(), description: $("#vdesc").value.trim() || undefined,
    ...(previous ? {previous} : {replace: !!TABLES.find(t => t.name === name && t.saved)})};
  $("#vgo").disabled = true; $("#vgo").textContent = "Saving…";
  let saved;
  try { saved = await api("/api/views", body); }
  catch (err) {
    $("#verr").textContent = err.message; $("#verr").hidden = false;
    $("#vgo").disabled = false; $("#vgo").textContent = previous ? "Save changes" : "Save table"; return;
  }
  const def = saved.kind === "bound" ? {table: saved.table, args: saved.args} : {sql: saved.sql};
  if (saved.description) def.description = saved.description;
  draftViews(v => { if (previous && previous !== name) delete v[previous]; v[name] = def; });
  if (saved.kind === "bound") JUST_SAVED.add(nestedKey(saved.table, saved.args));  // its catalog row shows ✓ name
  $("#viewdlg").close();
  toast(previous ? `Saved “${name}”` : `Saved “${name}” — it's a table now`);
  afterSavedTables();
  if (!previous) { openTab("sql"); $("#sqltext").value = `SELECT * FROM ${name} LIMIT 100`; $("#sqltext").focus(); }
});
$("#sqlsave").addEventListener("click", () => saveAsTable($("#sqltext").value));

async function reply(convId, text) {
  runJob("/api/answer", {conversation_id: convId, reply: text}, LAST_QUESTION);
}

function table(rows, max = 500) {
  if (!rows || !rows.length) return `<p class="muted">No rows.</p>`;
  const cols = Object.keys(rows[0]);
  const num = cols.map(c => rows.every(r => r[c] === null || typeof r[c] === "number"));
  return `<div class="tablewrap"><table><thead><tr>${cols.map((c, i) => `<th class="${num[i] ? "num" : ""}">${esc(c)}</th>`).join("")}</tr></thead>
    <tbody>${rows.slice(0, max).map(r => `<tr>${cols.map((c, i) => `<td class="${num[i] ? "num" : ""}">${cellHtml(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
// a nested value (an ADX dynamic column, a JSON field) shows as JSON, never "[object Object]"
function cellHtml(v) {
  if (v === null || v === undefined) return '<span class="muted">—</span>';
  if (typeof v !== "object") return esc(v);
  const text = JSON.stringify(v), CUT = 160;
  return `<span class="mono jcell" title="${esc(JSON.stringify(v, null, 2))}">${esc(text.length > CUT ? text.slice(0, CUT) + "…" : text)}</span>`;
}

const SHAPE_WORDS = {list: "a list", count: "a count", values: "the different values", count_values: "a count of values",
                     count_by: "a count per group", lookup: "everything about the value", locate: "where the value is",
                     catalog: "what data there is", small_talk: "small talk", out_of_scope: "not about the data",
                     browse: "the table itself"};

let LAST_QUESTION = "";
function render(c) {
  const r = c.result, box = $("#conversation");
  LAST_QUESTION = r.question;
  let html = `<div class="card"><div class="q">${esc(r.question)}</div>`;
  if (r.intent && r.intent.english_question) html += `<div class="muted small">read as: ${esc(r.intent.english_question)}</div>`;
  if (c.history.length) html += `<div class="thread">${c.history.map(h =>
      `<div class="turn">Q: ${esc(h.asked)}<br>A: ${esc(h.reply)}${h.understood ? "" : " <em>(not understood)</em>"}</div>`).join("")}</div>`;
  html += `</div>`;
  if (r.reply) {
    const shape = r.intent?.answer_shape;
    html += `<div class="card"><p style="margin:0 0 8px">${esc(r.reply)}</p>
      ${(r.suggestions || []).map(q => `<button class="option" type="button" data-suggest="${esc(q)}">${esc(q)}</button>`).join("")}
      ${shape === "out_of_scope" ? `<div class="row" style="margin-top:8px"><button class="secondary" type="button" data-anyway>${esc(META?.texts?.ask_anyway || "It is about the data — try anyway")}</button></div>` : ""}
    </div>`;
  } else if (r.status === "needs_clarification" && r.followup && r.followup.options.length) {
    const f = r.followup;
    html += `<div class="card"><h3>${esc(f.question)}</h3>${f.context ? `<p class="context">${esc(f.context)}</p>` : ""}
      ${f.options.map((o, i) => `<button class="option" data-reply="${i + 1}">${i + 1}) ${esc(o.label)}${o.detail ? ` <span class="detail">— ${esc(o.detail)}</span>` : ""}</button>`).join("")}
      <form class="row" id="freeform" style="margin-top:8px"><input id="freetext" placeholder="…or answer in your own words" style="flex:1">
      <button class="secondary">Reply</button></form></div>`;
  } else if (r.status !== "ok") {
    html += `<div class="card"><h3>I couldn't answer this</h3><p class="context">${esc(r.clarification || r.status)}</p></div>`;
  } else {
    const shape = (r.intent && r.intent.answer_shape) || "list";
    const tables = shape === "catalog" ? [] : (r.query_plan ? r.query_plan.sources : (r.sections || []).filter(s => s.rows).map(s => s.source));
    const n = (r.results || []).length;
    html += `<div class="card"><div class="row"><span class="pill">${esc(SHAPE_WORDS[shape] || shape)}</span>
      ${r.only_sources ? `<span class="pill" title="you chose these tables">only: ${esc(r.only_sources.join(", "))}</span>` : ""}
      ${tables.map(s => `<span class="pill">${icon(META?.source_icons?.[s])}${esc(s)}</span>`).join("")}
      <span class="muted small">${n.toLocaleString()} row${n === 1 ? "" : "s"}${r.sample && r.has_more ? " (sample)" : ""}${c.truncated ? " (first 500 shown)" : ""}</span>
      ${runPill(r)}
      ${(c.result.column_options || []).length ? `<button class="secondary" type="button" data-columns aria-expanded="${COLS_OPEN.has(c.conversation_id)}"
        style="margin-left:auto;padding:4px 10px;font-size:13px" title="Show more of these rows: other columns of the same tables">＋ Columns${(r.added_columns || []).length ? ` (${r.added_columns.length})` : ""}</button>` : ""}
      ${META?.features?.sql && n ? `<button class="secondary" type="button" data-takeover style="${(c.result.column_options || []).length ? "" : "margin-left:auto;"}padding:4px 10px;font-size:13px"
        title="Register these rows as a table and continue in the SQL tab">Take over from here</button>` : ""}</div>
      ${moreBar(c)}
      ${COLS_OPEN.has(c.conversation_id) ? columnPanel(r) : ""}
      ${n ? `<div class="resbar" style="margin-top:6px"><span class="grow"></span><button class="secondary" type="button" data-expandrows
        style="padding:3px 10px;font-size:12.5px" title="Open these rows full screen: search, sort, a value in full, CSV">⤢ Expand</button></div>` : ""}
      ${table(r.results)}
      ${r.summary ? `<details><summary>Where it was looked for</summary>${table(r.summary)}</details>` : ""}
      ${(r.sections || []).filter(s => s.results && s.results.length).map(s =>
          `<details><summary>${esc(s.source)} — ${s.rows} row${s.rows === 1 ? "" : "s"}</summary>${table(s.results)}</details>`).join("")}
      ${r.sql ? `<details><summary>SQL</summary><pre>${esc(r.sql)}</pre>${META?.features?.sql && r.query_plan ? `<button class="secondary" type="button" data-runsql>Open in the SQL tab</button>` : ""}</details>` : ""}
      ${(r.hypotheses || []).length > 1 ? `<details><summary>Readings considered (${r.hypotheses.length}, judged side by side)</summary>${table(r.hypotheses.map(h => ({
          reading: h.label, plan: h.description, "chosen by the engine": h.probability, "answers the question": h.answers})))}</details>` : ""}
      <details><summary>How it was decided</summary>${table((r.decisions || []).map(d => ({
          decision: d.kind, about: d.subject, answer: typeof d.answer === "object" ? JSON.stringify(d.answer) : d.answer,
          probability: Math.round(d.probability * 100) / 100, by: d.decided_by})))}</details>
      ${(r.intent && r.intent.similar_cases && r.intent.similar_cases.length) ? `<details><summary>Similar questions confirmed before</summary>${table(r.intent.similar_cases.map(x => ({question: x.question, similarity: x.similarity, kind: x.kind, answer: x.answer_shape, tables: (x.sources || []).join(", ")})))}</details>` : ""}
    </div>`;
  }
  if (c.done && r.search_id) html += feedbackForm(r);
  box.innerHTML = html;
  box.querySelectorAll("button.option").forEach(b => b.addEventListener("click", () => reply(c.conversation_id, b.dataset.reply)));
  box.querySelector("[data-runsql]")?.addEventListener("click", () => { $("#sqltext").value = r.sql; openTab("sql"); });
  box.querySelector("[data-takeover]")?.addEventListener("click", () => takeOver(c.conversation_id));
  box.querySelector("[data-expandrows]")?.addEventListener("click", () => openViewer(rowsSource(c.result.results || []), c.result.question));
  wireColumns(c);
  box.querySelector("[data-fetchall]")?.addEventListener("click", () =>
    runJob("/api/all", {conversation_id: c.conversation_id}, r.question));
  box.querySelectorAll("[data-suggest]").forEach(b => b.addEventListener("click", () => {
    $("#question").value = b.dataset.suggest; askFresh(); }));
  box.querySelector("[data-anyway]")?.addEventListener("click", () => ask(r.question, {in_scope: true}));
  $("#freeform")?.addEventListener("submit", (e) => { e.preventDefault(); const t = $("#freetext").value.trim(); if (t) reply(c.conversation_id, t); });
  wireFeedback(r);
}

// ---- more columns: a step back after the answer --------------------------
// The same plan runs again with the fields picked here next to what was asked (no new decision).
const COLS_OPEN = new Set();
let colsTimer = null;

function columnPanel(r) {
  const opts = r.column_options || [];
  const chip = o => `<button type="button" class="colchip" data-col="${esc(o.ref)}" aria-pressed="${o.selected}"
      ${o.asked ? "disabled" : ""} title="${esc(o.description || o.ref)}${o.asked ? " — what the question asked for" : o.kept ? " — already read: instant" : " — needs reading the data again"}">${esc(
      r.query_plan.sources.length > 1 ? o.ref : o.field)}${o.why ? ` <span class="why">· ${esc(o.why)}</span>` : ""}</button>`;
  const suggested = opts.filter(o => o.suggested), rest = opts.filter(o => !o.suggested && !o.asked), asked = opts.filter(o => o.asked);
  const distinct = r.query_plan.distinct;
  return `<div class="colpanel" id="colpanel">
    <div class="jobhead"><h4 style="margin:0">Show more of these rows</h4><span class="grow"></span>
      ${rest.length || suggested.length ? `<button type="button" class="secondary" data-colall style="padding:2px 10px;font-size:13px"
        title="Every column of ${esc(r.query_plan.sources.join(", "))}">All columns</button>` : ""}
      ${(r.added_columns || []).length ? `<button type="button" class="secondary" data-colreset style="padding:2px 10px;font-size:13px"
        title="Back to the columns the question asked for">Clear</button>` : ""}</div>
    <div class="fbchips">${asked.map(chip).join("")}</div>
    ${suggested.length ? `<h4>Used by the question <button type="button" class="secondary" data-colsuggested
        style="padding:1px 8px;font-size:12px;margin-left:6px" title="Add the columns the question filtered on">add these</button></h4><div class="fbchips">${suggested.map(chip).join("")}</div>` : ""}
    ${rest.length ? `<h4>Other columns</h4><div class="fbchips">${rest.map(chip).join("")}</div>` : ""}
    <div class="colstatus" id="colstatus" role="status">${r.reused_data === true ? "✓ From the rows already read — nothing fetched again."
      : r.reused_data === false ? "Read the data again (the rows read before didn't have those columns)." : ""}</div>
    <div class="muted small">Same question, same filters — only the columns change.${distinct ? " One row per distinct combination." : ""}</div>
  </div>`;
}

function wireColumns(c) {
  const box = $("#conversation");
  box.querySelector("[data-columns]")?.addEventListener("click", () => {
    COLS_OPEN.has(c.conversation_id) ? COLS_OPEN.delete(c.conversation_id) : COLS_OPEN.add(c.conversation_id);
    render(c);
  });
  const panel = box.querySelector("#colpanel");
  if (!panel) return;
  const chosen = () => [...panel.querySelectorAll(".colchip:not([disabled])")]
    .filter(b => b.getAttribute("aria-pressed") === "true").map(b => b.dataset.col);
  const send = (cols) => {
    clearTimeout(colsTimer);
    colsTimer = setTimeout(async () => {
      const kept = cols.every(ref => (c.result.column_options || []).some(o => o.ref === ref && (o.kept || o.selected)));
      if (!kept) {  // not in the rows read: read again, showing each step (pause / cancel)
        runJob("/api/columns", {conversation_id: c.conversation_id, columns: cols}, c.result.question); return;
      }
      const st = panel.querySelector("#colstatus");
      if (st) st.innerHTML = `<span class="spin" aria-hidden="true"></span> Updating from the rows already read…`;
      panel.querySelectorAll(".colchip").forEach(b => b.disabled = true);
      try { render(await api("/api/columns", {conversation_id: c.conversation_id, columns: cols})); }
      catch (err) { toast(err.message); render(c); }
    }, 350);  // a few quick taps → one run
  };
  panel.querySelectorAll(".colchip:not([disabled])").forEach(b => b.addEventListener("click", () => {
    b.setAttribute("aria-pressed", b.getAttribute("aria-pressed") === "true" ? "false" : "true");
    send(chosen());
  }));
  panel.querySelector("[data-colsuggested]")?.addEventListener("click", () => {
    panel.querySelectorAll(".colchip:not([disabled])").forEach(b => {
      if ((c.result.column_options || []).some(o => o.ref === b.dataset.col && o.suggested)) b.setAttribute("aria-pressed", "true");
    });
    send(chosen());
  });
  panel.querySelector("[data-colall]")?.addEventListener("click", () => {
    panel.querySelectorAll(".colchip:not([disabled])").forEach(b => b.setAttribute("aria-pressed", "true"));
    send(chosen());
  });
  panel.querySelector("[data-colreset]")?.addEventListener("click", () => {
    panel.querySelectorAll(".colchip:not([disabled])").forEach(b => b.setAttribute("aria-pressed", "false"));
    send([]);
  });
}

// ---- feedback ----------------------------------------------------------
function options(obj, blank) { return (blank ? `<option value="">${blank}</option>` : "") +
  Object.entries(obj).map(([k, v]) => `<option value="${esc(k)}" title="${esc(v)}">${esc(k)}</option>`).join(""); }

// Feedback: one tap on a verdict saves it; chips, words and "what would have been right" save as they change.
// One widget per search — on the Ask page under the answer, and on every History row. One answer keeps one
// feedback row: the first save creates it, the next ones replace it (feedback_id).
const FB_THANKS = {answered: "✓ Saved — thanks! It'll remember this worked.",
                   partial: "✓ Saved. Tap what was missing below, if you like.",
                   not_answered: "✓ Saved. Tap what went wrong below, if you like — it learns from it."};
function fbVerdicts(compact = false) {
  const b = (v, e, label) => `<button class="verdict${compact ? " sm" : ""}" type="button" data-v="${v}" aria-pressed="false" title="${label}"><span class="e" aria-hidden="true">${e}</span>${compact ? `<span class="sr">${label}</span>` : label}</button>`;
  return b("answered", "👍", "Yes") + b("partial", "🤏", "Partly") + b("not_answered", "👎", "No");
}
function fbMoreHtml() {
  const m = META || {categories: {}, sources: {}, answer_shapes: {}, entities: {}, values: {}};
  return `<p class="muted small fblabel">What went wrong? <span>Optional — tap any that fit; it saves as you go.</span></p>
    <div class="fbchips">${Object.entries(m.categories).map(([k, v]) =>
      `<button type="button" class="fbchip" data-cat="${esc(k)}" aria-pressed="false">${esc(v)}</button>`).join("")}</div>
    <textarea class="fbreason" rows="2" placeholder="Or say it in your words — e.g. I wanted how many, not the list"></textarea>
    <details><summary>What would have been right? <span class="muted small">(optional — this is what it learns the most from)</span></summary>
      <div class="grid2">
        <label>Tables<br><select class="fbsources" multiple size="${Math.min(Math.max(Object.keys(m.sources).length, 2), 8)}">${options(m.sources)}</select></label>
        <label>Kind of answer<br><select class="fbshape">${options(m.answer_shapes, "—")}</select></label>
        <label>What it asks about<br><select class="fbentity">${options(m.entities, "—")}</select></label>
      </div>
      <p class="muted small" style="margin:12px 0 4px">A word it misread: “<em>word</em>” means <em>field = value</em></p>
      <div class="row"><input class="fbword" placeholder="word, e.g. urgentes" style="width:180px">
        <select class="fbfield">${options(Object.fromEntries(Object.keys(m.values).map(k => [k, k])), "field")}</select>
        <select class="fbvalue"><option value="">value</option></select></div>
    </details>`;
}
const parseJson = (x, fallback) => { if (x && typeof x === "object") return x; try { return x ? JSON.parse(x) : fallback; } catch (e) { return fallback; } };

// saved: {search_id, feedback_id, verdict, categories, reason, expected} (a History row, or just the search id)
// els: {verdicts, status, more?} — more can come later (attachMore), e.g. when a History row is opened
function feedbackWidget(saved, els, opts = {}) {
  const w = {searchId: saved.search_id, id: saved.feedback_id || null, verdict: saved.verdict || null,
             categories: new Set(parseJson(saved.categories, [])), reason: saved.reason || "",
             expected: parseJson(saved.expected, null) || {}, chain: Promise.resolve(), timer: null, more: null};
  const status = (text, cls = "") => { if (els.status) { els.status.textContent = text; els.status.className = "small fbstatus " + (cls || "muted"); } };
  w.press = (v) => {
    els.verdicts.querySelectorAll("button.verdict").forEach(x => x.setAttribute("aria-pressed", String(x.dataset.v === v)));
    if (w.more) w.more.hidden = !v || v === "answered";
  };
  w.body = () => {
    const answered = w.verdict === "answered";
    return {search_id: w.searchId, verdict: w.verdict, user: user(), feedback_id: w.id,
            categories: answered ? [] : [...w.categories], reason: answered ? "" : w.reason,
            expected: answered ? null : w.expected};
  };
  w.save = (thanks) => {  // chained, so the first save's id is known before the next replaces it
    if (!w.verdict) return;
    clearTimeout(w.timer); w.timer = null; status("Saving…");
    w.chain = w.chain.then(async () => {
      try { const r = await api("/api/feedback", w.body()); w.id = r.id; status(thanks || "✓ Saved", "fbok"); opts.onSaved?.(w); }
      catch (err) { status("Couldn't save — tap again. " + err.message, "fberr"); }
    });
  };
  w.soon = () => { if (w.verdict) { clearTimeout(w.timer); status("…"); w.timer = setTimeout(() => w.save(), 700); } };
  w.attachMore = (el) => {
    if (w.more === el) return;
    el.innerHTML = fbMoreHtml(); w.more = el;
    const q = (c) => el.querySelector(c), e = w.expected;
    el.querySelectorAll(".fbchip").forEach(c => {
      c.setAttribute("aria-pressed", String(w.categories.has(c.dataset.cat)));
      c.addEventListener("click", () => {
        const on = c.getAttribute("aria-pressed") !== "true"; c.setAttribute("aria-pressed", String(on));
        on ? w.categories.add(c.dataset.cat) : w.categories.delete(c.dataset.cat); w.save();
      });
    });
    q(".fbreason").value = w.reason;
    q(".fbreason").addEventListener("input", () => { w.reason = q(".fbreason").value; w.soon(); });
    q(".fbreason").addEventListener("blur", () => { if (w.timer) w.save(); });
    const fillValues = () => { const vals = (META?.values?.[q(".fbfield").value] || []);
      q(".fbvalue").innerHTML = `<option value="">value</option>` + vals.map(v => `<option>${esc(v)}</option>`).join(""); };
    [...q(".fbsources").options].forEach(o => o.selected = (e.sources || []).includes(o.value));
    q(".fbshape").value = e.answer_shape || ""; q(".fbentity").value = e.entity || "";
    if (e.synonym) { q(".fbword").value = e.synonym.word || ""; q(".fbfield").value = e.synonym.field || ""; fillValues(); q(".fbvalue").value = e.synonym.value || ""; }
    if (e.sources?.length || e.answer_shape || e.entity || e.synonym) q("details").open = true;
    const readExpected = () => {
      const x = {}, srcs = [...q(".fbsources").selectedOptions].map(o => o.value);
      if (srcs.length) x.sources = srcs;
      if (q(".fbshape").value) x.answer_shape = q(".fbshape").value;
      if (q(".fbentity").value) x.entity = q(".fbentity").value;
      if (q(".fbword").value.trim() && q(".fbfield").value && q(".fbvalue").value)
        x.synonym = {word: q(".fbword").value.trim(), field: q(".fbfield").value, value: q(".fbvalue").value};
      w.expected = x;
    };
    q(".fbfield").addEventListener("change", fillValues);
    [".fbsources", ".fbshape", ".fbentity", ".fbfield", ".fbvalue"].forEach(c => q(c).addEventListener("change", () => { readExpected(); w.save(); }));
    q(".fbword").addEventListener("input", () => { readExpected(); w.soon(); });
    w.press(w.verdict);
  };
  els.verdicts.querySelectorAll("button.verdict").forEach(b => b.addEventListener("click", () => {
    w.verdict = b.dataset.v; w.press(w.verdict);
    b.classList.remove("pop"); void b.offsetWidth; b.classList.add("pop");
    opts.onVerdict?.(w);
    w.save(opts.thanks ? FB_THANKS[w.verdict] : "✓ Saved");
  }));
  if (els.more) w.attachMore(els.more);
  w.press(w.verdict);
  // "Take over from here" counts as a yes (the server records it when nobody rated the answer yet)
  w.takenOver = (f) => { if (!f) return; w.id = f.id; w.verdict = f.verdict; w.press(f.verdict);
                         status("✓ Marked as answered — you took the rows over.", "fbok"); };
  return w;
}

function feedbackForm(r) {
  return `<div class="card feedback" id="fb">
    <div class="fbhead"><h3>Did this answer your question?</h3>
      <span class="small fbstatus muted" role="status" aria-live="polite">One tap — it learns from every answer.</span></div>
    <div class="row fbverdicts">${fbVerdicts()}</div>
    <div class="fbmore" hidden></div></div>`;
}
let FB = null, FB_EL = null, FB_ID = null;  // the Ask page's widget, its element, the answer it rates
function wireFeedback(r) {
  const fb = $("#fb"); if (!fb) { FB = null; return; }
  // the same answer drawn again (columns added): keep the widget, and what was already saved with it
  if (FB && FB_EL && FB_ID === r.search_id) { fb.replaceWith(FB_EL); return; }
  FB_EL = fb; FB_ID = r.search_id;
  FB = feedbackWidget({search_id: r.search_id}, {verdicts: fb.querySelector(".fbverdicts"),
    status: fb.querySelector(".fbstatus"), more: fb.querySelector(".fbmore")}, {thanks: true});
}
function fbTakenOver(feedback) { if (FB) FB.takenOver(feedback); }

// ---- history -----------------------------------------------------------
async function loadHistory() {
  try {
    const mode = $("#historymode").value;
    const rows = await api("/api/searches?limit=200" + (mode ? "&reader=" + encodeURIComponent(mode) : ""));
    if (!rows.length) { $("#history").innerHTML = `<p class="muted">No questions asked yet${mode ? " in this mode" : ""}.</p>`; return; }
    $("#history").innerHTML = `<div class="tablewrap"><table class="histtable"><thead><tr><th>When</th><th>Question</th><th>Mode</th><th>Result</th><th>Answer</th><th>Tables</th><th class="num">Time</th><th class="num">Calls</th><th>Did it answer?</th></tr></thead><tbody>${
      rows.map((r, i) => `<tr class="hrow" data-i="${i}"><td class="small muted">${esc((r.created_at || "").replace("T", " ").slice(0, 16))}</td>
        <td><button type="button" class="hopen" aria-expanded="false" title="The SQL it ran, and more feedback">${esc(r.question)}</button>
          ${r.user_name ? `<div class="muted small">${esc(r.user_name)}</div>` : ""}</td>
        <td class="small" title="${esc(r.engine || "")}">${r.requested_reader === "auto" ? "🧭→" : ""}${modeName(r.reader)}</td>
        <td class="small">${esc(r.status)}</td><td class="small">${esc(r.answer_shape || "")}</td>
        <td class="small">${esc((parseJson(r.sources, [])).join(", "))}</td>
        <td class="num small">${ms(r.elapsed_ms)}</td>
        <td class="num small" title="decision-engine calls · LLM calls${r.llm_tokens ? " · " + r.llm_tokens + " LLM tokens" : ""}${r.cost !== null && r.cost !== undefined ? " · $" + r.cost.toFixed(6) : ""}">${r.engine_calls ?? 0} · ${r.llm_calls ?? 0}</td>
        <td class="hfb"><div class="row fbverdicts fbmini">${fbVerdicts(true)}</div><div class="small fbstatus muted">${r.verdict ? "" : "not rated"}</div></td></tr>
      <tr class="hdetail" data-i="${i}" hidden><td colspan="9"><div class="hdgrid">
        <div class="hsql"><h4>SQL it ran</h4>${r.sql ? `<pre>${esc(r.sql)}</pre>${META?.features?.sql ? `<button class="secondary" type="button" data-opensql="${i}">Open in the SQL tab</button>` : ""}`
          : `<p class="muted small">No SQL — ${r.status === "needs_clarification" ? "it asked back" + (r.clarification ? ": " + esc(r.clarification) : "") : esc(r.clarification || r.status || "nothing ran")}.</p>`}
          ${r.english_question && r.english_question !== r.question ? `<p class="muted small">read as: ${esc(r.english_question)}</p>` : ""}</div>
        <div class="hmore"><h4>Feedback</h4><p class="muted small hmorehint">Tap 👍 🤏 👎 on the row. After 🤏 or 👎, say what went wrong here.</p><div class="fbmore" hidden></div></div>
      </div></td></tr>`).join("")}</tbody></table></div>`;
    const open = (i, on) => {
      const row = $(`#history tr.hrow[data-i="${i}"]`), det = $(`#history tr.hdetail[data-i="${i}"]`);
      det.hidden = !on; row.querySelector(".hopen").setAttribute("aria-expanded", String(on)); row.classList.toggle("open", on);
      if (on) HISTORY_FB[i].attachMore(det.querySelector(".fbmore"));
    };
    HISTORY_FB = rows.map((r, i) => {
      const row = $(`#history tr.hrow[data-i="${i}"]`);
      return feedbackWidget({...r, search_id: r.id}, {verdicts: row.querySelector(".fbverdicts"), status: row.querySelector(".fbstatus")},
        {onVerdict: (w) => { if (w.verdict !== "answered") open(i, true); }});
    });
    document.querySelectorAll("#history .hopen").forEach(b => b.addEventListener("click", () => {
      const i = +b.closest("tr").dataset.i; open(i, $(`#history tr.hdetail[data-i="${i}"]`).hidden);
    }));
    document.querySelectorAll("#history [data-opensql]").forEach(b => b.addEventListener("click", () => {
      $("#sqltext").value = rows[+b.dataset.opensql].sql; openTab("sql");
    }));
  } catch (err) { $("#history").innerHTML = `<p class="error">${esc(err.message)}</p>`; }
}
let HISTORY_FB = [];

// ---- modes: names, what one search cost, the comparison table ------------------------------
const modeName = (r) => `${READER_ICONS[r || "rules"] || ""} ${esc(READER_NAMES[r || "rules"] || r || "")}`;
const ms = (x) => x === null || x === undefined ? "—" : x >= 1000 ? (x / 1000).toFixed(1) + " s" : Math.round(x) + " ms";
function runPill(r) {
  const u = r.usage || {}, parts = [(r.requested_reader === "auto" ? "🧭→" : "") + modeName(r.reader)];
  if (u.engine_calls) parts.push(`${u.engine_calls} decision call${u.engine_calls === 1 ? "" : "s"}`);
  if (u.llm_calls) parts.push(`${u.llm_calls} LLM call${u.llm_calls === 1 ? "" : "s"}`);
  if (u.reused) parts.push("reading from the preview");
  parts.push(ms(r.elapsed_ms ?? r.execution?.elapsed_ms));
  const tip = [u.llm_tokens_in || u.llm_tokens_out ? `${(u.llm_tokens_in || 0) + (u.llm_tokens_out || 0)} LLM tokens` : "",
               u.cost !== null && u.cost !== undefined ? `$${u.cost.toFixed(6)} reported` : ""].filter(Boolean).join(" · ");
  return `<span class="pill" title="${esc(tip)}">${parts.join(" · ")}</span>`;
}
function modesTable(byMode, fromStats = false) {  // {reader: metrics} — from /api/stats (usage) or /api/evaluate
  const order = ["rules", "llm", "llm_decides"];
  const modes = Object.keys(byMode).sort((a, b) => (order.indexOf(a) + 1 || 9) - (order.indexOf(b) + 1 || 9));
  if (!modes.length) return `<p class="muted">Nothing yet.</p>`;
  const rows = fromStats ? [
    ["searches", m => m.searches], ["rated", m => m.rated], ["answered (of rated)", m => pct(m.answer_rate)],
    ["asked back", m => pct(m.asked_back_rate)], ["median time", m => ms(m.median_ms)],
    ["decision calls / search", m => m.avg_engine_calls], ["LLM calls / search", m => m.avg_llm_calls],
    ["LLM tokens / search", m => m.avg_llm_tokens], ["reported cost", m => m.searches_with_cost ? "$" + (m.reported_cost || 0).toFixed(4) : "—"],
    ["previews while typing", m => m.previews ?? 0], ["decision calls while typing", m => m.typing_engine_calls ?? 0],
    ["LLM calls while typing", m => m.typing_llm_calls ?? 0], ["LLM tokens while typing", m => m.typing_llm_tokens ?? 0],
    ["cost while typing (reported)", m => m.typing_cost === null || m.typing_cost === undefined ? "—" : "$" + m.typing_cost.toFixed(4)],
  ] : [
    ["tables right", m => pct(m.source_accuracy)], ["about right (entity)", m => pct(m.entity_accuracy)],
    ["kind of answer right", m => pct(m.answer_shape_accuracy)], ["planned", m => pct(m.plan_validity)],
    ["asked back", m => pct(m.asked_back_rate)], ["mean time", m => ms(m.mean_latency_ms)],
    ["decision calls / question", m => (m.mean_engine_calls ?? 0).toFixed(1)], ["LLM calls / question", m => (m.mean_llm_calls ?? 0).toFixed(1)],
    ["reported cost", m => m.reported_cost === null || m.reported_cost === undefined ? "—" : "$" + m.reported_cost.toFixed(4)],
  ];
  return `<div class="tablewrap"><table><thead><tr><th></th>${modes.map(r => `<th class="num">${modeName(r)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(([label, f]) => `<tr><td>${esc(label)}</td>${modes.map(r => `<td class="num">${f(byMode[r]) ?? "—"}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

// ---- dashboard ---------------------------------------------------------
const pct = (x) => x === null || x === undefined ? "—" : Math.round(x * 100) + "%";
function barTable(title, rows, valueKey, label, fmt = (x) => x, countKey = "rated") {
  if (!rows.length) return `<div class="card"><h3>${esc(title)}</h3><p class="muted">No rated questions yet.</p></div>`;
  const max = Math.max(...rows.map(r => r[valueKey] || 0), 1e-9);
  return `<div class="card"><h3>${esc(title)}</h3><div class="tablewrap"><table><thead><tr><th>${esc(label)}</th>
    ${countKey ? `<th class="num">${esc(countKey)}</th>` : ""}<th class="num">${esc(valueKey.replace("_", " "))}</th><th></th></tr></thead><tbody>${
    rows.map(r => `<tr><td>${esc(r.label || r.key)}</td>${countKey ? `<td class="num">${r[countKey] ?? ""}</td>` : ""}
      <td class="num">${fmt(r[valueKey])}</td>
      <td style="width:40%"><div class="bar" title="${esc(r.label || r.key)}: ${fmt(r[valueKey])}"><span style="width:${Math.max(0, (r[valueKey] || 0) / max * 100)}%"></span></div></td></tr>`).join("")}
    </tbody></table></div></div>`;
}
async function loadDashboard() {
  try {
    const s = await api("/api/stats"), o = s.overall;
    $("#dashboard").innerHTML = `<div class="kpis">
      <div class="kpi"><div class="v">${o.searches}</div><div class="l">searches</div></div>
      <div class="kpi"><div class="v">${o.rated}</div><div class="l">rated by users</div></div>
      <div class="kpi"><div class="v">${pct(o.answer_rate)}</div><div class="l">answered (of rated)</div></div>
      <div class="kpi"><div class="v">${pct(o.asked_back_rate)}</div><div class="l">asked a question back</div></div>
    </div><div style="height:14px"></div>
    <div class="card"><h3>Modes compared</h3>
      <p class="muted small" style="margin:0 0 6px">Every search, by how it was read and decided. Answer rate counts
        rated searches only; calls and tokens are per search. Typing costs too: the preview while you type calls Jev
        (and, in Dive and Fly, the LLM) — counted separately, never twice. Cost is what the providers reported.</p>
      ${modesTable(Object.fromEntries((s.by_reader || []).map(r => [r.reader, r])), true)}</div>
    ${(s.auto || []).length ? `<div class="card"><h3>🧭 Auto's picks</h3>
      <p class="muted small" style="margin:0 0 6px">The questions asked in Auto, by the mode it picked, and how they went.</p>
      ${table(s.auto.map(a => ({picked: `${READER_ICONS[a.reader] || ""} ${READER_NAMES[a.reader] || a.reader}`, searches: a.searches, rated: a.rated, answered: a.answered, "answer rate": pct(a.answer_rate)})))}</div>` : ""}
    ${barTable("Answer rate by kind of answer", s.by_answer_shape, "answer_rate", "kind of answer", pct)}
    ${barTable("Answer rate by table", s.by_source, "answer_rate", "table", pct)}
    ${barTable("What went wrong", s.by_category, "count", "problem", (x) => x, null)}
    ${barTable("Searches per day", s.by_day.map(d => ({key: String(d.day).slice(0, 10), rated: d.rated, searches: d.searches})), "searches", "day")}`;
  } catch (err) { $("#dashboard").innerHTML = `<div class="card error">${esc(err.message)}</div>`; }
}

// ---- suggestions ---------------------------------------------------------
const KIND = {value_synonym: "synonym for a value", source_example: "example question for a table",
  answer_wording: "wording for a kind of answer", entity_keyword: "keyword for an entity",
  activity_keyword: "keyword for an activity", source_note: "note on a table", unknown_word: "word the catalog doesn't know"};
async function loadSuggestions() {
  try {
    const items = await api("/api/suggestions?all=" + ($("#showall").checked ? 1 : 0));
    $("#suggestions").innerHTML = items.length ? items.map(s => `<div class="card sugg">
      <div class="kind">${esc(KIND[s.kind] || s.kind)}${s.target ? ` · ${esc(s.target)}` : ""}${s.status !== "open" ? ` · ${esc(s.status)}` : ""}</div>
      <div class="change">${esc(s.change)}</div><div class="muted small">${esc(s.reason)}</div>
      <details><summary>${s.support} question${s.support === 1 ? "" : "s"} behind it</summary><ul>${s.questions.map(q => `<li>${esc(q)}</li>`).join("")}</ul></details>
      ${s.status === "open" ? `<div class="row" style="margin-top:10px">
        <button class="primary" data-accept="${s.id}" ${s.applicable && META?.can_accept ? "" : "disabled"}>Accept</button>
        <button class="secondary" data-dismiss="${s.id}">Dismiss</button></div>` : ""}</div>`).join("")
      : `<p class="muted">No suggestions — they come from questions rated “not answered” or “partly”.</p>`;
    document.querySelectorAll("[data-accept]").forEach(b => b.addEventListener("click", async () => {
      try { const r = await api(`/api/suggestions/${b.dataset.accept}/accept`, {user: user()}); toast(r.applied); loadSuggestions(); }
      catch (err) { toast(err.message); } }));
    document.querySelectorAll("[data-dismiss]").forEach(b => b.addEventListener("click", async () => {
      try { await api(`/api/suggestions/${b.dataset.dismiss}/dismiss`, {user: user()}); loadSuggestions(); }
      catch (err) { toast(err.message); } }));
  } catch (err) { $("#suggestions").innerHTML = `<p class="error">${esc(err.message)}</p>`; }
}
$("#showall").addEventListener("change", loadSuggestions);
$("#historymode").addEventListener("change", loadHistory);
$("#evalbtn").addEventListener("click", async () => {
  $("#evaluation").innerHTML = `<p class="muted">Evaluating…</p>`;
  try {
    const e = await api("/api/evaluate", $("#evalmodes").checked ? {readers: META?.readers?.available || []} : {});
    if (!e.size) { $("#evaluation").innerHTML = `<p class="muted">${esc(e.note)}</p>`; return; }
    $("#evaluation").innerHTML = `<p class="small muted">${e.size} rated questions.</p>
      ${table(Object.entries(e.metrics).filter(([k]) => k.endsWith("accuracy") && k !== "answer_accuracy").map(([k, v]) => ({metric: k.replace(/_/g, " "), value: v === null ? null : Math.round(v * 1000) / 1000})))}
      ${e.misses.length ? `<details><summary>${e.misses.length} question(s) it still gets wrong</summary>${table(e.misses)}</details>` : ""}
      ${Object.keys(e.readers || {}).length ? `<h3 style="margin-top:14px">Modes compared (same questions)</h3>${modesTable(e.readers)}` : ""}
      <details open><summary>Suggested thresholds</summary><pre>${esc(e.calibration)}</pre></details>`;
  } catch (err) { $("#evaluation").innerHTML = `<p class="error">${esc(err.message)}</p>`; }
});
$("#exportbtn").addEventListener("click", async () => {
  const headers = {}; const t = store.get("duckduck-token"); if (t) headers["X-Duckduck-Token"] = t;
  const r = await fetch("/api/export.md?redact=" + ($("#exportredact").checked ? 1 : 0), {headers});
  if (!r.ok) { toast("couldn't build the brief"); return; }
  const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob());
  a.download = "feedback_export.md"; a.click();
});
$("#evaljson").addEventListener("click", async (e) => {
  e.preventDefault();
  const headers = {}; const t = store.get("duckduck-token"); if (t) headers["X-Duckduck-Token"] = t;
  const r = await fetch("/api/evaluation.json", {headers}); const blob = await r.blob();
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "feedback_evaluation.json"; a.click();
});

// ---- icons: one per kind of source (generic shapes, no brand logos) ------------------------
const ICONS = {
  dataset: '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M2 6h12M6 6v7.5"/><path d="M9.5 9.5l1.5 1.5 2.5-3" />',
  database: '<ellipse cx="8" cy="3.5" rx="5" ry="2"/><path d="M3 3.5v9c0 1.1 2.2 2 5 2s5-.9 5-2v-9"/><path d="M3 8c0 1.1 2.2 2 5 2s5-.9 5-2"/>',
  sharepoint: '<rect x="2.5" y="4.5" width="8" height="9.5" rx="1"/><path d="M5.5 4.5V2.5a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1V11a1 1 0 0 1-1 1h-2"/><path d="M4.5 8h4M4.5 10.5h4"/>',
  servicenow: '<path d="M2 4.5h12v2.2a1.6 1.6 0 0 0 0 3.1v2.2H2V9.8a1.6 1.6 0 0 0 0-3.1z"/><path d="M9.5 4.5v7.5" stroke-dasharray="1.4 1.4"/>',
  insightvm: '<path d="M8 1.5l5 2v4c0 3.2-2.2 5.6-5 7-2.8-1.4-5-3.8-5-7v-4z"/><path d="M5.8 8l1.6 1.6L10.4 6.5"/>',
  axonius: '<rect x="1.5" y="3" width="10" height="7" rx="1"/><path d="M4.5 13h4M6.5 10v3"/><rect x="11.5" y="6" width="3" height="7" rx=".8"/>',
  glue: '<path d="M2.5 4.2h11l-1.3 9a1 1 0 0 1-1 .8H4.8a1 1 0 0 1-1-.8z"/><ellipse cx="8" cy="4.2" rx="5.5" ry="1.7"/>',
  blob_storage: '<path d="M4.5 12.5h7a3 3 0 0 0 .4-6A4 4 0 0 0 4.3 7a2.8 2.8 0 0 0 .2 5.5z"/>',
  adx: '<path d="M2 13.5h12"/><path d="M4 11V8M7 11V4.5M10 11V6.5M13 11V9"/>',
  files: '<path d="M4 1.5h5l3.5 3.5v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-11.5a1 1 0 0 1 1-1z"/><path d="M9 1.5V5h3.5"/>',
  python: '<path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5"/>',
  duckdb: '<rect x="2" y="2.5" width="12" height="11" rx="1"/><path d="M2 6h12M6 6v7.5"/>',
  api: '<path d="M6 1.5v3M10 1.5v3M4 4.5h8v3a4 4 0 0 1-8 0zM8 11.5v3"/>',
};
const ICON_NAMES = {database: "SQL database", sharepoint: "SharePoint", servicenow: "ServiceNow", insightvm: "InsightVM",
  axonius: "Axonius", glue: "S3 / Glue", blob_storage: "Azure Blob Storage", adx: "Azure Data Explorer",
  files: "local files", python: "Python module", duckdb: "DuckDB", dataset: "taken-over answer", api: "API"};
function icon(kind) {
  if (!kind) return "";
  const k = ICONS[kind] ? kind : "api";
  return `<svg class="ico" viewBox="0 0 16 16" role="img" aria-label="${esc(ICON_NAMES[k])}"><title>${esc(ICON_NAMES[k])}</title>${ICONS[k]}</svg>`;
}
function openTab(name) { document.querySelector(`nav button[data-tab="${name}"]`)?.click(); }

// ---- SQL tab -----------------------------------------------------------------------------
let TABLES = [];
// the expanded catalog, per connector: service → {tables, notes, loading}; EXPANDED = the connectors shown expanded
const NESTED = {}, EXPANDED = new Set();
// connector groups the list shows closed — remembered in this browser
const COLLAPSED = new Set((() => { try { return JSON.parse(store.get("duckduck-collapsed") || "[]"); } catch { return []; } })());
function keepCollapsed() { store.set("duckduck-collapsed", JSON.stringify([...COLLAPSED])); }
function groupsOf() { return [...new Set(TABLES.map(t => t.service || "other"))]; }
async function loadTables() {
  loadConnections();
  try { TABLES = await api("/api/tables"); drawTables(); }
  catch (err) { TABLES = []; $("#tablelist").innerHTML = `<p class="error small">${esc(err.message)}</p>`; }
  EXPANDED.forEach(svc => loadNested(svc, false));
}
// which connectors in duckduck.json started, and why the others didn't — why the list is what it is
async function loadConnections() {
  let c;
  try { c = await api("/api/connections"); } catch { $("#connstatus").innerHTML = ""; return; }
  const svcs = c.services || [], failed = svcs.filter(x => !x.started), ok = svcs.length - failed.length;
  const where = c.config_path ? `<div class="muted mono" style="word-break:break-all">${esc(c.config_path)}</div>` : "";
  if (!svcs.length) {
    $("#connstatus").innerHTML = `<div class="connbox bad"><b>No connectors configured.</b> Add them under
      <span class="mono">services</span> in duckduck.json${META?.features?.config ? " (Config tab)" : ""}.${where}</div>`;
    return;
  }
  // one line when all is well; what failed folds under it
  $("#connstatus").innerHTML = `<div class="connbox ${failed.length ? "bad" : ""}">
    <span class="dot ${failed.length ? "bad" : "ok"}" aria-hidden="true"></span><b>${ok} of ${svcs.length}</b> connector${svcs.length === 1 ? "" : "s"} started
    ${failed.length ? `<details><summary>${failed.length} didn't start — why</summary><ul>${failed.map(x => `<li><b>${esc(x.name)}</b>
      <span class="muted">(${esc(x.connector)})</span>: <span class="err">${esc(x.error || "unknown error")}</span></li>`).join("")}</ul>${where}</details>` : ""}
    ${svcs.some(x => x.started && !x.tables) ? `<div class="muted">Started with no tables: ${esc(svcs.filter(x => x.started && !x.tables).map(x => x.name).join(", "))}</div>` : ""}
    ${!failed.length && !c.tables ? where : ""}</div>`;
}
// one connector's catalogs: the tables behind them (Glue, ADX, a database…), under each catalog
async function loadNested(svc, refresh) {
  EXPANDED.add(svc); NESTED[svc] = {...(NESTED[svc] || {tables: [], notes: []}), loading: true};
  drawTables();
  let got;
  try { got = await api("/api/tables/nested?" + new URLSearchParams({service: svc, ...(refresh ? {refresh: 1} : {})})); }
  catch (err) { got = {tables: [], notes: [err.message]}; }
  NESTED[svc] = {tables: got.tables, notes: got.notes, loading: false};
  drawTables();
}
function collapseNested(svc) { EXPANDED.delete(svc); drawTables(); }
function previewSql(sql) { $("#sqltext").value = sql; runSql(); }
// nested rows saved in this session stay listed, marked; ones saved before are skipped (they're tables above)
const JUST_SAVED = new Set(), SHOW_SAVED = new Set();
const nestedKey = (table, args) => table + "|" + JSON.stringify(Object.keys(args || {}).sort().map(k => [k, String(args[k])]));
const nestedUsage = (n) => n.address ? `SELECT * FROM ${n.address} LIMIT 100` : n.usage;
const KIND_TAGS = {"table function": "fn", catalog: "catalog", "raw query": "raw"};
function drawTables() {
  const f = $("#tablefilter").value.trim().toLowerCase();
  const byCatalog = {};
  EXPANDED.forEach(svc => (NESTED[svc]?.tables || []).forEach(n => (byCatalog[n.catalog] ||= []).push(n)));
  const matches = (n) => !f || `${n.label} ${n.table} ${n.address || ""} ${n.saved_as || ""}`.toLowerCase().includes(f);
  const nestedOf = t => (byCatalog[t.name] || []).filter(matches);
  const rows = TABLES.filter(t => !f || `${t.name} ${t.address || ""} ${t.description || ""} ${t.service || ""}`.toLowerCase().includes(f)
                                 || nestedOf(t).length);
  const groups = {};
  rows.forEach(t => (groups[t.service || "other"] ||= []).push(t));
  const MAXN = 200, canSave = !!META?.features?.saved_tables;
  // a connector with a catalog gets its own toggle: expand just its catalog
  const toggle = (svc, ts) => {
    if (!ts.some(t => t.expandable)) return "";
    const st = NESTED[svc], on = EXPANDED.has(svc);
    if (on && st?.loading) return `<span class="gacts muted small"><span class="spin" aria-hidden="true"></span> reading…</span>`;
    return `<span class="gacts">${on && st ? `<button type="button" class="iconbtn" data-exprefresh="${esc(svc)}" title="Read ${esc(svc)}'s catalog again">↻</button>` : ""}
      <button type="button" class="pillbtn ${on ? "on" : ""}" data-expand="${esc(svc)}" aria-pressed="${on}"
        title="${on ? "Hide the tables behind this connector's catalog" : "List the tables behind this connector's catalog (reads it now)"}">${on ? "Hide catalog" : "Expand catalog"}</button></span>`;
  };
  const short = (t) => t.address ? t.address.split(".").slice(1).join(".").replace(/"/g, "") : t.name;
  const tableRow = (t, kids) => {
    const tip = [t.name + (t.address && t.address !== t.name ? `  ·  ${t.address}` : ""), t.description,
                 t.pushdown ? "push-down: " + t.pushdown : ""].filter(Boolean).join("\n");
    const tag = t.saved ? `<span class="savedtag" title="${t.saved === "bound" ? "A saved table over a table function" : "A saved query"} — kept in duckduck.json">saved</span>`
      : KIND_TAGS[t.kind] ? `<span class="ktag" title="${esc(t.kind)}">${KIND_TAGS[t.kind]}</span>` : "";
    return `<div class="trow ${t.saved ? "saved" : ""}"><button class="titem" type="button" data-usage="${esc(t.usage || ("SELECT * FROM " + t.name + " LIMIT 100"))}" title="${esc(tip)}">
      ${icon(t.icon)}<span class="tname">${esc(short(t))}</span>${tag}${kids ? `<span class="kcount">${kids}</span>` : ""}</button>
      ${t.saved ? `<button type="button" class="editbtn" data-editview="${esc(t.name)}" title="See and edit the statement behind it">✎ Edit</button>` : ""}</div>`;
  };
  const nestedBlock = (t, svc) => {
    const all = byCatalog[t.name] || [], kids = nestedOf(t);
    if (!EXPANDED.has(svc) || NESTED[svc]?.loading || !t.expandable) return "";
    if (!all.length) return `<div class="nested"><div class="nhead muted small">No tables listed</div></div>`;
    const savedBefore = kids.filter(n => n.saved_as && !JUST_SAVED.has(nestedKey(n.table, n.args)));
    const shown = SHOW_SAVED.has(t.name) ? kids : kids.filter(n => !savedBefore.includes(n));
    const todo = all.filter(n => !n.saved_as);
    return `<div class="nested"><div class="nhead"><span class="small"><b>${all.length}</b> behind it${all.length - todo.length ? ` · <span class="okc">${all.length - todo.length} saved</span>` : ""}</span>
        ${canSave && todo.length ? `<button type="button" class="pillbtn accent" data-regall="${esc(t.name)}"
          title="Save each of the ${todo.length} not saved yet as a table of its own (the ones already saved are skipped)">Register all ${todo.length}</button>` : ""}</div>
      ${shown.slice(0, MAXN).map(n => {
        const use = nestedUsage(n), key = nestedKey(n.table, n.args);
        return `<div class="nitem ${n.saved_as ? "saved" : ""} ${JUST_SAVED.has(key) ? "just" : ""}" title="${esc(n.address || n.usage)}">
          <span class="lbl" data-usage="${esc(n.saved_as ? `SELECT * FROM ${n.saved_as} LIMIT 100` : use)}">${esc(n.label)}</span>
          ${n.saved_as ? `<span class="savedok" title="Saved as the table ${esc(n.saved_as)}">✓ ${esc(n.saved_as)}</span>
              <button type="button" class="editbtn" data-editview="${esc(n.saved_as)}" title="See and edit its statement">✎</button>`
            : `${canSave ? `<button class="mini" type="button" data-savetable="${esc(use)}" title="Keep it as a table of its own, with a name">＋ Table</button>` : ""}
              <button class="mini" type="button" data-preview="${esc(use)}" title="Run SELECT * … LIMIT 100">Preview</button>`}</div>`; }).join("")}
      ${shown.length > MAXN ? `<div class="muted small">+${shown.length - MAXN} more — filter to find them</div>` : ""}
      ${savedBefore.length ? `<div class="muted small nnote">${savedBefore.length} already saved ${SHOW_SAVED.has(t.name) ? "" : "(listed above as tables) — skipped"}
        <button type="button" class="linkish" data-showsaved="${esc(t.name)}">${SHOW_SAVED.has(t.name) ? "hide them" : "show them"}</button></div>` : ""}</div>`;
  };
  $("#tablecount").textContent = `${TABLES.length} table${TABLES.length === 1 ? "" : "s"}`;
  $("#tablelist").innerHTML = Object.entries(groups).map(([svc, ts]) => {
    const shut = COLLAPSED.has(svc) && !f;  // a filter shows what it matched, collapsed or not
    return `<div class="tgroup"><div class="ghead"><button type="button" class="gtoggle" data-group="${esc(svc)}" aria-expanded="${!shut}"
      title="${shut ? "Show" : "Hide"} ${esc(svc)}'s tables"><span class="chev" aria-hidden="true">▸</span><span class="gname">${esc(svc)}</span>
      <span class="gcount">${ts.length}</span></button>${shut ? "" : toggle(svc, ts)}</div>` + (shut ? "" : ts.map(t => {
      const kids = (byCatalog[t.name] || []).length;
      return tableRow(t, kids) + nestedBlock(t, svc);
    }).join("") + (EXPANDED.has(svc) && NESTED[svc]?.notes?.length
      ? `<div class="muted small nested">${NESTED[svc].notes.map(esc).join("<br>")}</div>` : "")) + `</div>`;
  }).join("")
    || (TABLES.length ? `<p class="muted small">Nothing matches.</p>` : `<p class="muted small">No tables registered — see above for why.</p>`);
  const on = (sel, fn) => $("#tablelist").querySelectorAll(sel).forEach(b => b.addEventListener("click", (e) => { e.stopPropagation(); fn(b); }));
  on("[data-usage]", b => {
    let u = b.dataset.usage;
    if (!/^\s*(select|with|from|show|describe)/i.test(u)) u = `SELECT * FROM ${u}`;
    if (!/\blimit\b/i.test(u)) u += " LIMIT 100";
    $("#sqltext").value = u; $("#sqltext").focus();
  });
  on("[data-preview]", b => previewSql(b.dataset.preview));
  on("[data-editview]", b => editSavedTable(b.dataset.editview));
  on("[data-group]", b => { const g = b.dataset.group; if (COLLAPSED.has(g)) COLLAPSED.delete(g); else COLLAPSED.add(g); keepCollapsed(); drawTables(); });
  on("[data-savetable]", b => saveAsTable(b.dataset.savetable.replace(/\s+limit\s+\d+\s*$/i, "")));
  on("[data-expand]", b => EXPANDED.has(b.dataset.expand) ? collapseNested(b.dataset.expand) : loadNested(b.dataset.expand, false));
  on("[data-exprefresh]", b => loadNested(b.dataset.exprefresh, true));
  on("[data-showsaved]", b => { const c = b.dataset.showsaved; if (SHOW_SAVED.has(c)) SHOW_SAVED.delete(c); else SHOW_SAVED.add(c); drawTables(); });
  on("[data-regall]", b => registerAll(b.dataset.regall, b));
}
// "Register all": every table behind a catalog not saved yet becomes a saved table (one write; already-saved ones skipped)
async function registerAll(catalog, button) {
  const todo = Object.values(NESTED).flatMap(x => x.tables || []).filter(n => n.catalog === catalog && !n.saved_as);
  if (!todo.length) return;
  if (!confirm(`Save ${todo.length} table${todo.length === 1 ? "" : "s"} behind ${catalog} as tables of their own?\n\n` +
    `Each gets a name like ${todo[0].address ? todo[0].address.replace(/"/g, "").replace(/\./g, "_") : "its arguments"} — kept in duckduck.json (a .bak is kept). ` +
    `Ones already saved are skipped. Rename or remove any later with ✎ Edit.`)) return;
  button.disabled = true; button.textContent = "Saving…";
  let r;
  try { r = await api("/api/views/many", {items: todo.map(n => ({table: n.table, args: n.args}))}); }
  catch (err) { toast(err.message); button.disabled = false; button.textContent = `Register all ${todo.length}`; return; }
  (r.created || []).forEach(v => { JUST_SAVED.add(nestedKey(v.table, v.args)); draftViews(views => { views[v.name] = {table: v.table, args: v.args}; }); });
  const skipped = (r.skipped || []).length;
  toast(`Saved ${(r.created || []).length} table${(r.created || []).length === 1 ? "" : "s"}` + (skipped ? ` · ${skipped} skipped` : ""));
  if (skipped) console.info("skipped:", r.skipped);
  afterSavedTables();
}
$("#tablefilter").addEventListener("input", drawTables);
$("#groupsopen").addEventListener("click", () => { COLLAPSED.clear(); keepCollapsed(); drawTables(); });
$("#groupsclose").addEventListener("click", () => { groupsOf().forEach(g => COLLAPSED.add(g)); keepCollapsed(); drawTables(); });
// a query runs as a job: its steps and log live, ⏸ Pause (between API calls and pages), ✕ Cancel (DuckDB too)
let SQLJOB = null;  // {id, timer, debug}
try { $("#sqldebug").checked = store.get("duckduck-sqldebug") === "1"; } catch {}
$("#sqldebug").addEventListener("change", () => store.set("duckduck-sqldebug", $("#sqldebug").checked ? "1" : "0"));
async function runSql() {
  const sql = $("#sqltext").value.trim(); if (!sql) return;
  if (SQLJOB) await sqlJobAction("cancel");  // a new run replaces the one still running
  const debug = $("#sqldebug").checked;
  $("#sqlresult").innerHTML = `<div class="card muted">Starting…</div>`;
  let r;
  try { r = await api("/api/sql", {sql, debug, background: true}); }
  catch (err) { $("#sqlresult").innerHTML = `<div class="card error">${esc(err.message)}</div>`; return; }
  if (!r.job_id) { drawSqlResult(r); return; }
  SQLJOB = {id: r.job_id, debug};
  pollSqlJob();
}
async function pollSqlJob() {
  const job = SQLJOB; if (!job) return;
  let v;
  try { v = await api(`/api/jobs/${job.id}`); }
  catch (err) { SQLJOB = null; $("#sqlresult").innerHTML = `<div class="card error">${esc(err.message)}</div>`; return; }
  if (SQLJOB !== job) return;  // replaced meanwhile
  if (v.state === "done" || v.state === "failed" || v.state === "cancelled") SQLJOB = null;
  if (v.state === "done") drawSqlResult(v.result);
  else if (v.state === "failed") $("#sqlresult").innerHTML = `<div class="card error">${esc(v.error?.message || "failed")}</div>`;
  else drawSqlJob(v, job.debug);
  if (SQLJOB === job) job.timer = setTimeout(pollSqlJob, v.state === "paused" ? 1000 : 350);
}
async function sqlJobAction(action) {
  const job = SQLJOB; if (!job) return;
  if (action === "cancel") { clearTimeout(job.timer); SQLJOB = null; }
  let v;
  try { v = await api(`/api/jobs/${job.id}/${action}`, {}); } catch (err) { toast(err.message); return; }
  if (action === "cancel") drawSqlJob({...v, state: "cancelled"}, job.debug);
  else if (SQLJOB === job) drawSqlJob(v, job.debug);
}
const SQL_STATE = {running: "Running", pausing: "Pausing…", paused: "Paused", cancelled: "Cancelled"};
function drawSqlJob(v, debug) {
  const st = v.state, live = st === "running" || st === "pausing", steps = (v.events || []).filter(Boolean);
  const secs = (ms) => ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
  const fetched = Object.entries(v.partial?.fetched || {});
  const log = v.log || [];
  $("#sqlresult").innerHTML = `<div class="card" aria-live="polite">
    <div class="jobhead">${live ? `<span class="spin" aria-hidden="true"></span>` : ""}
      <strong>${esc(SQL_STATE[st] || st)}</strong><span class="muted small">${v.elapsed ? secs(v.elapsed) : ""}</span>
      <span class="grow"></span>
      ${st === "running" ? `<button class="secondary" type="button" data-sqljob="pause" title="Stop before the next API call or page">⏸ Pause</button>` : ""}
      ${st === "paused" || st === "pausing" ? `<button class="secondary" type="button" data-sqljob="resume">▶ Continue</button>` : ""}
      ${st !== "cancelled" ? `<button class="secondary" type="button" data-sqljob="cancel" title="Stop the query">✕ Cancel</button>` : ""}
    </div>
    <ol class="steps">${steps.map((e, k) => {
      const now = k === steps.length - 1 && live;
      return `<li class="${now ? "now" : "done"}"><span class="mark">${now ? `<span class="spin" aria-hidden="true"></span>` : k < steps.length - 1 ? "✓" : st === "cancelled" ? "✕" : st === "paused" ? "⏸" : "✓"}</span>
        <span>${esc(e.text)}</span><span class="at">${secs(e.ms)}</span></li>`; }).join("")
      || `<li class="now"><span class="mark"><span class="spin"></span></span><span>Starting…</span></li>`}</ol>
    ${st === "pausing" ? `<p class="jobnote">A call already in flight finishes first — then it stops. DuckDB's own step can't pause; cancel stops it.</p>` : ""}
    ${st === "cancelled" ? `<p class="jobnote">Stopped. Nothing else is read.</p>` : ""}
    ${fetched.length ? `<div class="muted small" style="margin-top:6px">Read so far: ${fetched.map(([t, f]) =>
      `<span class="mono">${esc(t)}</span> ${(f.rows ?? 0).toLocaleString()} rows${f.pages ? ` (${f.pages} pages)` : ""}`).join(" · ")}</div>` : ""}
    <details class="sqllog" ${log.length && (debug || st !== "running") ? "open" : ""}><summary>Log${debug ? " (debug)" : ""} · ${log.length} line${log.length === 1 ? "" : "s"}</summary>
      <pre class="log mono">${esc(log.join("\n"))}</pre></details>
  </div>`;
  const pre = $("#sqlresult pre.log"); if (pre) pre.scrollTop = pre.scrollHeight;
  $("#sqlresult").querySelectorAll("[data-sqljob]").forEach(b => b.addEventListener("click", () => sqlJobAction(b.dataset.sqljob)));
}
let LAST_SQL = null;  // {result, sql}: what ⤢ Expand opens
function drawSqlResult(r) {
  const rows = (r.rows || []).map(row => Object.fromEntries(r.columns.map((c, i) => [c, row[i]])));
  LAST_SQL = r.error ? null : {result: r, sql: $("#sqltext").value.trim()};
  $("#sqlresult").innerHTML = `<div class="card sqlres">` + (r.error ? `<p class="error">${esc(r.error)}</p>` :
    `<div class="resbar"><span class="muted small">${r.row_count.toLocaleString()} row${r.row_count === 1 ? "" : "s"} · ${r.columns.length} column${r.columns.length === 1 ? "" : "s"} · ${r.elapsed_ms} ms${r.truncated ? " · first " + rows.length.toLocaleString() + " shown here" : ""}</span>
      <span class="grow"></span>${r.columns.length ? `<button class="secondary" type="button" id="sqlexpand" title="Open the result full screen: search, sort, every row, CSV">⤢ Expand</button>` : ""}</div>${table(rows)}`) +
    ((r.log || []).length ? `<details${r.error || r.debug ? " open" : ""}><summary>${r.debug ? "Debug log" : "What went to each source (push-down)"}</summary><pre class="log mono">${esc(r.log.join("\n"))}</pre></details>` : "") + `</div>`;
}
$("#sqlresult").addEventListener("click", (e) => {
  if (e.target.closest("#sqlexpand") && LAST_SQL) openViewer(resultSource(LAST_SQL.result), LAST_SQL.sql || "Result");
});
$("#sqlrun").addEventListener("click", runSql);

// ---- the result viewer: a query's whole result full screen — search, sort, pages, columns, a value in full, CSV ----
// A source gives pages: resultSource (a SQL result the server keeps whole) or rowsSource (rows the page already has).
function resultSource(r) {
  if (!r.result_id) return rowsSource((r.rows || []).map(row => Object.fromEntries(r.columns.map((c, i) => [c, row[i]]))));
  const params = (o) => new URLSearchParams(Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "")));
  return {
    fetch: (o) => api(`/api/sql/results/${r.result_id}?` + params({offset: o.offset, limit: o.limit, sort: o.sort, desc: o.desc ? 1 : 0, q: o.q})),
    csv: async (o) => {
      const headers = {}; const t = store.get("duckduck-token"); if (t) headers["X-Duckduck-Token"] = t;
      const res = await fetch(`/api/sql/results/${r.result_id}/csv?` + params({sort: o.sort, desc: o.desc ? 1 : 0, q: o.q, columns: o.columns.join(",")}), {headers});
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      return res.blob();
    },
  };
}
function rowsSource(rows) {
  const columns = rows.length ? Object.keys(rows[0]) : [];
  const numbered = rows.map((row, i) => ({row, n: i + 1}));
  const view = (o) => {
    let out = numbered;
    if (o.q) { const q = o.q.toLowerCase(); out = out.filter(({row}) => columns.some(c => cellText(row[c]).toLowerCase().includes(q))); }
    if (o.sort) {
      const k = o.sort, dir = o.desc ? -1 : 1;
      out = [...out].sort((a, b) => {
        const x = a.row[k], y = b.row[k];
        if (x === null || x === undefined) return (y === null || y === undefined) ? a.n - b.n : 1;
        if (y === null || y === undefined) return -1;
        const c = typeof x === "number" && typeof y === "number" ? x - y : cellText(x).localeCompare(cellText(y), undefined, {numeric: true});
        return c ? c * dir : a.n - b.n;
      });
    }
    return out;
  };
  return {
    fetch: async (o) => { const all = view(o), page = all.slice(o.offset, o.offset + o.limit);
      return {columns, rows: page.map(({row}) => columns.map(c => row[c] ?? null)), row_numbers: page.map(p => p.n),
              total: rows.length, filtered: all.length, offset: o.offset, limit: o.limit}; },
    csv: async (o) => {
      const cell = (v) => { const t = v === null || v === undefined ? "" : cellText(v); return /[",\n\r]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
      const lines = [o.columns.map(cell).join(","), ...view(o).map(({row}) => o.columns.map(c => cell(row[c])).join(","))];
      return new Blob([lines.join("\n") + "\n"], {type: "text/csv"});
    },
  };
}
const cellText = (v) => v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
let VW = null;  // {source, title, columns, hidden, sort, desc, q, offset, size, page, sel, timer}
async function openViewer(source, title) {
  VW = {source, title, columns: [], hidden: new Set(), sort: null, desc: false, q: "", offset: 0,
        size: Number($("#vwsize").value) || 200, page: null, sel: null, timer: null};
  $("#vwtitle").textContent = title; $("#vwtitle").title = title;
  $("#vwsearch").value = ""; $("#vwcell").hidden = true; $("#vwcols").open = false;
  try { $("#vwwrap").checked = store.get("duckduck-vwwrap") === "1"; } catch {}
  $("#vwgrid").innerHTML = `<div class="vwempty">Loading…</div>`;
  $("#viewer").showModal();
  await loadViewerPage();
}
async function loadViewerPage() {
  const vw = VW; if (!vw) return;
  let page;
  try { page = await vw.source.fetch({offset: vw.offset, limit: vw.size, sort: vw.sort, desc: vw.desc, q: vw.q}); }
  catch (err) { $("#vwgrid").innerHTML = `<div class="vwempty error">${esc(err.message)}</div>`; return; }
  if (VW !== vw) return;
  vw.page = page;
  if (!vw.columns.length) { vw.columns = page.columns; drawViewerColumns(); }
  drawViewer();
}
function drawViewer() {
  const vw = VW, p = vw.page, cols = p.columns.map((c, i) => ({c, i})).filter(({c}) => !vw.hidden.has(c));
  const num = cols.map(({i}) => p.rows.length && p.rows.every(r => r[i] === null || typeof r[i] === "number"));
  $("#vwcount").textContent = `${p.total.toLocaleString()} row${p.total === 1 ? "" : "s"} · ${p.columns.length} column${p.columns.length === 1 ? "" : "s"}` +
    (vw.q ? ` · ${p.filtered.toLocaleString()} match “${vw.q}”` : "") + (vw.hidden.size ? ` · ${vw.hidden.size} hidden` : "");
  const arrow = (c) => vw.sort === c ? `<span class="arrow">${vw.desc ? "▼" : "▲"}</span>` : "";
  $("#vwgrid").innerHTML = !p.rows.length ? `<div class="vwempty">${vw.q ? "No row matches." : "No rows."}</div>` :
    `<table class="vwtable${$("#vwwrap").checked ? " wrap" : ""}"><thead><tr><th class="rn" title="Row number in the result">#</th>
      ${cols.map(({c}, k) => `<th class="${num[k] ? "num" : ""}" data-sort="${esc(c)}" title="Sort by ${esc(c)}">${esc(c)}${arrow(c)}</th>`).join("")}</tr></thead>
    <tbody>${p.rows.map((r, ri) => `<tr><td class="rn">${p.row_numbers[ri].toLocaleString()}</td>${cols.map(({i}, k) => {
      const v = r[i];
      return `<td class="${num[k] ? "num" : ""}" data-r="${ri}" data-c="${i}">${v === null || v === undefined ? '<span class="muted">—</span>'
        : typeof v === "object" ? `<span class="mono jcell">${esc(JSON.stringify(v))}</span>` : esc(v)}</td>`; }).join("")}</tr>`).join("")}</tbody></table>`;
  const last = Math.min(p.offset + p.rows.length, p.filtered);
  $("#vwrange").textContent = p.filtered ? `Rows ${(p.offset + 1).toLocaleString()}–${last.toLocaleString()} of ${p.filtered.toLocaleString()}` : "";
  $("#vwprev").disabled = p.offset <= 0;
  $("#vwnext").disabled = p.offset + p.rows.length >= p.filtered;
}
function drawViewerColumns() {
  $("#vwcollist").innerHTML = VW.columns.map(c => `<label><input type="checkbox" data-col="${esc(c)}" ${VW.hidden.has(c) ? "" : "checked"}> <span class="mono">${esc(c)}</span></label>`).join("");
}
$("#vwcollist").addEventListener("change", (e) => {
  const c = e.target.dataset.col; if (c === undefined) return;
  if (e.target.checked) VW.hidden.delete(c); else VW.hidden.add(c);
  drawViewer();
});
$("#vwallcols").addEventListener("click", () => { VW.hidden.clear(); drawViewerColumns(); drawViewer(); });
$("#vwnocols").addEventListener("click", () => { VW.columns.forEach(c => VW.hidden.add(c)); drawViewerColumns(); drawViewer(); });
$("#vwgrid").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (th) {  // ascending → descending → as it came
    const c = th.dataset.sort;
    if (VW.sort !== c) { VW.sort = c; VW.desc = false; } else if (!VW.desc) VW.desc = true; else { VW.sort = null; VW.desc = false; }
    VW.offset = 0; loadViewerPage(); return;
  }
  const td = e.target.closest("td[data-c]"); if (!td) return;
  $("#vwgrid").querySelectorAll("td.sel").forEach(x => x.classList.remove("sel")); td.classList.add("sel");
  const p = VW.page, ri = Number(td.dataset.r), ci = Number(td.dataset.c), v = p.rows[ri][ci];
  VW.sel = v;
  $("#vwcellname").textContent = p.columns[ci];
  $("#vwcellwhere").textContent = `row ${p.row_numbers[ri].toLocaleString()}` + (v !== null && typeof v === "object" ? " · JSON" : "");
  $("#vwcellvalue").textContent = v === null || v === undefined ? "NULL" : typeof v === "object" ? JSON.stringify(v, null, 2) : String(v);
  $("#vwcell").hidden = false;
});
$("#vwcellclose").addEventListener("click", () => { $("#vwcell").hidden = true; $("#vwgrid").querySelectorAll("td.sel").forEach(x => x.classList.remove("sel")); });
$("#vwcellcopy").addEventListener("click", () => copyText($("#vwcellvalue").textContent, "Value copied"));
$("#vwsearch").addEventListener("input", () => {
  clearTimeout(VW?.timer);
  if (VW) VW.timer = setTimeout(() => { VW.q = $("#vwsearch").value.trim(); VW.offset = 0; loadViewerPage(); }, 300);
});
$("#vwwrap").addEventListener("change", () => { store.set("duckduck-vwwrap", $("#vwwrap").checked ? "1" : "0"); if (VW?.page) drawViewer(); });
$("#vwsize").addEventListener("change", () => { if (!VW) return; VW.size = Number($("#vwsize").value); VW.offset = 0; loadViewerPage(); });
$("#vwprev").addEventListener("click", () => { VW.offset = Math.max(0, VW.offset - VW.size); loadViewerPage(); });
$("#vwnext").addEventListener("click", () => { VW.offset += VW.size; loadViewerPage(); });
$("#vwclose").addEventListener("click", () => $("#viewer").close());
$("#viewer").addEventListener("close", () => { VW = null; });
$("#viewer").addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $("#vwsearch")) { e.preventDefault(); $("#vwsearch").focus(); }
});
$("#vwcopy").addEventListener("click", () => {
  const p = VW?.page; if (!p) return;
  const keep = p.columns.map((c, i) => i).filter(i => !VW.hidden.has(p.columns[i]));
  const clean = (v) => cellText(v).replace(/[\t\n\r]+/g, " ");
  const text = [keep.map(i => p.columns[i]).join("\t"), ...p.rows.map(r => keep.map(i => clean(r[i])).join("\t"))].join("\n");
  copyText(text, `${p.rows.length.toLocaleString()} row${p.rows.length === 1 ? "" : "s"} copied`);
});
$("#vwcsv").addEventListener("click", async () => {
  if (!VW) return;
  const b = $("#vwcsv"); b.disabled = true; b.textContent = "Preparing…";
  try {
    const blob = await VW.source.csv({sort: VW.sort, desc: VW.desc, q: VW.q, columns: VW.columns.filter(c => !VW.hidden.has(c))});
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "result.csv";
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (err) { toast(err.message); }
  finally { b.disabled = false; b.textContent = "Download CSV"; }
});
async function copyText(text, done) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const t = document.createElement("textarea"); t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
    $("#viewer").open ? $("#viewer").append(t) : document.body.append(t);
    t.select(); try { document.execCommand("copy"); } finally { t.remove(); }
  }
  toast(done);
}
$("#sqltext").addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runSql(); } });

// ---- Semantic catalog (Config tab): generate it with the LLM, as a job with its log live ----------
let CAT = null, CATJOB = null;  // CATJOB: {id, lines, timer}
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
}
async function loadCatalog() {
  try { CAT = await api("/api/catalog"); } catch (err) { $("#catnote").innerHTML = `<span class="msg bad">${esc(err.message)}</span>`; return; }
  const info = CAT.info || {};
  $("#catpath").textContent = info.path || "";
  $("#catllm").textContent = info.llm ? `drafted by ${info.llm}` : (CAT.off ? "" : "no LLM configured for catalog_generation");
  const off = CAT.off || (!info.llm ? "no LLM is configured for catalog generation (default_llm or catalog_generation.llm)" : null);
  const running = CAT.job?.state === "running";
  $("#catupdate").disabled = $("#catall").disabled = !!off || running;
  $("#catnote").className = "small " + (off ? "muted" : "muted");
  $("#catnote").textContent = off ? off : (info.max_age ? `Update drafts new tables and those older than ${info.max_age}` : "Update drafts the tables not in the catalog yet")
    + (info.max_tables ? ` · at most ${info.max_tables} per run` : "") + (off ? "" : ".");
  drawCatalogTables(!!off || running);
  drawConnections();
  drawEnable(info, off, running);
  if (CFGPAGE === "overview" && !$("#tab-config").hidden) drawOverview();
  if (CAT.job && (!CATJOB || CATJOB.id !== CAT.job.id)) watchJob(CAT.job);  // a job started elsewhere, or before a reload
}
function drawCatalogTables(disabled) {
  const src = CAT.sources || [], missing = CAT.not_in_catalog || [];
  $("#catcount").textContent = `Tables — ${src.length} in the catalog` + (missing.length ? ` · ${missing.length} not yet` : "");
  const btn = (label, attr, title) => `<button class="secondary" type="button" ${attr} title="${esc(title)}" style="padding:3px 10px;font-size:12.5px" ${disabled ? "disabled" : ""}>${label}</button>`;
  $("#catlist").innerHTML = `<div class="tablewrap"><table><thead><tr><th>table</th><th>reads</th><th class="num">fields</th><th>drafted</th><th></th></tr></thead><tbody>
    ${src.map(t => `<tr><td><strong>${esc(t.name)}</strong>${t.notes ? ` <span class="muted small" title="has your notes — kept when redrafted">✎ notes</span>` : ""}
        ${t.description ? `<div class="muted small">${esc(t.description)}</div>` : ""}</td>
      <td class="mono">${t.relation ? "DuckDB relation" : esc(t.table || "") + (Object.keys(t.args || {}).length ? `(${esc(Object.entries(t.args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", "))})` : "")}</td>
      <td class="num">${t.fields}</td>
      <td class="small">${t.generated_at ? `${esc(ago(t.generated_at))}<div class="muted">${esc(t.generated_by || "")}</div>` : `<span class="muted">written by hand</span>`}</td>
      <td>${t.relation ? "" : btn("Redraft", `data-redraft="${esc(t.name)}"`, t.generated_at ? "Draft this table again" : "Replace the hand-written description with a draft (your notes are kept)")}</td></tr>`).join("")}
    ${missing.map(n => `<tr><td><span class="muted">${esc(n)}</span> <span class="pill">not in the catalog</span></td><td class="mono">${esc(n)}</td><td></td><td></td>
      <td>${btn("Add", `data-add="${esc(n)}"`, "Draft this table into the catalog")}</td></tr>`).join("")}
    </tbody></table></div>`;
  $("#catlist").querySelectorAll("[data-redraft]").forEach(b => b.addEventListener("click", () => startGeneration({force: [b.dataset.redraft]})));
  $("#catlist").querySelectorAll("[data-add]").forEach(b => b.addEventListener("click", () => startGeneration({only: [b.dataset.add]})));
}
// ---- enable Ask: what's missing, in order, with the button that does the last step ----
function drawEnable(info, off, running) {
  const setup = META?.setup;
  $("#enablecard").hidden = !setup;
  if (!setup) return;
  const hasLlm = !!info.llm, canEdit = !!META?.features?.catalog_generation;
  const step = (ok, text) => `<li><span class="${ok ? "ok" : "todo"}">${ok ? "✓" : "○"}</span> ${text}</li>`;
  $("#enablesteps").innerHTML =
    step(hasLlm, hasLlm ? `An LLM to draft it: <b>${esc(info.llm)}</b>`
      : `Add an LLM on the <a href="#" data-cfggo="ai">AI providers</a> page and pick it as the <b>Default LLM</b> there — then Save.`) +
    step(canEdit, canEdit ? "Drafting from this page is on."
      : `Restart the server with <span class="mono">--edit-config</span> to draft it from here — or run
         <span class="mono">python -m duckduck.semantic generate-catalog</span> and reload.`) +
    step(false, hasLlm && canEdit
      ? `<button class="primary" type="button" id="enablego" ${running ? "disabled" : ""}>${running ? "Drafting…" : "Draft the catalog now"}</button>
         <span class="muted small">one LLM call per table · Ask turns on by itself when it's done</span>`
      : "Draft the catalog (the button appears here once the steps above are done).");
  $("#enablego")?.addEventListener("click", () => startGeneration({}));
}
// the quick link: #enable-ask, from the banner on every tab (or a bookmarked / printed URL)
function goEnable() {
  if (META?.features?.config) {
    CFGPAGE = "overview"; openTab("config");
    setTimeout(() => $("#enablecard").scrollIntoView({behavior: "smooth", block: "start"}), 150);
  }
  else { openTab("ask"); $("#setupcard").scrollIntoView({behavior: "smooth"}); }
}
$("#enablelink").addEventListener("click", (e) => { e.preventDefault(); history.replaceState(null, "", "#enable-ask"); goEnable(); });

// ---- catalog connections: is each table's system connected, and why not ----
const CONN_CHECKS = {};  // source → the last Test result
function drawConnections() {
  const rows = CAT?.status || [], bad = rows.filter(r => !r.connected).length;
  $("#conncount").textContent = rows.length ? `${rows.length - bad} connected` + (bad ? ` · ${bad} not connected` : "") : "";
  if (!rows.length) { $("#connlist").innerHTML = `<p class="muted small">The catalog has no tables yet.</p>`; return; }
  const check = r => {
    const c = CONN_CHECKS[r.source]; if (!c) return "";
    if (c.running) return `<div class="conncheck muted"><span class="spin" aria-hidden="true"></span> reading one row…</div>`;
    return c.ok ? `<div class="conncheck connok">✓ answered in ${c.ms.toLocaleString()} ms · ${c.columns} columns</div>`
                : `<div class="conncheck connwhy">✗ ${esc(c.error)}</div>`;
  };
  $("#connlist").innerHTML = `<div class="tablewrap"><table class="conntable"><thead><tr><th>table</th><th>system</th><th>reads</th><th>status</th><th></th></tr></thead><tbody>
    ${rows.map(r => `<tr><td><strong>${esc(r.source)}</strong>${r.description ? `<div class="muted small">${esc(r.description)}</div>` : ""}</td>
      <td>${r.system ? `${icon(r.icon)} ${esc(r.system)}` : `<span class="muted">—</span>`}</td>
      <td class="mono small">${r.relation ? "DuckDB relation" : esc(r.table || "") + (Object.keys(r.args || {}).length ? `(${esc(Object.entries(r.args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", "))})` : "")}</td>
      <td>${r.connected ? `<span class="connok">✓ Connected</span>` : `<span class="connbad">✗ Not connected</span><div class="connwhy">${esc(r.reason || "")}</div>`}${check(r)}</td>
      <td>${r.connected ? `<button class="secondary" type="button" data-conntest="${esc(r.source)}" style="padding:3px 10px;font-size:12.5px"
          ${CONN_CHECKS[r.source]?.running ? "disabled" : ""}>Test</button>` : ""}</td></tr>`).join("")}
  </tbody></table></div>`;
  $("#connlist").querySelectorAll("[data-conntest]").forEach(b => b.addEventListener("click", () => testConnection(b.dataset.conntest)));
  $("#conntestall").disabled = !rows.some(r => r.connected) || Object.values(CONN_CHECKS).some(c => c.running);
}
async function testConnection(source) {
  CONN_CHECKS[source] = {running: true}; drawConnections();
  try { CONN_CHECKS[source] = await api("/api/catalog/check", {source}); }
  catch (err) { CONN_CHECKS[source] = {ok: false, error: err.message}; }
  drawConnections();
}
$("#conntestall").addEventListener("click", async () => {
  const todo = (CAT?.status || []).filter(r => r.connected).map(r => r.source);
  $("#connnote").textContent = `testing ${todo.length} table${todo.length === 1 ? "" : "s"}…`;
  for (const s of todo) await testConnection(s);  // one at a time: rate-limited APIs (NVD) stay happy
  const failed = todo.filter(s => !CONN_CHECKS[s]?.ok).length;
  $("#connnote").textContent = failed ? `${failed} didn't answer — see below` : "every connected table answered";
});

async function startGeneration(body) {
  try { watchJob(await api("/api/catalog/generate", body)); }
  catch (err) { $("#catnote").innerHTML = `<span class="msg bad">✗ ${esc(err.message)}</span>`; }
}
function watchJob(job) {
  if (CATJOB?.timer) clearTimeout(CATJOB.timer);
  CATJOB = {id: job.id, lines: 0, timer: null};
  $("#catlog").textContent = ""; $("#catresult").innerHTML = ""; $("#catjob").hidden = false;
  showJob(job);
}
function showJob(job) {
  const log = $("#catlog"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  if (job.log?.length) { log.textContent += (log.textContent ? "\n" : "") + job.log.join("\n"); CATJOB.lines = job.log_size; }
  if (atEnd) log.scrollTop = log.scrollHeight;
  const steps = [...log.textContent.matchAll(/\[(\d+)\/(\d+)\]/g)], last = steps[steps.length - 1];
  const running = job.state === "running";
  $("#catspin").hidden = !running;
  $("#catbar").max = last ? +last[2] : 1; $("#catbar").value = running ? (last ? +last[1] - 0.5 : 0) : $("#catbar").max;
  $("#catstate").textContent = running ? (last ? `Drafting ${last[1]} of ${last[2]}…` : "Looking at the tables…")
    : job.state === "done" ? (job.result?.changed ? "Catalog updated" : "Already up to date") : "Generation failed";
  $("#catelapsed").textContent = `${Math.round(job.elapsed_s)} s`;
  $("#catupdate").disabled = $("#catall").disabled = running || !!CAT?.off;
  document.querySelectorAll("#catlist button").forEach(b => b.disabled = running || !!CAT?.off);
  if (running) { CATJOB.timer = setTimeout(pollJob, 1000); return; }
  const r = job.result;
  $("#catresult").innerHTML = job.state === "failed" ? `<p class="msg bad">✗ ${esc(job.error)}</p>` :
    `<p class="msg good">✓ ${esc(Object.keys(r.drafted).length ? `Drafted ${Object.keys(r.drafted).length} table${Object.keys(r.drafted).length === 1 ? "" : "s"}` : "Nothing needed drafting")} · ${r.sources} in the catalog${r.deferred.length ? ` · ${r.deferred.length} left for the next run (max_tables)` : ""}</p>
     ${job.reloaded ? `<p class="msg good">✓ Questions use the new catalog from now on.</p>` : ""}
     ${job.reload_error ? `<p class="msg bad">Written, but reloading failed: ${esc(job.reload_error)}</p>` : ""}
     ${(r.warnings || []).map(w => `<p class="msg warn">! ${esc(w)}</p>`).join("")}
     ${r.changed ? `<p class="muted small">Review the file — it's a draft: add <em>notes</em> where the LLM got something wrong, and they're kept next time.</p>` : ""}`;
  if (job.reloaded) api("/api/meta").then(m => { META = m; showFeatures(); }).catch(() => {});
  if (!CAT || CAT.job?.id !== job.id || CAT.job.state !== job.state) loadCatalog();  // the table ages, the buttons
}
async function pollJob() {
  if (!CATJOB) return;
  try { showJob(await api(`/api/catalog/jobs/${CATJOB.id}?since=${CATJOB.lines}`)); }
  catch (err) { $("#catstate").textContent = "Lost track of the job: " + err.message; $("#catspin").hidden = true; }
}
$("#catupdate").addEventListener("click", () => startGeneration({force: false}));
$("#catall").addEventListener("click", () => {
  const n = (CAT?.sources || []).filter(t => t.generated_at).length + (CAT?.not_in_catalog || []).length;
  if (confirm(`Draft every generated table again? That's about ${n} LLM call${n === 1 ? "" : "s"}. Hand-written tables and your notes are kept.`))
    startGeneration({force: true});
});

// ---- Config tab: a page per kind of setting ----------------------------------------------
// Every page edits the same DRAFT (duckduck.json); the editor card (Validate / Save) follows you between them.
let CFG = null, CONN = null;  // CONN: /api/connections — each connector started, or why not
let SAVED = null;  // /api/views: the saved tables registered now, and the ones that failed
const CFG_PAGES = {
  overview: {title: "Overview", editor: false},
  connectors: {title: "Connectors", intro: "The systems data is read from. Each connector becomes a set of tables named <name>_<table>, queryable in the SQL tab and usable by Ask.", editor: true},
  ai: {title: "AI providers", intro: "The models Ask can use — to draft the semantic catalog, read questions, or make its decisions. Other pages refer to them by the name you give here.", editor: true},
  saved: {title: "Saved tables", intro: "Queries kept as tables. A table over a table function (sn_table(table_name='incident')) is that function with its arguments fixed — filters still go to the source; a saved query runs each time it's read.", editor: true},
  catalog: {title: "Semantic catalog", editor: false},
  connections: {title: "Catalog connections", editor: false},
  reading: {title: "How questions are read", intro: "Who reads a question, who decides what it means, and when Ask considers several readings before asking you back.",
            editor: true, semantic: ["reader", "default_llm", "decision_engine", "extractor", "router", "hypotheses"]},
  answers: {title: "Answers & data", intro: "How much data an answer reads and shows, and which tables Ask may use.",
            editor: true, semantic: ["sample", "default_limit", "stream", "allowed_sources", "strict", "live_evidence"]},
  drafting: {title: "Catalog drafting", intro: "Where the semantic catalog lives, and how the LLM drafts it from your tables.",
             editor: true, semantic: ["catalog_path", "catalog_generation"]},
  thresholds: {title: "Confidence thresholds", intro: "How sure Ask must be of each decision before acting on it. Below its threshold it asks you instead of guessing. Higher = asks more often, guesses less.",
               editor: true, semantic: ["thresholds"]},
  learning: {title: "Feedback & learning", intro: "Where ratings are kept, how Ask learns from similar questions, and the wording it uses.",
             editor: true, semantic: ["feedback", "answer_shapes", "clarification_texts"]},
  json: {title: "duckduck.json", intro: "The whole file as JSON. The other pages edit this same text — change it here or there.", editor: true},
  reference: {title: "Every option", editor: false},
};
let CFGPAGE = CFG_PAGES[store.get("duckduck-cfgpage")] ? store.get("duckduck-cfgpage") : "overview";

// friendly names: "<context>.<key>" first, then the key alone, then the key made readable
const LABELS = {
  on_error: "When a connector fails to start", connector: "Connector type", table_prefix: "Table name prefix",
  "authentication.type": "Where the credentials come from", secret_id: "Secret name", region_name: "AWS region",
  profile_name: "AWS profile", vault_url: "Key Vault URL", tenant_id: "Tenant ID",
  client_id: "Client (app) ID", client_secret: "Client secret", default_page_size: "Rows per page", page_size: "Rows per page",
  hostname: "SharePoint hostname", site_path: "Default site path", host: "Host", username: "Username", password: "Password",
  verify: "Verify the TLS certificate", connection_string: "Connection string", instance: "Instance", api_key: "API key",
  api_secret: "API secret", request_interval: "Pause between requests (s)", max_retries: "Retries on errors",
  base_url: "Base URL", include_rejected: "Include rejected CVEs", timeout: "Timeout (s)",
  aws_access_key_id: "AWS access key ID", aws_secret_access_key: "AWS secret access key", cluster: "Cluster",
  database: "Database", notruncation: "Allow results over ADX's size limits", path: "Folder",
  include_hidden: "Include hidden files", module: "Python file or module", factory: "Factory function",
  kwargs: "Arguments for the factory", account_name: "Storage account",
  views: "Saved tables", "views.table": "Table function", "views.args": "Arguments", "views.sql": "Query", description: "Description",
  provider: "Provider", api: "API style", decisions_url: "Decisions API URL", model: "Model", max_tokens: "Max output tokens",
  effort: "Reasoning effort", fallbacks: "Refusal fallback", endpoint: "Endpoint", resource: "Resource name",
  deployment: "Deployment", api_version: "API version", require_parameters: "Only providers that honour structured output",
  headers: "Extra HTTP headers",
  catalog_path: "Catalog file", decision_engine: "Decision engine", default_llm: "Default LLM", extractor: "Value extraction",
  reader: "Default reading mode", router: "Auto mode", hypotheses: "Weigh several readings before asking back",
  stream: "Read page by page", sample: "Rows shown per answer", catalog_generation: "Catalog drafting",
  thresholds: "Confidence thresholds", clarification_texts: "Follow-up question texts", answer_shapes: "Answer wording",
  feedback: "Feedback", live_evidence: "Check values live in the data", allowed_sources: "Only these tables",
  default_limit: "Row cap per answer", strict: "Fail when a catalog table isn't connected",
  "decision_engine.type": "Engine", "decision_engine.ai_provider": "AI provider (empty = offline rules)", retries: "Retries",
  system_prompt: "System prompt", system_prompt_file: "System prompt file",
  "extractor.type": "Extractor", "extractor.llm": "LLM", "extractor.on_error": "If the LLM fails", translate: "Translate questions to English",
  min_similarity: "Min similarity to past questions", max_runs: "Past runs considered", fallback: "Fallback mode",
  tie_margin: "Tie margin", enabled: "On", max_hypotheses: "Max readings", depth: "Depth", workers: "Parallel workers",
  "hypotheses.threshold": "Winner's min probability", margin: "Winner's lead", check: "Plan check min probability",
  output_path: "Write to (default: the catalog file)", max_age: "Redraft tables older than", auto_refresh: "Refresh when the server starts",
  sample_rows: "Sample rows sent to the LLM", source_prompt: "Table prompt", source_prompt_file: "Table prompt file",
  link_prompt: "Vocabulary prompt", link_prompt_file: "Vocabulary prompt file", tables: "Tables to draft (explicit list)",
  discover: "Discover the tables behind catalogs", include: "Only tables matching", exclude: "Skip tables matching",
  max_tables: "Max tables per run", profile_rows: "Rows profiled", sample_values: "Example values kept",
  sample_sensitive: "Keep examples of sensitive fields", max_enum_values: "Max values per category",
  api_docs: "API documentation", api_docs_max_chars: "API docs budget (characters)", "catalog_generation.llm": "LLM",
  link_llm: "LLM for the vocabulary pass",
  "thresholds.source": "A table is relevant", "thresholds.field": "The right field", "thresholds.relationship": "The right join",
  "thresholds.critical": "A critical field", "thresholds.entity": "What it's about", "thresholds.activity": "The activity",
  "thresholds.resource_type": "A value's type", "thresholds.answer_shape": "The kind of answer",
  "thresholds.out_of_scope": "Not about the data", "thresholds.subject": "Data or this assistant",
  "thresholds.subject_doubt": "Ask when possibly about this assistant", "thresholds.reply": "Understanding your reply",
  "thresholds.router": "Auto mode's pick",
  "feedback.path": "Store file", memory: "Learn from similar questions", max_cases: "Similar questions used",
  learned_answer_shapes: "Learned wording file", min_support: "Failures before a suggestion",
  max_probes: "Max checks per question", "live_evidence.timeout": "Timeout per check (s)",
};
const ACRONYMS = {id: "ID", url: "URL", api: "API", llm: "LLM", ai: "AI", aws: "AWS", sql: "SQL", kql: "KQL", tls: "TLS", ssl: "SSL", http: "HTTP"};
function humanize(key) {
  const words = String(key).replace(/^_+/, "").split(/[_\s]+/).filter(Boolean).map(w => ACRONYMS[w.toLowerCase()] || w.toLowerCase());
  if (!words.length) return String(key);
  words[0] = words[0][0].toUpperCase() + words[0].slice(1);
  return words.join(" ");
}
const labelOf = (key, ctx) => LABELS[ctx ? `${ctx}.${key}` : key] || LABELS[key] || humanize(key);
// friendly names for the values of a choice
const CHOICE_LABELS = {
  on_error: {raise: "Stop everything (raise)", warn: "Skip it and warn (warn)"},
  "authentication.type": {local: "Written here (local)", aws: "AWS Secrets Manager (aws)", azure: "Azure Key Vault (azure)"},
  reader: {rules: "🦆 Paddle — rules (rules)", llm: "🤿 Dive — an LLM reads (llm)", llm_decides: "🪽 Fly — an LLM reads and decides (llm_decides)", auto: "🧭 Auto — picks per question (auto)"},
  "router.fallback": {rules: "🦆 Paddle (rules)", llm: "🤿 Dive (llm)", llm_decides: "🪽 Fly (llm_decides)"},
  "extractor.type": {rules: "Rules — offline (rules)", llm: "An LLM (llm)"},
  "extractor.on_error": {fallback: "Fall back to rules (fallback)", raise: "Fail (raise)"},
  "decision_engine.type": {lexical: "Offline rules (lexical)"},
  provider: {anthropic: "Anthropic — Claude API (anthropic)", foundry: "Claude on Microsoft Foundry (foundry)",
             azure_openai: "Azure OpenAI (azure_openai)", openrouter: "OpenRouter (openrouter)", jev: "Jev — Decisions API (jev)"},
  api: {chat: "Chat model (chat)", decisions: "Decisions API (decisions)"},
};
const choiceLabel = (key, ctx, value) => ((CHOICE_LABELS[ctx ? `${ctx}.${key}` : key] || CHOICE_LABELS[key] || {})[value]) ?? String(value);
// connector kinds, as a person would call them
const CONNECTOR_INFO = {
  sharepoint: ["SharePoint", "Sites, lists, files and drives through Microsoft Graph"],
  insightvm: ["Rapid7 InsightVM", "Assets, vulnerabilities and sites"],
  database: ["SQL database", "SQL Server, PostgreSQL, MySQL, Oracle, SQLite… through SQLAlchemy"],
  servicenow: ["ServiceNow", "Incidents, problems, changes, users, CMDB — any table"],
  axonius: ["Axonius", "Devices and users"],
  nvd: ["NVD (CVE database)", "Public CVEs from NIST — an API key is optional"],
  restcountries: ["REST Countries", "Countries of the world (needs an API key)"],
  glue: ["AWS Glue / S3", "Parquet, Delta and Iceberg tables from the Glue Data Catalog"],
  adx: ["Azure Data Explorer", "Kusto tables and KQL queries"],
  files: ["Local files", "Every CSV, Parquet or JSON file in a folder"],
  python: ["Python module", "Tables made by your own Python functions"],
  blob_storage: ["Azure Blob Storage", "Parquet, CSV, JSON, Delta or Iceberg in a container"],
};
const connectorName = (c) => (CONNECTOR_INFO[c] || [humanize(c || "?")])[0];
async function loadConfig() {
  try {
    CFG = await api("/api/config");
    $("#cfgpath").textContent = CFG.path || "";
    DRAFT = JSON.parse(JSON.stringify(CFG.config || {}));
    $("#cfgnote").textContent = (CFG.editable ? "Secrets show as \"***\" — leave them to keep the saved value, or type a new one. Saving keeps a .bak and reconnects everything."
      : "Read-only here: start the server with --edit-config to save. Secrets show as \"***\".");
    $("#cfgsave").disabled = !CFG.editable;
    $("#cfgmsgs").innerHTML = "";
    CFGVIEW = "form";
    showCfgPage(CFGPAGE);
  } catch (err) { $("#cfgmsgs").innerHTML = `<p class="msg bad">${esc(err.message)}</p>`; }
}
// which connectors started, and why the others didn't (needs the SQL console; without it, no status)
async function loadCfgStatus() {
  [CONN, SAVED] = await Promise.all([api("/api/connections").catch(() => null), api("/api/views").catch(() => null)]);
  navCounts();
}
function parsedConfig() {
  if (CFGVIEW === "form") return DRAFT;
  try { return JSON.parse($("#cfgtext").value); }
  catch (err) { $("#cfgmsgs").innerHTML = `<p class="msg bad">Not valid JSON: ${esc(err.message)}</p>`; return undefined; }
}
function showReport(rep, okText) {
  $("#cfgmsgs").innerHTML = (rep.errors || []).map(e => `<p class="msg bad">✗ ${esc(e)}</p>`).join("") +
    (rep.warnings || []).map(w => `<p class="msg warn">! ${esc(w)}</p>`).join("") +
    (!(rep.errors || []).length ? `<p class="msg good">✓ ${esc(okText)}</p>` : "");
}
$("#cfgvalidate").addEventListener("click", async () => {
  const cfg = parsedConfig(); if (cfg === undefined) return;
  try { showReport(await api("/api/config/validate", {config: cfg}), "Valid — nothing saved yet."); }
  catch (err) { $("#cfgmsgs").innerHTML = `<p class="msg bad">${esc(err.message)}</p>`; }
});
$("#cfgsave").addEventListener("click", async () => {
  const cfg = parsedConfig(); if (cfg === undefined) return;
  const headers = {"Content-Type": "application/json"}; const t = store.get("duckduck-token"); if (t) headers["X-Duckduck-Token"] = t;
  const r = await fetch("/api/config", {method: "PUT", headers, body: JSON.stringify({config: cfg})});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { $("#cfgmsgs").innerHTML = `<p class="msg bad">✗ ${esc(d.detail || r.statusText)}</p>`; return; }
  META = await api("/api/meta").catch(() => META); showFeatures();
  await loadCfgStatus(); await loadConfig(); loadCatalog();  // reconnected: fresh status, the saved file, no unsaved changes
  showReport({warnings: [...(d.warnings || []), ...(d.reload_error ? ["saved, but reconnecting failed: " + d.reload_error] : [])]},
    d.reloaded ? `Saved (${d.saved}) and reconnected.` : `Saved (${d.saved}).`);
});
$("#cfgreset").addEventListener("click", loadConfig);
$("#cfgtext").addEventListener("keydown", (e) => {
  if (e.key === "Tab") { e.preventDefault(); const t = e.target, s = t.selectionStart;
    t.value = t.value.slice(0, s) + "  " + t.value.slice(t.selectionEnd); t.selectionStart = t.selectionEnd = s + 2; }
});
$("#cfgtext").addEventListener("input", markDirty);

// ---- pages ----
function showCfgPage(page) {
  if (!CFG_PAGES[page]) page = "overview";
  if (CFGVIEW === "json" && page !== "json") {  // leaving the JSON page: what's typed there becomes the draft
    try { const d = JSON.parse($("#cfgtext").value || "{}"); if (!isObj(d)) throw new Error("the config must be a JSON object"); DRAFT = d; }
    catch (err) { $("#cfgmsgs").innerHTML = `<p class="msg bad">Fix the JSON first: ${esc(err.message)}</p>`; return; }
    CFGVIEW = "form";
  }
  CFGPAGE = page; store.set("duckduck-cfgpage", page);
  if (!$("#tab-config").hidden) history.replaceState(null, "", "#config/" + page);
  document.querySelectorAll("#cfgnav [data-cfgpage]").forEach(b => {
    if (b.dataset.cfgpage === page) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
  document.querySelectorAll("#tab-config .cfgpage").forEach(d => { d.hidden = d.dataset.page !== page; });
  const info = CFG_PAGES[page];
  $("#cfgeditor").hidden = !info.editor;
  if (page === "reference" && CFG) drawReference();
  if (page === "overview") drawOverview();
  if (!info.editor || !CFG) return;
  $("#cfgtitle").textContent = info.title;
  $("#cfgintro").textContent = info.intro || "";
  CFGVIEW = page === "json" ? "json" : "form";
  $("#cfgform").hidden = page === "json"; $("#cfgtext").hidden = page !== "json";
  if (page === "json") syncJson(); else renderForm();
}
document.querySelectorAll("#cfgnav [data-cfgpage]").forEach(b => b.addEventListener("click", () => {
  showCfgPage(b.dataset.cfgpage); window.scrollTo({top: 0});
}));
// any link to a page: <a data-cfggo="ai">
document.addEventListener("click", (e) => {
  const go = e.target.closest?.("[data-cfggo]"); if (!go) return;
  e.preventDefault();
  if ($("#tab-config").hidden) { CFGPAGE = go.dataset.cfggo; openTab("config"); } else showCfgPage(go.dataset.cfggo);
  if (go.dataset.open) { OPEN.add(go.dataset.open); if (CFG) renderForm();
    setTimeout(() => document.getElementById("card-" + go.dataset.open)?.scrollIntoView({behavior: "smooth", block: "start"}), 150); }
});
function navCounts() {
  const svcs = Object.keys(DRAFT.services || {}), bad = (CONN?.services || []).filter(s => !s.started).length;
  const c = $("#navc-connectors");
  c.textContent = svcs.length ? (bad ? `${svcs.length} · ${bad} ✗` : String(svcs.length)) : "";
  c.classList.toggle("bad", !!bad);
  c.title = bad ? `${bad} didn't start` : "";
  const ai = Object.keys(DRAFT.ai_providers || {}).length;
  $("#navc-ai").textContent = ai ? String(ai) : "";
  const saved = Object.keys(DRAFT.views || {}).length, sbad = Object.keys(SAVED?.failed || {}).length;
  const sc = $("#navc-saved");
  sc.textContent = saved ? (sbad ? `${saved} · ${sbad} ✗` : String(saved)) : "";
  sc.classList.toggle("bad", !!sbad);
}
function markDirty() {
  let now = DRAFT;
  if (CFGVIEW === "json") { try { now = JSON.parse($("#cfgtext").value); } catch { now = null; } }
  $("#cfgdirty").hidden = !CFG || JSON.stringify(now) === JSON.stringify(CFG.config || {});
}

// ---- Config form: edits DRAFT; the JSON page shows it -----------------------------------------
// Every field comes from the options reference (/api/config → reference): its "input" says how to edit it.
// Only what's set is written; clearing a field removes the key (the default applies).
let DRAFT = {}, CFGVIEW = "form", FID = 0;
const OPEN = new Set();  // the cards left open: "svc:<name>" / "ai:<name>"
const LLM_REFS = new Set(["default_llm", "ai_provider", "llm", "link_llm"]);
const AUTH_KEYS = {local: [], aws: ["secret_id", "region_name", "profile_name"], azure: ["secret_id", "vault_url", "tenant_id"]};
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  kids.flat(Infinity).forEach(c => { if (c !== null && c !== undefined && c !== false) el.append(c.nodeType ? c : document.createTextNode(c)); });
  return el;
}
const plain = (text) => String(text || "").replace(/``/g, "");  // reST literals in docstrings
function iconEl(kind) { const s = h("span"); s.innerHTML = icon(kind); return s.firstElementChild || s; }
function syncJson() { $("#cfgtext").value = JSON.stringify(DRAFT, null, 2); markDirty(); navCounts(); }
const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
const ROOT = { obj: () => DRAFT, get: (k) => DRAFT[k],
  set(k, v) { if (v === undefined) delete DRAFT[k]; else DRAFT[k] = v; syncJson(); } };
// a nested object of its parent: created when a key is first set, removed when empty (unless keep)
function scope(parent, key, keep = false) {
  return {
    obj() { const o = parent.get(key); return isObj(o) ? o : {}; },
    get(k) { return this.obj()[k]; },
    set(k, v) { const o = {...this.obj()}; if (v === undefined) delete o[k]; else o[k] = v; this.replace(o); },
    replace(o) { parent.set(key, Object.keys(o).length || keep ? o : undefined); },
    rename(from, to) {
      if (!to || to === from || to in this.obj()) return false;
      this.replace(Object.fromEntries(Object.entries(this.obj()).map(([k, v]) => [k === from ? to : k, v]))); return true;
    },
  };
}
function isSecretKey(key) {
  const s = CFG?.reference?.secrets; if (!s) return false;
  return new RegExp(s.pattern, "i").test(key) && !s.not_suffixes.some(x => key.toLowerCase().endsWith(x));
}
const shown = (v) => v === undefined || v === null ? "" : typeof v === "string" ? v : JSON.stringify(v);
function scalarOf(text) {  // a number / true / false / null if it reads as one, else the text
  if (/^(-?\d+(\.\d+)?([eE][-+]?\d+)?|true|false|null)$/.test(text.trim())) return JSON.parse(text.trim());
  return text;
}
// a label people read, with the key as written in duckduck.json next to it
function labelEl(id, opt, ctx) {
  const name = labelOf(opt.name, ctx);
  return h("label", {for: id, title: opt.name},
    name, opt.required ? h("span", {class: "req", title: "required"}, " *") : null,
    name.toLowerCase().replace(/[^a-z0-9]/g, "") !== opt.name.toLowerCase().replace(/[^a-z0-9]/g, "")
      ? h("span", {class: "key"}, opt.name) : null);
}
function field(opt, sc, {after, wide, ctx} = {}) {
  const id = "cf" + (++FID), cur = sc.get(opt.name), def = opt.default, kind = opt.input || "text";
  const set = (v) => { sc.set(opt.name, v); after?.(v); };
  const defText = def === null || def === undefined || def === "" ? "" : typeof def === "object" ? JSON.stringify(def) : String(def);
  let input, hint = plain(opt.description);
  if (LLM_REFS.has(opt.name) && (kind === "text" || kind === "scalar")) {  // pick one of the AI providers by name
    const names = Object.keys(DRAFT.ai_providers || {});
    if (cur !== undefined && !names.includes(cur)) names.push(cur);
    input = h("select", {id, onchange: (e) => set(e.target.value === "" ? undefined : e.target.value)},
      h("option", {value: ""}, opt.name === "ai_provider" ? "— offline rules —" : "— none —"),
      names.map(n => h("option", {value: n, selected: n === cur}, n)));
    if (!Object.keys(DRAFT.ai_providers || {}).length) hint = (hint ? hint + " " : "") + "Add one on the AI providers page first.";
  } else if (kind === "choice" || kind === "bool") {
    const choices = kind === "bool" ? [true, false] : [...opt.choices];
    if (cur !== undefined && !choices.some(c => JSON.stringify(c) === JSON.stringify(cur))) choices.push(cur);
    const text = (c) => kind === "bool" ? (c ? "Yes" : "No") : choiceLabel(opt.name, ctx, c);
    input = h("select", {id, onchange: (e) => set(e.target.value === "" ? undefined : JSON.parse(e.target.value))},
      h("option", {value: ""}, defText ? `Default — ${kind === "bool" ? (def ? "Yes" : "No") : choiceLabel(opt.name, ctx, def)}` : "—"),
      choices.map(c => h("option", {value: JSON.stringify(c), selected: JSON.stringify(c) === JSON.stringify(cur)}, text(c))));
  } else if (kind === "int" || kind === "float") {
    input = h("input", {id, type: "number", step: kind === "int" ? "1" : "any", value: shown(cur), placeholder: defText,
      oninput: (e) => set(e.target.value === "" ? undefined : Number(e.target.value))});
  } else if (kind === "list") {
    input = h("input", {id, value: Array.isArray(cur) ? cur.join(", ") : shown(cur), placeholder: defText || "a, b, c",
      oninput: (e) => { const v = e.target.value.split(",").map(x => x.trim()).filter(Boolean); set(v.length ? v : undefined); }});
    hint = (hint ? hint + " " : "") + "Comma-separated.";
  } else if (kind === "json") {
    input = h("textarea", {id, class: "mono fjson", spellcheck: "false", placeholder: defText ? "default: " + defText : "JSON",
      value: cur === undefined ? "" : JSON.stringify(cur, null, 2),
      oninput: (e) => { const t = e.target.value.trim();
        if (!t) { e.target.classList.remove("invalid"); return set(undefined); }
        try { const v = JSON.parse(t); e.target.classList.remove("invalid"); set(v); } catch { e.target.classList.add("invalid"); } }});
    wide = true;
  } else {
    const secret = opt.secret || isSecretKey(opt.name);
    input = h("input", {id, type: secret ? "password" : "text", value: shown(cur), placeholder: defText, autocomplete: "off",
      oninput: (e) => { const t = e.target.value; set(t === "" ? undefined : kind === "scalar" ? scalarOf(t) : t); }});
    if (secret && cur === CFG.reference.secrets.mask) hint = "Saved — leave it to keep, or type a new value. " + hint;
  }
  return h("div", {class: "fld" + (wide ? " wide" : "")}, labelEl(id, opt, ctx), input, hint ? h("div", {class: "hint"}, hint) : null);
}
function fields(opts, sc, ctx) {  // plain fields in a grid, nested sections below it
  const grid = h("div", {class: "fgrid"}), sections = [];
  opts.forEach(o => {
    if (o.options) {
      sections.push(h("details", {class: "sect", open: Object.keys(scope(sc, o.name).obj()).length ? true : null},
        h("summary", {}, labelOf(o.name, ctx), " ", h("span", {class: "key muted small mono"}, o.name)),
        o.description ? h("div", {class: "hint muted small"}, plain(o.description)) : null,
        fields(o.options, scope(sc, o.name), o.name)));
    } else grid.append(field(o, sc, {ctx}));
  });
  return h("div", {}, grid.children.length ? grid : null, sections);
}
// a titled box of settings on a page (one nested object of duckduck.json)
function group(title, key, description, ...body) {
  return h("div", {class: "group"}, h("div", {class: "gt"}, title, key ? h("span", {class: "key muted small mono"}, key) : null),
    description ? h("div", {class: "gd"}, plain(description)) : null, body);
}
// free keys (an authentication block, a service's keys no option describes): key → value rows
function keyRows(sc, skip, suggest = [], note = "") {
  const box = h("div");
  const draw = () => {
    box.replaceChildren();
    Object.keys(sc.obj()).filter(k => !skip.includes(k)).forEach(k => {
      const v = sc.get(k), secret = isSecretKey(k);
      const keyIn = h("input", {value: k, "aria-label": "key", class: "mono",
        onchange: (e) => { if (!sc.rename(k, e.target.value.trim())) e.target.value = k; draw(); }});
      const valIn = h("input", {value: shown(v), type: secret ? "password" : "text", "aria-label": k, autocomplete: "off",
        placeholder: secret ? "secret" : "value or \"$secret.<key>\"",
        oninput: (e) => sc.set(k, typeof v === "string" || v === undefined ? e.target.value : scalarOf(e.target.value))});
      box.append(h("div", {class: "kv"}, keyIn, valIn, h("button", {type: "button", class: "x", title: "remove " + k,
        onclick: () => { sc.set(k, undefined); draw(); }}, "remove")));
    });
    const missing = suggest.filter(k => !(k in sc.obj()) && !skip.includes(k));
    const addKey = (k) => { if (!k || k in sc.obj()) return; sc.set(k, ""); draw();
      box.querySelector(`.kv:last-of-type input[aria-label="${CSS.escape(k)}"]`)?.focus(); };
    box.append(h("div", {}, missing.map(k => h("button", {type: "button", class: "add", title: k, onclick: () => addKey(k)}, "+ " + labelOf(k))),
      h("button", {type: "button", class: "add", onclick: () => { const k = prompt("Key name:"); if (k) addKey(k.trim()); }}, "+ other key")));
    if (note) box.append(h("div", {class: "hint muted small"}, note));
  };
  draw();
  return box;
}
// sign-in: where the credentials come from, and — written here — the credentials themselves as fields
function authBlock(sc, credentials, optional) {
  const box = h("div", {class: "sec"});
  const authOpt = (k) => CFG.reference.authentication.find(o => o.name === k) || {name: k, input: "text"};
  const draw = () => {
    const type = sc.get("type") || "local", known = AUTH_KEYS[type] || [];
    const local = type === "local";
    box.replaceChildren(
      h("div", {class: "st"}, "Sign-in"),
      h("div", {class: "sd"}, optional ? "Optional for this one. " : "",
        local ? "Written in this file, as is. AWS Secrets Manager or Azure Key Vault keep secrets out of it."
          : credentials.length ? `Read from the secret, which should hold: ${credentials.map(k => labelOf(k)).join(", ")}.` : "Read from the secret."),
      h("div", {class: "fgrid"}, field(authOpt("type"), sc, {after: draw, ctx: "authentication"}),
        known.map(k => field(authOpt(k), sc, {ctx: "authentication"})),
        local ? credentials.map(k => field({name: k, input: "text", secret: isSecretKey(k), description: ""}, sc)) : null),
      h("details", {class: "adv", open: Object.keys(sc.obj()).some(k => !["type", ...known, ...(local ? credentials : [])].includes(k)) || null},
        h("summary", {}, local ? "Other keys" : "Override keys of the secret"),
        keyRows(sc, ["type", ...known, ...(local ? credentials : [])], local ? [] : credentials,
          local ? "Anything else the connector reads from its credentials."
            : "Optional: a value replaces the secret's key; \"$secret.<key>\" copies another key of the secret.")));
  };
  draw();
  return box;
}
const connectorRef = (c) => CFG.reference.connectors.find(x => x.connector === c);
function connectorSelect(current, onchange, id) {
  const names = CFG.reference.connectors.map(c => c.connector);
  if (current && !names.includes(current)) names.push(current);
  return h("select", {id, "aria-label": "connector type", onchange: (e) => onchange(e.target.value)},
    names.map(n => h("option", {value: n, selected: n === current}, `${connectorName(n)} (${n})`)));
}
function statusPill(name, saved) {
  if (!saved) return h("span", {class: "status new", title: "Save to connect it"}, "not saved yet");
  const st = (CONN?.services || []).find(x => x.name === name);
  if (!st) return null;
  return st.started ? h("span", {class: "status ok"}, `● connected · ${st.tables} table${st.tables === 1 ? "" : "s"}`)
    : h("span", {class: "status bad", title: st.error || ""}, "● didn't start");
}
function openToggle(key) { return (e) => { if (e.target.open) OPEN.add(key); else OPEN.delete(key); }; }
// ---- Connectors page: one collapsible card per service ----
function serviceCard(services, name) {
  const sc = scope(services, name, true), conn = sc.get("connector") || name, ref = connectorRef(conn);
  const opts = ref ? ref.options.filter(o => !o.credential) : [];
  const creds = ref ? ref.options.filter(o => o.credential).map(o => o.name) : [];
  const main = opts.filter(o => o.required || o.default === null || o.default === undefined);
  const adv = opts.filter(o => !main.includes(o));
  const common = CFG.reference.service_common.filter(o => o.name === "table_prefix");
  const known = ["connector", "authentication", ...common.map(o => o.name), ...opts.map(o => o.name)];
  const saved = name in ((CFG.config || {}).services || {});
  const st = (CONN?.services || []).find(x => x.name === name);
  const key = "svc:" + name, prefix = sc.get("table_prefix") ?? name;
  const tables = !ref ? [] : ref.dynamic_tables ? null : ref.tables.map(t => (prefix ? prefix + "_" : "") + t);
  const nameId = "cf" + (++FID), connId = "cf" + (++FID);
  const card = h("details", {class: "ccard", id: "card-" + key, open: OPEN.has(key) || null, ontoggle: openToggle(key)},
    h("summary", {}, iconEl(ref?.icon || "api"),
      h("span", {class: "who"}, h("b", {}, name), h("span", {}, connectorName(conn) +
        (tables ? ` · ${tables.length} table${tables.length === 1 ? "" : "s"}` : ref ? " · a table per file / function" : " · unknown connector"))),
      h("span", {class: "grow"}), statusPill(name, saved), h("span", {class: "chev", "aria-hidden": "true"}, "▶")),
    h("div", {class: "body"},
      st && !st.started ? h("div", {class: "errbox"}, h("b", {}, "Didn't start: "), h("span", {class: "err"}, st.error || "unknown error"),
        h("div", {class: "muted small"}, "Fix it below and Save — it reconnects.")) : null,
      h("div", {class: "sec"}, h("div", {class: "st"}, "Name & type"),
        h("div", {class: "fgrid"},
          h("div", {class: "fld"}, h("label", {for: nameId}, "Name"),
            h("input", {id: nameId, class: "mono", value: name,
              onchange: (e) => { const to = e.target.value.trim();
                if (services.rename(name, to)) { if (OPEN.delete(key)) OPEN.add("svc:" + to); renderForm(); } else e.target.value = name; }}),
            h("div", {class: "hint"}, "Its tables are named after it.")),
          h("div", {class: "fld"}, h("label", {for: connId}, "Connector type", h("span", {class: "key"}, "connector")),
            connectorSelect(conn, (c) => { sc.set("connector", c); OPEN.add(key); card.replaceWith(serviceCard(services, name)); }, connId),
            h("div", {class: "hint"}, (CONNECTOR_INFO[conn] || [])[1] || "")),
          common.map(o => field(o, sc))),
        h("div", {class: "hint muted small", style: "margin-top:6px"},
          tables ? `Tables: ${tables.join(", ")}` : ref ? "Tables: one per file / module function" : "")),
      main.length ? h("div", {class: "sec"}, h("div", {class: "st"}, "Connection"),
        h("div", {class: "fgrid"}, main.map(o => field(o, sc, {ctx: conn})))) : null,
      authBlock(scope(sc, "authentication"), creds, ref && !ref.requires_authentication),
      adv.length ? h("details", {class: "adv", open: adv.some(o => sc.get(o.name) !== undefined) || null},
        h("summary", {}, "Advanced"), h("div", {class: "fgrid", style: "margin-top:8px"}, adv.map(o => field(o, sc, {ctx: conn})))) : null,
      Object.keys(sc.obj()).some(k => !known.includes(k)) ? h("div", {class: "sec"}, h("div", {class: "st"}, "Other keys"),
        keyRows(sc, known, [], "Not options of this connector — Validate says if they're ignored.")) : null,
      h("div", {class: "foot"}, h("button", {type: "button", class: "danger",
        onclick: () => { if (confirm(`Remove the connector “${name}”? (Nothing changes until you Save.)`)) { services.set(name, undefined); OPEN.delete(key); renderForm(); } }},
        "Remove connector"))));
  return card;
}
function addConnectorBox(services) {
  let chosen = null;
  const nameIn = h("input", {class: "mono", placeholder: "name, e.g. crm", "aria-label": "new connector name"});
  const addBtn = h("button", {type: "button", class: "primary", disabled: true}, "Add");
  const unique = (base) => { let n = base, i = 2; while (n in services.obj()) n = `${base}_${i++}`; return n; };
  const gallery = h("div", {class: "gallery"}, CFG.reference.connectors.map(c => h("button", {type: "button", "aria-pressed": "false",
    onclick: (e) => { chosen = c.connector; gallery.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b === e.currentTarget)));
      if (!nameIn.value || nameIn.dataset.auto) { nameIn.value = unique(c.connector); nameIn.dataset.auto = "1"; }
      addBtn.disabled = false; nameIn.focus(); nameIn.select(); }},
    iconEl(c.icon || "api"), h("span", {}, h("b", {}, connectorName(c.connector)), h("span", {class: "d"}, (CONNECTOR_INFO[c.connector] || [])[1] || c.connector)))));
  const go = () => {
    const n = nameIn.value.trim(); if (!chosen) return; if (!n) { nameIn.focus(); return; }
    if (n in services.obj()) { toast(`“${n}” already exists`); return; }
    const auth = (connectorRef(chosen) || {}).requires_authentication === false ? {} : {authentication: {type: "local"}};
    services.set(n, {connector: chosen, ...auth}); OPEN.add("svc:" + n); renderForm();
    setTimeout(() => document.getElementById("card-svc:" + n)?.scrollIntoView({behavior: "smooth", block: "start"}), 50);
  };
  nameIn.addEventListener("input", () => delete nameIn.dataset.auto);
  nameIn.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  addBtn.addEventListener("click", go);
  const body = h("div", {hidden: true}, h("p", {class: "muted small", style: "margin:8px 0 0"}, "Pick what it connects to:"), gallery,
    h("div", {class: "row", style: "margin-top:10px"}, nameIn, addBtn));
  return h("div", {class: "addbox"}, h("button", {type: "button", class: "secondary",
    onclick: (e) => { body.hidden = !body.hidden; e.currentTarget.textContent = body.hidden ? "＋ Add a connector" : "Close"; }}, "＋ Add a connector"), body);
}
function formConnectors(form) {
  const services = scope(ROOT, "services", true), names = Object.keys(services.obj());
  const bad = (CONN?.services || []).filter(s => !s.started).length;
  form.append(h("div", {class: "listtools"},
    h("span", {class: "muted small"}, names.length ? `${names.length} connector${names.length === 1 ? "" : "s"}` +
      (CONN ? ` · ${(CONN.services || []).filter(s => s.started).length} connected` + (bad ? ` · ${bad} didn't start` : "") : "") : "No connectors yet."),
    h("span", {class: "grow"}),
    names.length > 1 ? h("button", {type: "button", class: "x", onclick: () => { names.forEach(n => OPEN.add("svc:" + n)); renderForm(); }}, "Expand all") : null,
    names.length > 1 ? h("button", {type: "button", class: "x", onclick: () => { names.forEach(n => OPEN.delete("svc:" + n)); renderForm(); }}, "Collapse all") : null));
  names.forEach(n => form.append(serviceCard(services, n)));
  form.append(addConnectorBox(services));
  const onErr = CFG.reference.top_level.find(o => o.name === "on_error");
  if (onErr) form.append(group("Startup", null, "", h("div", {class: "fgrid"}, field(onErr, ROOT))));
}

// ---- AI providers page ----
const PROVIDER_ONLY = {resource: ["foundry"], deployment: ["azure_openai"], api_version: ["azure_openai"], endpoint: ["foundry", "azure_openai"],
  tenant_id: ["foundry", "azure_openai"], fallbacks: ["anthropic", "foundry"], base_url: ["anthropic", "openrouter", "jev"],
  require_parameters: ["openrouter"], max_tokens: ["anthropic", "foundry", "azure_openai", "openrouter"],
  effort: ["anthropic", "foundry", "azure_openai", "openrouter"], headers: ["anthropic", "foundry", "azure_openai", "openrouter"],
  decisions_url: null, timeout: null};
function usedBy(name) {
  const sem = DRAFT.semantic || {}, out = [];
  if (sem.default_llm === name) out.push("default LLM");
  if (sem.decision_engine?.ai_provider === name) out.push("decision engine");
  if (sem.extractor?.llm === name) out.push("value extraction");
  if (sem.catalog_generation?.llm === name) out.push("catalog drafting");
  if (sem.catalog_generation?.link_llm === name) out.push("vocabulary pass");
  return out;
}
function providerCard(providers, name) {
  const sc = scope(providers, name, true), key = "ai:" + name;
  const provider = sc.get("provider") || "anthropic", api = sc.get("api") || (provider === "jev" ? "decisions" : "chat");
  const all = CFG.reference.ai_provider.filter(o => o.name !== "authentication");
  const fits = (o) => { const only = PROVIDER_ONLY[o.name];
    if (o.name === "decisions_url" || o.name === "timeout") return api === "decisions";
    if (["max_tokens", "effort", "headers", "require_parameters"].includes(o.name) && api === "decisions") return false;
    return !only || only.includes(provider); };
  const shownOpt = (o) => fits(o) || sc.get(o.name) !== undefined;  // a stray setting stays visible, so it can be cleared
  const mainNames = ["provider", "api", "model", "deployment", "decisions_url"];
  const endNames = ["base_url", "endpoint", "resource", "api_version", "tenant_id"];
  const pick = (names) => names.map(n => all.find(o => o.name === n)).filter(o => o && shownOpt(o));
  const advOpts = all.filter(o => !mainNames.includes(o.name) && !endNames.includes(o.name) && shownOpt(o));
  const used = usedBy(name), nameId = "cf" + (++FID);
  const redraw = () => { OPEN.add(key); card.replaceWith(providerCard(providers, name)); };
  const model = sc.get("model") || sc.get("deployment") || "";
  const card = h("details", {class: "ccard", id: "card-" + key, open: OPEN.has(key) || null, ontoggle: openToggle(key)},
    h("summary", {}, iconEl("api"),
      h("span", {class: "who"}, h("b", {}, name), h("span", {}, choiceLabel("provider", null, provider).replace(/ \([a-z_]+\)$/, "") + (model ? ` · ${model}` : ""))),
      h("span", {class: "grow"}),
      used.length ? h("span", {class: "status ok", title: "Referenced by name from these settings"}, "used for " + used.join(", "))
        : h("span", {class: "status new"}, "not used yet"),
      h("span", {class: "chev", "aria-hidden": "true"}, "▶")),
    h("div", {class: "body"},
      h("div", {class: "sec"}, h("div", {class: "st"}, "Model"),
        h("div", {class: "fgrid"},
          h("div", {class: "fld"}, h("label", {for: nameId}, "Name"),
            h("input", {id: nameId, class: "mono", value: name,
              onchange: (e) => { const to = e.target.value.trim();
                if (providers.rename(name, to)) { if (OPEN.delete(key)) OPEN.add("ai:" + to); renderForm(); } else e.target.value = name; }}),
            h("div", {class: "hint"}, "Other settings pick it by this name.")),
          pick(mainNames).map(o => field(o, sc, {after: ["provider", "api"].includes(o.name) ? redraw : undefined})))),
      pick(endNames).length ? h("div", {class: "sec"}, h("div", {class: "st"}, "Endpoint"),
        h("div", {class: "fgrid"}, pick(endNames).map(o => field(o, sc)))) : null,
      authBlock(scope(sc, "authentication"), ["api_key"], true),
      advOpts.length ? h("details", {class: "adv", open: advOpts.some(o => sc.get(o.name) !== undefined) || null},
        h("summary", {}, "Advanced"), h("div", {style: "margin-top:8px"}, fields(advOpts, sc))) : null,
      h("div", {class: "foot"}, h("button", {type: "button", class: "danger",
        onclick: () => { if (confirm(`Remove “${name}”? (Nothing changes until you Save.)`)) { providers.set(name, undefined); OPEN.delete(key); renderForm(); } }},
        "Remove provider"))));
  return card;
}
function formProviders(form) {
  const providers = scope(ROOT, "ai_providers"), names = Object.keys(providers.obj());
  const sem = scope(ROOT, "semantic"), dl = CFG.reference.semantic.find(o => o.name === "default_llm");
  if (dl) form.append(group("Default LLM", "semantic.default_llm", "Used by every step that doesn't name its own (catalog drafting, value extraction, Dive / Fly).",
    h("div", {class: "fgrid"}, field(dl, sem))));
  names.forEach(n => form.append(providerCard(providers, n)));
  const nameIn = h("input", {class: "mono", placeholder: "name, e.g. claude", "aria-label": "new AI provider name"});
  const provSel = h("select", {"aria-label": "provider"}, (CFG.reference.ai_provider.find(o => o.name === "provider")?.choices || ["anthropic"])
    .map(p => h("option", {value: p}, choiceLabel("provider", null, p))));
  const go = () => { const n = nameIn.value.trim(); if (!n) { nameIn.focus(); return; }
    if (n in providers.obj()) { toast(`“${n}” already exists`); return; }
    providers.set(n, {provider: provSel.value}); OPEN.add("ai:" + n); renderForm(); };
  nameIn.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  form.append(h("div", {class: "addbox"}, h("div", {class: "st muted small", style: "margin-bottom:6px"}, "Add an AI provider"),
    h("div", {class: "row"}, nameIn, provSel, h("button", {type: "button", class: "secondary", onclick: go}, "Add"))));
}

// ---- Saved tables page: duckduck.json → views ----
function savedCard(views, name) {
  const sc = scope(views, name, true), key = "view:" + name, def = sc.obj(), bound = !!def.table;
  const saved = name in ((CFG.config || {}).views || {}), failed = (SAVED?.failed || {})[name];
  const registered = (SAVED?.views || []).some(v => v.name === name);
  const call = bound ? `${def.table}(${Object.entries(def.args || {}).map(([k, v]) => `${k}=${sqlLiteral(v)}`).join(", ")})` : "";
  const pill = !saved ? h("span", {class: "status new", title: "Save to register it"}, "not saved yet")
    : failed ? h("span", {class: "status bad", title: failed}, "● not registered")
    : registered ? h("span", {class: "status ok"}, "● registered") : null;
  const nameId = "cf" + (++FID), sqlId = "cf" + (++FID);
  const card = h("details", {class: "ccard", id: "card-" + key, open: OPEN.has(key) || null, ontoggle: openToggle(key)},
    h("summary", {}, iconEl("dataset"),
      h("span", {class: "who"}, h("b", {}, name), h("span", {}, bound ? `over ${call}` : "saved query")),
      h("span", {class: "grow"}), pill, h("span", {class: "chev", "aria-hidden": "true"}, "▶")),
    h("div", {class: "body"},
      failed ? h("div", {class: "errbox"}, h("b", {}, "Not registered: "), h("span", {class: "err"}, failed)) : null,
      h("div", {class: "sec"}, h("div", {class: "st"}, bound ? "A table over a table function" : "A saved query"),
        h("div", {class: "fgrid"},
          h("div", {class: "fld"}, h("label", {for: nameId}, "Name"),
            h("input", {id: nameId, class: "mono", value: name,
              onchange: (e) => { const to = e.target.value.trim().toLowerCase();
                if (views.rename(name, to)) { if (OPEN.delete(key)) OPEN.add("view:" + to); renderForm(); } else e.target.value = name; }}),
            h("div", {class: "hint"}, "SELECT * FROM this name.")),
          field({name: "description", input: "text", description: "What it holds — shown in the SQL tab, used by catalog drafting."}, sc, {ctx: "views", wide: true}),
          bound ? [field({name: "table", input: "text", required: true, description: "A registered table function (e.g. sn_table)."}, sc, {ctx: "views"}),
                   field({name: "args", input: "json", required: true, description: "Its arguments, fixed: {\"table_name\": \"incident\"}."}, sc, {ctx: "views"})]
            : h("div", {class: "fld wide"}, h("label", {for: sqlId}, "Query", h("span", {class: "key"}, "sql")),
                h("textarea", {id: sqlId, class: "mono fjson", spellcheck: "false", value: def.sql || "", style: "min-height:90px",
                  oninput: (e) => sc.set("sql", e.target.value.trim() || undefined)}),
                h("div", {class: "hint"}, "A read query over the registered tables — it runs each time the table is read.")))),
      h("div", {class: "foot"},
        saved ? h("button", {type: "button", class: "secondary", style: "margin-right:auto",
          title: "Its statement in the editing dialog: change it, rename it — saved at once", onclick: () => editSavedTable(name)},
          "✎ Edit the statement") : null,
        h("button", {type: "button", class: "danger",
        onclick: () => { if (confirm(`Remove the saved table “${name}”? (Nothing changes until you Save.)`)) { views.set(name, undefined); OPEN.delete(key); renderForm(); } }},
        "Remove saved table"))));
  return card;
}
function formSaved(form) {
  const views = scope(ROOT, "views"), names = Object.keys(views.obj());
  form.append(h("p", {class: "muted small", style: "margin:0 0 6px"}, "Make one from the SQL tab: write a query and press ",
    h("b", {}, "Save as table"), " — or ", h("b", {}, "＋ Table"), " on a table behind a connector's catalog. ",
    h("a", {href: "#", onclick: (e) => { e.preventDefault(); openTab("sql"); }}, "Open the SQL tab")));
  if (!names.length) form.append(h("p", {class: "muted"}, "No saved tables yet."));
  names.forEach(n => form.append(savedCard(views, n)));
  const stray = Object.keys(SAVED?.failed || {}).filter(n => !names.includes(n));
  if (stray.length) form.append(h("p", {class: "msg warn"}, `Not registered, and no longer in this draft: ${stray.join(", ")}.`));
}

// ---- Ask pages: the semantic section, a page per concern ----
function formSemantic(form, page) {
  const ref = CFG.reference.semantic, sem = scope(ROOT, "semantic");
  let names = CFG_PAGES[page]?.semantic || [];
  if (page === "answers") {  // anything no page claims shows up here, so every option stays reachable
    const claimed = new Set(Object.values(CFG_PAGES).flatMap(p => p.semantic || []));
    names = [...names, ...ref.map(o => o.name).filter(n => !claimed.has(n))];
  }
  const opts = names.map(n => ref.find(o => o.name === n)).filter(Boolean);
  const flat = opts.filter(o => !o.options), nested = opts.filter(o => o.options);
  if (nested.length === 1 && !flat.length) {  // a page that is one object (thresholds): its fields, no extra box
    const o = nested[0];
    form.append(fields(o.options, scope(sem, o.name), o.name));
    return;
  }
  if (flat.length) form.append(group("General", null, "", h("div", {class: "fgrid"}, flat.map(o => field(o, sem)))));
  nested.forEach(o => form.append(group(labelOf(o.name), "semantic." + o.name, o.description, fields(o.options, scope(sem, o.name), o.name))));
}
function renderForm() {
  const ref = CFG?.reference; if (!ref) return;
  const form = $("#cfgform"); form.replaceChildren();
  if (CFGPAGE === "connectors") formConnectors(form);
  else if (CFGPAGE === "ai") formProviders(form);
  else if (CFGPAGE === "saved") formSaved(form);
  else formSemantic(form, CFGPAGE);
  navCounts();
}
// the overview: what's configured, what needs attention
function drawOverview() {
  const box = $("#cfgoverview"); if (!box) return;
  const svcs = Object.keys(DRAFT.services || {}), conn = CONN?.services || [];
  const failed = conn.filter(s => !s.started), started = conn.filter(s => s.started);
  const ai = Object.keys(DRAFT.ai_providers || {}), sem = DRAFT.semantic || {};
  const cat = CAT?.sources || [], notConn = (CAT?.status || []).filter(r => !r.connected);
  const badViews = Object.entries(SAVED?.failed || {});
  const tile = (page, k, v, sub, bad) => `<button type="button" class="tile" data-cfggo="${page}"><span class="k">${esc(k)}</span>
    <span class="v">${esc(v)}</span><span class="s ${bad ? "bad" : ""}">${esc(sub)}</span></button>`;
  box.innerHTML = `<div class="card"><h3 style="margin:0 0 10px">At a glance</h3><div class="tiles">
    ${tile("connectors", "Connectors", svcs.length, CONN ? `${started.length} connected${failed.length ? ` · ${failed.length} didn't start` : ""}` : "status needs the SQL console", failed.length)}
    ${tile("ai", "AI providers", ai.length, sem.default_llm ? `default LLM: ${sem.default_llm}` : ai.length ? "no default LLM" : "none yet — Ask works offline", false)}
    ${tile("catalog", "Semantic catalog", cat.length, META?.setup ? "Ask is off — no catalog yet" : `tables described${notConn.length ? ` · ${notConn.length} not connected` : ""}`, !!META?.setup || notConn.length)}
    ${tile("reading", "Decisions by", sem.decision_engine?.ai_provider || "offline rules", `reading mode: ${sem.reader || "rules"}`, false)}
  </div>
  ${failed.length || notConn.length || badViews.length ? `<h4 style="margin:16px 0 4px">Needs attention</h4><ul class="problems">
    ${badViews.map(([n, why]) => `<li>Saved table <b>${esc(n)}</b> isn't registered: <span class="err">${esc(why)}</span> —
      <a href="#" data-cfggo="saved" data-open="view:${esc(n)}">open it</a></li>`).join("")}
    ${failed.map(s => `<li><b>${esc(s.name)}</b> <span class="muted">(${esc(connectorName(s.connector))})</span> didn't start:
      <span class="err">${esc(s.error || "unknown error")}</span> — <a href="#" data-cfggo="connectors" data-open="svc:${esc(s.name)}">open it</a></li>`).join("")}
    ${notConn.length ? `<li>${notConn.length} catalog table${notConn.length === 1 ? "" : "s"} can't be used in answers — <a href="#" data-cfggo="connections">see why</a></li>` : ""}
  </ul>` : `<p class="msg good" style="margin-top:12px">✓ Nothing needs attention.</p>`}</div>`;
}

function optRows(opts, ctx) {
  return opts.map(o => o.options ? `<details data-find="${esc((o.name + " " + labelOf(o.name, ctx) + " " + (o.description || "")).toLowerCase())}"><summary>${esc(labelOf(o.name, ctx))} <span class="t mono">${esc(o.name)}</span></summary>
      ${o.description ? `<div class="d">${esc(plain(o.description))}</div>` : ""}${optRows(o.options, o.name)}</details>`
    : `<div class="opt" data-find="${esc((o.name + " " + labelOf(o.name, ctx) + " " + (o.description || "")).toLowerCase())}"><span class="n">${esc(labelOf(o.name, ctx))}</span><span class="t mono">${esc(o.name)} · ${esc(o.type || "")}</span>
      ${o.required ? `<span class="badge">required</span>` : ""}${o.secret ? `<span class="badge">secret</span>` : ""}
      ${o.description ? `<div class="d">${esc(plain(o.description))}</div>` : ""}
      ${o.default !== null && o.default !== undefined && o.default !== "" ? `<div class="def">default: <span class="mono">${esc(JSON.stringify(o.default))}</span></div>` : ""}</div>`).join("");
}
function drawReference() {
  const ref = CFG.reference;
  $("#optref").innerHTML =
    `<details open data-find="top level general"><summary>General (top level)</summary>${optRows(ref.top_level)}</details>` +
    `<details data-find="services service connectors"><summary>Every connector</summary>${optRows(ref.service_common)}</details>` +
    `<details data-find="authentication sign-in"><summary>Sign-in <span class="t mono">authentication</span></summary>${optRows(ref.authentication, "authentication")}</details>` +
    `<details data-find="connectors"><summary>Connector types</summary>` + ref.connectors.map(c =>
      `<details data-find="${esc((c.connector + " " + connectorName(c.connector) + " " + c.description).toLowerCase())}"><summary>${icon(c.icon || "api")} ${esc(connectorName(c.connector))} <span class="t mono">${esc(c.connector)}</span></summary>
        <div class="d">${esc((CONNECTOR_INFO[c.connector] || [])[1] || "")}</div>
        <div class="def">tables: ${c.dynamic_tables ? "one per file / module function" : esc(c.tables.join(", "))}${c.requires_authentication ? "" : " · sign-in optional"}</div>
        ${optRows(c.options, c.connector)}</details>`).join("") + `</details>` +
    `<details data-find="ai_providers ai providers llm"><summary>An AI provider <span class="t mono">ai_providers.&lt;name&gt;</span></summary>${optRows(ref.ai_provider)}</details>` +
    `<details data-find="semantic ask"><summary>Ask <span class="t mono">semantic</span></summary>${optRows(ref.semantic)}</details>`;
}
$("#optfilter").addEventListener("input", () => {
  const f = $("#optfilter").value.trim().toLowerCase();
  $("#optref").querySelectorAll(".opt").forEach(el => { el.hidden = !!f && !el.dataset.find.includes(f); });
  $("#optref").querySelectorAll("details").forEach(d => {
    const hit = !f || d.dataset.find.includes(f) || [...d.querySelectorAll(".opt")].some(o => !o.hidden);
    d.hidden = !hit; if (f && hit) d.open = true;
  });
});

function showFeatures() {
  showReaders();
  // no catalog yet (a first run): Ask says what's missing; SQL and Config work
  const setup = META?.setup, gen = !!META?.features?.catalog_generation;
  $("#setupcard").hidden = !setup;
  $("#enablebar").hidden = !setup;
  if (!setup && CAT) $("#enablecard").hidden = true;
  $("#setuppath").textContent = setup ? setup.catalog_path : "";
  $("#setuphow").textContent = !setup ? "" : gen
    ? "Set up the catalog drafts it from your connected tables with your LLM (Config → Semantic catalog → Update catalog)."
    : META?.features?.config
      ? "To draft it from here, start the server with --edit-config; or run: python -m duckduck.semantic generate-catalog"
      : "Draft it with: python -m duckduck.semantic generate-catalog — or write it by hand (examples/semantic/catalog.yaml).";
  $("#setupconfig").hidden = !(setup && META?.features?.config);
  $("#askform").querySelectorAll("input, button, select").forEach(el => { if (setup) el.disabled = true; else if (el.dataset.setupOff) el.disabled = false; if (setup) el.dataset.setupOff = "1"; });
  if (!setup) $("#askform").querySelectorAll("[data-setup-off]").forEach(el => delete el.dataset.setupOff);
  const sql = !!META?.features?.sql;  // the tab is always there; off, it says how to turn it on
  $("#sqloff").hidden = sql; $("#sqlon").hidden = !sql;
  document.querySelector('nav button[data-tab="config"]').hidden = !META?.features?.config;
}
$("#setupsql").addEventListener("click", () => openTab("sql"));
$("#setupconfig").addEventListener("click", goEnable);
api("/api/meta").then(m => {
  META = m; showFeatures();
  if (location.hash === "#enable-ask" && META.setup) goEnable();
  const page = /^#config\/(\w+)$/.exec(location.hash);  // a bookmarked Config page
  if (page && META.features?.config && CFG_PAGES[page[1]]) { CFGPAGE = page[1]; openTab("config"); }
}).catch(err => toast(err.message));
</script>
</body>
</html>
"""

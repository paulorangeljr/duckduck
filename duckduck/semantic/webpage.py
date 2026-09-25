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
.fbok { color: var(--good-ink); font-weight: 600; } .fberr { color: var(--bad); }
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
.sqlgrid { display: grid; grid-template-columns: 270px minmax(0, 1fr); gap: 14px; align-items: start; }
.cfggrid { display: grid; grid-template-columns: minmax(0, 1fr) 400px; gap: 14px; align-items: start; }
textarea.editor { width: 100%; min-height: 180px; resize: vertical; tab-size: 2; line-height: 1.45; }
#cfgtext { min-height: 520px; }
.tlist { max-height: 70vh; overflow: auto; margin-top: 8px; }
.tlist h4 { margin: 10px 0 4px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.titem { display: flex; gap: 6px; align-items: center; width: 100%; background: none; border: 0; padding: 4px 6px;
         border-radius: 6px; color: var(--ink); font: inherit; font-size: 13.5px; text-align: left; cursor: pointer; }
.titem:hover { background: var(--surface-2); }
.titem .kind { margin-left: auto; font-size: 11px; color: var(--muted); }
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
@media (max-width: 860px) { .sqlgrid, .cfggrid { grid-template-columns: 1fr; } }
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
<main>
  <section id="tab-ask">
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
      <aside class="card"><h3>Tables</h3>
        <input id="tablefilter" placeholder="filter tables" style="width:100%">
        <div class="tlist" id="tablelist"></div></aside>
      <div>
        <div class="card">
          <textarea class="editor mono" id="sqltext" spellcheck="false" aria-label="SQL">SHOW TABLES</textarea>
          <div class="row" style="margin-top:8px"><button class="primary" id="sqlrun">Run</button>
            <span class="muted small">Ctrl+Enter · read queries only (SELECT, WITH, SHOW, DESCRIBE…) · no file or network access</span></div>
        </div>
        <div id="sqlresult"></div>
      </div>
    </div>
  </section>
  <section id="tab-config" hidden>
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
    <div class="cfggrid">
      <div class="card"><div class="cfghead"><h3>duckduck.json <span class="muted small mono" id="cfgpath"></span></h3>
          <div class="seg" role="group" aria-label="Edit as">
            <button type="button" data-view="form" aria-pressed="true">Form</button>
            <button type="button" data-view="json" aria-pressed="false">JSON</button></div></div>
        <p class="muted small" id="cfgnote"></p>
        <div id="cfgform"></div>
        <textarea class="editor mono" id="cfgtext" spellcheck="false" aria-label="duckduck.json" hidden></textarea>
        <div class="row cfgbar"><button class="secondary" id="cfgvalidate">Validate</button>
          <button class="primary" id="cfgsave">Save and reload</button>
          <button class="secondary" id="cfgreset">Discard changes</button></div>
        <div id="cfgmsgs"></div>
      </div>
      <aside class="card optref"><h3>Every option</h3>
        <input id="optfilter" placeholder="search options" style="width:100%">
        <div id="optref" style="margin-top:8px"></div></aside>
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
  ({history: loadHistory, dashboard: loadDashboard, suggestions: loadSuggestions,
    sql: () => META?.features?.sql && loadTables(),
    config: () => { loadCatalog(); return CFG || loadConfig(); }})[b.dataset.tab]?.();  // config: loaded once, so switching tabs keeps edits
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
  const b = $("#askbtn"); b.disabled = busy(); b.textContent = busy() ? (READER_BUSY[READER] || "Reading…") : "Ask";
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
  const body = {question: q, user: user(), reader: READER, ...extra};
  if (pre.chosen) body.only_sources = [...pre.chosen];
  if (pre.entity) body.entity = pre.entity;
  if (pre.field) body.values_field = pre.field;
  if (pre.blocked.size) body.blocked_joins = [...pre.blocked].map(k => k.split("="));
  $("#conversation").innerHTML = `<div class="card muted">Thinking…</div>`;
  try { render(await api("/api/ask", body)); }
  catch (err) { $("#conversation").innerHTML = `<div class="card error">${esc(err.message)}</div>`; }
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

async function reply(convId, text) {
  try { render(await api("/api/answer", {conversation_id: convId, reply: text})); }
  catch (err) { toast(err.message); }
}

function table(rows, max = 500) {
  if (!rows || !rows.length) return `<p class="muted">No rows.</p>`;
  const cols = Object.keys(rows[0]);
  const num = cols.map(c => rows.every(r => r[c] === null || typeof r[c] === "number"));
  return `<div class="tablewrap"><table><thead><tr>${cols.map((c, i) => `<th class="${num[i] ? "num" : ""}">${esc(c)}</th>`).join("")}</tr></thead>
    <tbody>${rows.slice(0, max).map(r => `<tr>${cols.map((c, i) => `<td class="${num[i] ? "num" : ""}">${r[c] === null ? '<span class="muted">—</span>' : esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

const SHAPE_WORDS = {list: "a list", count: "a count", values: "the different values", count_values: "a count of values",
                     count_by: "a count per group", lookup: "everything about the value", locate: "where the value is",
                     catalog: "what data there is", small_talk: "small talk", out_of_scope: "not about the data",
                     browse: "the table itself"};

function render(c) {
  const r = c.result, box = $("#conversation");
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
      <span class="muted small">${n} row${n === 1 ? "" : "s"}${c.truncated ? " (first 500 shown)" : ""}</span>
      ${runPill(r)}
      ${META?.features?.sql && n ? `<button class="secondary" type="button" data-takeover style="margin-left:auto;padding:4px 10px;font-size:13px"
        title="Register these rows as a table and continue in the SQL tab">Take over from here</button>` : ""}</div>
      ${table(r.results)}
      ${r.summary ? `<details><summary>Where it was looked for</summary>${table(r.summary)}</details>` : ""}
      ${(r.sections || []).filter(s => s.results && s.results.length).map(s =>
          `<details><summary>${esc(s.source)} — ${s.rows} row${s.rows === 1 ? "" : "s"}</summary>${table(s.results)}</details>`).join("")}
      ${r.sql ? `<details><summary>SQL</summary><pre>${esc(r.sql)}</pre>${META?.features?.sql && r.query_plan ? `<button class="secondary" type="button" data-runsql>Open in the SQL tab</button>` : ""}</details>` : ""}
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
  box.querySelectorAll("[data-suggest]").forEach(b => b.addEventListener("click", () => {
    $("#question").value = b.dataset.suggest; askFresh(); }));
  box.querySelector("[data-anyway]")?.addEventListener("click", () => ask(r.question, {in_scope: true}));
  $("#freeform")?.addEventListener("submit", (e) => { e.preventDefault(); const t = $("#freetext").value.trim(); if (t) reply(c.conversation_id, t); });
  wireFeedback(r);
}

// ---- feedback ----------------------------------------------------------
function options(obj, blank) { return (blank ? `<option value="">${blank}</option>` : "") +
  Object.entries(obj).map(([k, v]) => `<option value="${esc(k)}" title="${esc(v)}">${esc(k)}</option>`).join(""); }

function feedbackForm(r) {
  const m = META || {categories: {}, sources: {}, answer_shapes: {}, entities: {}, values: {}};
  return `<div class="card feedback" id="fb">
    <div class="fbhead"><h3>Did this answer your question?</h3>
      <span class="muted small" id="fbstatus" role="status" aria-live="polite">One tap — it learns from every answer.</span></div>
    <div class="row fbverdicts">
      <button class="verdict" type="button" data-v="answered" aria-pressed="false"><span class="e" aria-hidden="true">👍</span>Yes</button>
      <button class="verdict" type="button" data-v="partial" aria-pressed="false"><span class="e" aria-hidden="true">🤏</span>Partly</button>
      <button class="verdict" type="button" data-v="not_answered" aria-pressed="false"><span class="e" aria-hidden="true">👎</span>No</button>
    </div>
    <div id="fbmore" hidden>
      <p class="muted small fblabel">What went wrong? <span>Optional — tap any that fit; it saves as you go.</span></p>
      <div class="fbchips">${Object.entries(m.categories).map(([k, v]) =>
        `<button type="button" class="fbchip" data-cat="${esc(k)}" aria-pressed="false">${esc(v)}</button>`).join("")}</div>
      <textarea id="fbreason" rows="2" placeholder="Or say it in your words — e.g. I wanted how many, not the list"></textarea>
      <details><summary>What would have been right? <span class="muted small">(optional — this is what it learns the most from)</span></summary>
        <div class="grid2">
          <label>Tables<br><select id="fbsources" multiple size="${Math.min(Math.max(Object.keys(m.sources).length, 2), 8)}">${options(m.sources)}</select></label>
          <label>Kind of answer<br><select id="fbshape">${options(m.answer_shapes, "—")}</select></label>
          <label>What it asks about<br><select id="fbentity">${options(m.entities, "—")}</select></label>
        </div>
        <p class="muted small" style="margin:12px 0 4px">A word it misread: “<em>word</em>” means <em>field = value</em></p>
        <div class="row"><input id="fbword" placeholder="word, e.g. urgentes" style="width:180px">
          <select id="fbfield">${options(Object.fromEntries(Object.keys(m.values).map(k => [k, k])), "field")}</select>
          <select id="fbvalue"><option value="">value</option></select></div>
      </details>
    </div></div>`;
}

// Every tap and every word is saved as it happens — no "send" button. One answer keeps one feedback row:
// the first save creates it, later ones replace it (feedback_id).
let FB = null;  // {searchId, id, verdict, chain, timer}
const FB_THANKS = {answered: "✓ Saved — thanks! It'll remember this worked.",
                   partial: "✓ Saved. Tap what was missing below, if you like.",
                   not_answered: "✓ Saved. Tap what went wrong below, if you like — it learns from it."};
function fbStatus(text, cls = "") { const el = $("#fbstatus"); if (el) { el.textContent = text; el.className = "small " + (cls || "muted"); } }
function fbPress(verdict) {
  document.querySelectorAll("#fb button.verdict").forEach(x => x.setAttribute("aria-pressed", String(x.dataset.v === verdict)));
  const more = $("#fbmore"); if (more) more.hidden = !verdict || verdict === "answered";
}
function fbBody() {
  const answered = FB.verdict === "answered", expected = {};
  if (!answered) {
    const srcs = [...($("#fbsources")?.selectedOptions || [])].map(o => o.value);
    if (srcs.length) expected.sources = srcs;
    if ($("#fbshape")?.value) expected.answer_shape = $("#fbshape").value;
    if ($("#fbentity")?.value) expected.entity = $("#fbentity").value;
    if ($("#fbword")?.value.trim() && $("#fbfield")?.value && $("#fbvalue")?.value)
      expected.synonym = {word: $("#fbword").value.trim(), field: $("#fbfield").value, value: $("#fbvalue").value};
  }
  return {search_id: FB.searchId, verdict: FB.verdict, user: user(),
    categories: answered ? [] : [...document.querySelectorAll("#fb .fbchip[aria-pressed=true]")].map(b => b.dataset.cat),
    reason: answered ? "" : ($("#fbreason")?.value || ""), expected: answered ? null : expected};
}
function fbSave(thanks) {  // saves are chained, so the first one's id is known before the next replaces it
  const fb = FB; if (!fb || !fb.verdict) return;
  clearTimeout(fb.timer); fb.timer = null;
  fbStatus("Saving…");
  fb.chain = fb.chain.then(async () => {
    try {
      const saved = await api("/api/feedback", {...fbBody(), feedback_id: fb.id});
      fb.id = saved.id;
      if (FB === fb) fbStatus(thanks || "✓ Saved", "fbok");
    } catch (err) { if (FB === fb) fbStatus("Couldn't save — tap again. " + err.message, "fberr"); }
  });
}
function fbSoon() { if (FB?.verdict) { clearTimeout(FB.timer); fbStatus("…"); FB.timer = setTimeout(() => fbSave(), 700); } }

function wireFeedback(r) {
  const fb = $("#fb"); if (!fb) return;
  FB = {searchId: r.search_id, id: null, verdict: null, chain: Promise.resolve(), timer: null};
  fb.querySelectorAll("button.verdict").forEach(b => b.addEventListener("click", () => {
    FB.verdict = b.dataset.v; fbPress(FB.verdict);
    b.classList.remove("pop"); void b.offsetWidth; b.classList.add("pop");
    fbSave(FB_THANKS[FB.verdict]);
  }));
  fb.querySelectorAll(".fbchip").forEach(c => c.addEventListener("click", () => {
    c.setAttribute("aria-pressed", String(c.getAttribute("aria-pressed") !== "true")); fbSave();
  }));
  $("#fbreason").addEventListener("input", fbSoon);
  $("#fbreason").addEventListener("blur", () => { if (FB.timer) fbSave(); });
  $("#fbfield")?.addEventListener("change", () => {
    const vals = (META.values[$("#fbfield").value] || []);
    $("#fbvalue").innerHTML = `<option value="">value</option>` + vals.map(v => `<option>${esc(v)}</option>`).join("");
  });
  ["#fbsources", "#fbshape", "#fbentity", "#fbfield", "#fbvalue"].forEach(sel => $(sel)?.addEventListener("change", () => fbSave()));
  $("#fbword")?.addEventListener("input", fbSoon);
}
// "Take over from here" counts as a yes (the server records it when nobody rated the answer yet)
function fbTakenOver(feedback) {
  if (!feedback || !FB) return;
  Object.assign(FB, {id: feedback.id, verdict: feedback.verdict});
  fbPress(feedback.verdict); fbStatus("✓ Marked as answered — you took the rows over.", "fbok");
}

// ---- history -----------------------------------------------------------
function verdictBadge(v) {
  if (!v) return `<span class="muted small">not rated</span>`;
  const m = {answered: ["good", "✓", "answered"], partial: ["mid", "◐", "partly"], not_answered: ["bad", "✗", "not answered"]}[v];
  return `<span class="status ${m[0]}"><i aria-hidden="true">${m[1]}</i>${m[2]}</span>`;
}
async function loadHistory() {
  try {
    const mode = $("#historymode").value;
    const rows = await api("/api/searches?limit=200" + (mode ? "&reader=" + encodeURIComponent(mode) : ""));
    $("#history").innerHTML = rows.length ? `<div class="tablewrap"><table><thead><tr><th>When</th><th>Question</th><th>Mode</th><th>Result</th><th>Answer</th><th>Tables</th><th class="num">Time</th><th class="num">Calls</th><th>Feedback</th></tr></thead><tbody>${
      rows.map(r => `<tr><td class="small muted">${esc((r.created_at || "").replace("T", " ").slice(0, 16))}</td>
        <td>${esc(r.question)}${r.user_name ? `<div class="muted small">${esc(r.user_name)}</div>` : ""}</td>
        <td class="small" title="${esc(r.engine || "")}">${r.requested_reader === "auto" ? "🧭→" : ""}${modeName(r.reader)}</td>
        <td class="small">${esc(r.status)}</td><td class="small">${esc(r.answer_shape || "")}</td>
        <td class="small">${esc((JSON.parse(r.sources || "[]")).join(", "))}</td>
        <td class="num small">${ms(r.elapsed_ms)}</td>
        <td class="num small" title="decision-engine calls · LLM calls${r.llm_tokens ? " · " + r.llm_tokens + " LLM tokens" : ""}${r.cost !== null && r.cost !== undefined ? " · $" + r.cost.toFixed(6) : ""}">${r.engine_calls ?? 0} · ${r.llm_calls ?? 0}</td>
        <td>${verdictBadge(r.verdict)}${r.reason ? `<div class="muted small">${esc(r.reason)}</div>` : ""}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="muted">No questions asked yet${mode ? " in this mode" : ""}.</p>`;
  } catch (err) { $("#history").innerHTML = `<p class="error">${esc(err.message)}</p>`; }
}

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
async function loadTables() {
  try { TABLES = await api("/api/tables"); drawTables(); }
  catch (err) { $("#tablelist").innerHTML = `<p class="error small">${esc(err.message)}</p>`; }
}
function drawTables() {
  const f = $("#tablefilter").value.trim().toLowerCase();
  const rows = TABLES.filter(t => !f || (t.name + " " + (t.description || "") + " " + (t.service || "")).toLowerCase().includes(f));
  const groups = {};
  rows.forEach(t => (groups[t.service || "other"] ||= []).push(t));
  $("#tablelist").innerHTML = Object.entries(groups).map(([svc, ts]) => `<h4>${esc(svc)}</h4>` + ts.map(t =>
    `<button class="titem" type="button" data-usage="${esc(t.usage || ("SELECT * FROM " + t.name + " LIMIT 100"))}"
      title="${esc([t.description, t.pushdown ? "push-down: " + t.pushdown : ""].filter(Boolean).join("\n"))}">
      ${icon(t.icon)}<span>${esc(t.name)}</span><span class="kind">${esc(t.kind || "")}</span></button>`).join("")).join("")
    || `<p class="muted small">No tables.</p>`;
  $("#tablelist").querySelectorAll("[data-usage]").forEach(b => b.addEventListener("click", () => {
    let u = b.dataset.usage;
    if (!/^\s*(select|with|from|show|describe)/i.test(u)) u = `SELECT * FROM ${u}`;
    if (!/\blimit\b/i.test(u)) u += " LIMIT 100";
    $("#sqltext").value = u; $("#sqltext").focus();
  }));
}
$("#tablefilter").addEventListener("input", drawTables);
async function runSql() {
  const sql = $("#sqltext").value.trim(); if (!sql) return;
  $("#sqlresult").innerHTML = `<div class="card muted">Running…</div>`;
  try {
    const r = await api("/api/sql", {sql});
    const rows = (r.rows || []).map(row => Object.fromEntries(r.columns.map((c, i) => [c, row[i]])));
    $("#sqlresult").innerHTML = `<div class="card">` + (r.error ? `<p class="error">${esc(r.error)}</p>` :
      `<div class="muted small">${r.row_count} row${r.row_count === 1 ? "" : "s"} · ${r.elapsed_ms} ms${r.truncated ? " · first " + rows.length + " shown" : ""}</div>${table(rows)}`) +
      ((r.log || []).length ? `<details${r.error ? " open" : ""}><summary>What went to each source (push-down)</summary><pre class="log mono">${esc(r.log.join("\n"))}</pre></details>` : "") + `</div>`;
  } catch (err) { $("#sqlresult").innerHTML = `<div class="card error">${esc(err.message)}</div>`; }
}
$("#sqlrun").addEventListener("click", runSql);
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

// ---- Config tab --------------------------------------------------------------------------
let CFG = null;
async function loadConfig() {
  try {
    CFG = await api("/api/config");
    $("#cfgpath").textContent = CFG.path || "";
    DRAFT = JSON.parse(JSON.stringify(CFG.config || {}));
    syncJson(); setView(CFGVIEW === "json" ? "json" : "form");
    $("#cfgnote").textContent = (CFG.editable ? "Secrets show as \"***\" — leave them to keep the saved value, or type a new one. Saving keeps a .bak and reconnects everything."
      : "Read-only here: start the server with --edit-config to save. Secrets show as \"***\".");
    $("#cfgsave").disabled = !CFG.editable;
    $("#cfgmsgs").innerHTML = "";
    drawReference();
  } catch (err) { $("#cfgmsgs").innerHTML = `<p class="msg bad">${esc(err.message)}</p>`; }
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
  showReport({warnings: [...(d.warnings || []), ...(d.reload_error ? ["saved, but reconnecting failed: " + d.reload_error] : [])]},
    d.reloaded ? `Saved (${d.saved}) and reconnected.` : `Saved (${d.saved}).`);
  META = await api("/api/meta").catch(() => META); showFeatures();
});
$("#cfgreset").addEventListener("click", loadConfig);
$("#cfgtext").addEventListener("keydown", (e) => {
  if (e.key === "Tab") { e.preventDefault(); const t = e.target, s = t.selectionStart;
    t.value = t.value.slice(0, s) + "  " + t.value.slice(t.selectionEnd); t.selectionStart = t.selectionEnd = s + 2; }
});
// ---- Config form: edits DRAFT, the JSON view shows it -----------------------------------------
// Every field comes from the options reference (/api/config → reference): its "input" says how to edit it.
// Only what's set is written; clearing a field removes the key (the default applies).
let DRAFT = {}, CFGVIEW = "form", FID = 0;
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
function iconEl(kind) { const s = h("span"); s.innerHTML = icon(kind); return s; }
function syncJson() { $("#cfgtext").value = JSON.stringify(DRAFT, null, 2); }
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
function field(opt, sc, {after, wide} = {}) {
  const id = "cf" + (++FID), cur = sc.get(opt.name), def = opt.default, kind = opt.input || "text";
  const set = (v) => { sc.set(opt.name, v); after?.(v); };
  const defText = def === null || def === undefined || def === "" ? "" : typeof def === "object" ? JSON.stringify(def) : String(def);
  let input, hint = plain(opt.description);
  if (kind === "choice" || kind === "bool") {
    const choices = kind === "bool" ? [true, false] : [...opt.choices];
    if (cur !== undefined && !choices.some(c => JSON.stringify(c) === JSON.stringify(cur))) choices.push(cur);
    input = h("select", {id, onchange: (e) => set(e.target.value === "" ? undefined : JSON.parse(e.target.value))},
      h("option", {value: ""}, defText ? `default (${defText})` : "—"),
      choices.map(c => h("option", {value: JSON.stringify(c), selected: JSON.stringify(c) === JSON.stringify(cur)}, String(c))));
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
    const list = LLM_REFS.has(opt.name) ? "cf-ai-names" : undefined;
    input = h("input", {id, type: secret ? "password" : "text", value: shown(cur), placeholder: defText, autocomplete: "off", list,
      oninput: (e) => { const t = e.target.value; set(t === "" ? undefined : kind === "scalar" ? scalarOf(t) : t); }});
    if (secret && cur === CFG.reference.secrets.mask) hint = "Saved — leave it to keep, or type a new value. " + hint;
    if (list) hint = hint || "An ai_providers name.";
  }
  return h("div", {class: "fld" + (wide ? " wide" : "")},
    h("label", {for: id}, opt.name, opt.required ? h("span", {class: "req", title: "required"}, " *") : null),
    input, hint ? h("div", {class: "hint"}, hint) : null);
}
function fields(opts, sc) {  // plain fields in a grid, nested sections below it
  const grid = h("div", {class: "fgrid"}), sections = [];
  opts.forEach(o => {
    if (o.options) {
      sections.push(h("details", {class: "sect", open: Object.keys(scope(sc, o.name).obj()).length ? true : null},
        h("summary", {}, o.name), o.description ? h("div", {class: "hint muted small"}, plain(o.description)) : null,
        fields(o.options, scope(sc, o.name))));
    } else grid.append(field(o, sc));
  });
  return h("div", {}, grid, sections);
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
    const missing = suggest.filter(k => !(k in sc.obj()));
    const addKey = (k) => { if (!k || k in sc.obj()) return; sc.set(k, ""); draw();
      box.querySelector(`.kv:last-of-type input[aria-label="${CSS.escape(k)}"]`)?.focus(); };
    box.append(h("div", {}, missing.map(k => h("button", {type: "button", class: "add", onclick: () => addKey(k)}, "+ " + k)),
      h("button", {type: "button", class: "add", onclick: () => { const k = prompt("Key name:"); if (k) addKey(k.trim()); }}, "+ other key")));
    if (note) box.append(h("div", {class: "hint muted small"}, note));
  };
  draw();
  return box;
}
function authBlock(sc, credentials, optional) {
  const box = h("div", {class: "sub"});
  const draw = () => {
    const type = sc.get("type") || "local";
    const typeOpt = CFG.reference.authentication.find(o => o.name === "type");
    const known = AUTH_KEYS[type] || [];
    box.replaceChildren(
      h("div", {class: "t"}, "authentication", optional ? h("span", {class: "muted"}, " — optional for this connector") : null),
      h("div", {class: "fgrid"}, field(typeOpt, sc, {after: draw}),
        known.map(k => field(CFG.reference.authentication.find(o => o.name === k), sc))),
      h("div", {class: "t", style: "margin-top:10px"}, type === "local" ? "Credentials" : "On top of the fetched secret"),
      keyRows(sc, ["type", ...known], credentials, type === "local"
        ? "Used exactly as written. Prefer aws / azure so no secret sits in this file."
        : "Optional: a value overrides the secret's key; \"$secret.<key>\" copies another key of the secret."));
  };
  draw();
  return box;
}
function connectorSelect(current, onchange) {
  const names = CFG.reference.connectors.map(c => c.connector);
  if (current && !names.includes(current)) names.push(current);
  return h("select", {"aria-label": "connector", onchange: (e) => onchange(e.target.value)},
    names.map(n => h("option", {value: n, selected: n === current}, n)));
}
function serviceEntry(services, name) {
  const sc = scope(services, name, true);
  const conn = sc.get("connector") || name;
  const ref = CFG.reference.connectors.find(c => c.connector === conn);
  const opts = ref ? ref.options.filter(o => !o.credential) : [];
  const creds = ref ? ref.options.filter(o => o.credential).map(o => o.name) : [];
  const common = CFG.reference.service_common.filter(o => o.name === "table_prefix");
  const known = ["connector", "authentication", ...common.map(o => o.name), ...opts.map(o => o.name)];
  const el = h("div", {class: "entry"},
    h("div", {class: "head"}, iconEl(ref?.icon || "api"),
      h("input", {class: "name mono", value: name, "aria-label": "service name",
        onchange: (e) => { if (services.rename(name, e.target.value.trim())) renderForm(); else e.target.value = name; }}),
      connectorSelect(conn, (c) => { sc.set("connector", c); el.replaceWith(serviceEntry(services, name)); }),
      h("span", {class: "grow muted small"}, ref ? (ref.dynamic_tables ? "tables: one per file / module function"
        : `tables ${sc.get("table_prefix") ?? name}_…: ${ref.tables.join(", ")}`) : "unknown connector"),
      h("button", {type: "button", class: "x", onclick: () => { if (confirm(`Remove the service “${name}”?`)) { services.set(name, undefined); renderForm(); } }}, "remove")),
    h("div", {class: "fgrid"}, [...common, ...opts].map(o => field(o, sc))),
    authBlock(scope(sc, "authentication"), creds, ref && !ref.requires_authentication));
  const extra = Object.keys(sc.obj()).filter(k => !known.includes(k));
  if (extra.length) el.append(h("div", {class: "sub"}, h("div", {class: "t"}, "Other keys"),
    keyRows(sc, known, [], "Not options of this connector — validation will say if they're ignored.")));
  return el;
}
function providerEntry(providers, name) {
  const sc = scope(providers, name, true);
  const opts = CFG.reference.ai_provider.filter(o => o.name !== "authentication");
  const el = h("div", {class: "entry"},
    h("div", {class: "head"},
      h("input", {class: "name mono", value: name, "aria-label": "provider name",
        onchange: (e) => { if (providers.rename(name, e.target.value.trim())) renderForm(); else e.target.value = name; }}),
      h("span", {class: "grow muted small"}, "referenced by this name from semantic"),
      h("button", {type: "button", class: "x", onclick: () => { if (confirm(`Remove “${name}”?`)) { providers.set(name, undefined); renderForm(); } }}, "remove")),
    fields(opts, sc),
    authBlock(scope(sc, "authentication"), ["api_key"], true));
  return el;
}
function addRow(label, withConnector, add) {
  const nameIn = h("input", {placeholder: "name", class: "mono", "aria-label": label + " name"});
  let conn = CFG.reference.connectors[0]?.connector;
  const go = () => { const n = nameIn.value.trim(); if (!n) { nameIn.focus(); return; }
    if (!add(n, conn)) { toast(`“${n}” already exists`); return; } renderForm(); };
  nameIn.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  return h("div", {class: "row", style: "margin-top:6px"}, nameIn,
    withConnector ? connectorSelect(conn, (c) => { conn = c; }) : null,
    h("button", {type: "button", class: "secondary", onclick: go}, "Add " + label));
}
function renderForm() {
  const ref = CFG?.reference; if (!ref) return;
  const services = scope(ROOT, "services", true), providers = scope(ROOT, "ai_providers");
  const form = $("#cfgform"); form.replaceChildren();
  form.append(h("datalist", {id: "cf-ai-names"}, Object.keys(providers.obj()).map(n => h("option", {value: n}))));
  form.append(h("h4", {}, "General"), h("div", {class: "fgrid"}, ref.top_level.filter(o => o.input).map(o => field(o, ROOT))));
  form.append(h("h4", {}, "Services", h("span", {class: "muted"}, "one per connection; its tables are <name>_<table>")));
  Object.keys(services.obj()).forEach(n => form.append(serviceEntry(services, n)));
  form.append(addRow("service", true, (n, c) => {
    if (n in services.obj()) return false;
    const auth = (CFG.reference.connectors.find(x => x.connector === c) || {}).requires_authentication === false ? {} : {authentication: {type: "local"}};
    services.set(n, {connector: c, ...auth}); return true; }));
  form.append(h("h4", {}, "AI providers", h("span", {class: "muted"}, "LLMs and decision engines, named here and used by name in semantic")));
  Object.keys(providers.obj()).forEach(n => form.append(providerEntry(providers, n)));
  form.append(addRow("AI provider", false, (n) => {
    if (n in providers.obj()) return false; providers.set(n, {provider: "anthropic"}); return true; }));
  form.append(h("h4", {}, "Semantic search"), fields(ref.semantic, scope(ROOT, "semantic")));
}
function setView(view) {
  if (view === "form" && CFGVIEW === "json") {
    try { DRAFT = JSON.parse($("#cfgtext").value || "{}"); }
    catch (err) { $("#cfgmsgs").innerHTML = `<p class="msg bad">Fix the JSON first: ${esc(err.message)}</p>`; return; }
    if (!isObj(DRAFT)) { $("#cfgmsgs").innerHTML = `<p class="msg bad">The config must be a JSON object.</p>`; DRAFT = {}; return; }
  }
  CFGVIEW = view;
  document.querySelectorAll(".seg [data-view]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
  $("#cfgform").hidden = view !== "form"; $("#cfgtext").hidden = view !== "json";
  if (view === "form") renderForm(); else syncJson();
}
document.querySelectorAll(".seg [data-view]").forEach(b => b.addEventListener("click", () => setView(b.dataset.view)));

function optRows(opts) {
  return opts.map(o => o.options ? `<details data-find="${esc((o.name + " " + (o.description || "")).toLowerCase())}"><summary>${esc(o.name)} <span class="t">${esc(o.type || "")}</span></summary>
      ${o.description ? `<div class="d">${esc(o.description)}</div>` : ""}${optRows(o.options)}</details>`
    : `<div class="opt" data-find="${esc((o.name + " " + (o.description || "")).toLowerCase())}"><span class="n">${esc(o.name)}</span><span class="t">${esc(o.type || "")}</span>
      ${o.required ? `<span class="badge">required</span>` : ""}${o.secret ? `<span class="badge">secret</span>` : ""}
      ${o.description ? `<div class="d">${esc(o.description)}</div>` : ""}
      ${o.default !== null && o.default !== undefined && o.default !== "" ? `<div class="def">default: <span class="mono">${esc(JSON.stringify(o.default))}</span></div>` : ""}</div>`).join("");
}
function drawReference() {
  const ref = CFG.reference;
  $("#optref").innerHTML =
    `<details open data-find="top level"><summary>Top level</summary>${optRows(ref.top_level)}</details>` +
    `<details data-find="services service"><summary>Every service</summary>${optRows(ref.service_common)}</details>` +
    `<details data-find="authentication"><summary>authentication</summary>${optRows(ref.authentication)}</details>` +
    `<details data-find="connectors"><summary>Connectors</summary>` + ref.connectors.map(c =>
      `<details data-find="${esc((c.connector + " " + c.description).toLowerCase())}"><summary>${icon({insightvm: "insightvm", blob_storage: "blob_storage", files: "files", python: "python"}[c.connector] || c.connector)} ${esc(c.connector)} <span class="t">${esc(c.class)}</span></summary>
        ${c.description ? `<div class="d">${esc(c.description)}</div>` : ""}
        <div class="def">tables: ${c.dynamic_tables ? "one per file / module function" : esc(c.tables.join(", "))}${c.requires_authentication ? "" : " · authentication optional"}</div>
        ${optRows(c.options)}</details>`).join("") + `</details>` +
    `<details data-find="ai_providers ai providers llm"><summary>An ai_providers entry</summary>${optRows(ref.ai_provider)}</details>` +
    `<details data-find="semantic"><summary>semantic</summary>${optRows(ref.semantic)}</details>`;
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
  const sql = !!META?.features?.sql;  // the tab is always there; off, it says how to turn it on
  $("#sqloff").hidden = sql; $("#sqlon").hidden = !sql;
  document.querySelector('nav button[data-tab="config"]').hidden = !META?.features?.config;
}
api("/api/meta").then(m => { META = m; showFeatures(); }).catch(err => toast(err.message));
</script>
</body>
</html>
"""

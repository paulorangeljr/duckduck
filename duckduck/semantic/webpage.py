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
section[hidden] { display: none; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
        padding: 16px 18px; margin-bottom: 14px; }
.ask { display: flex; gap: 8px; }
input, textarea, select { font: inherit; color: var(--ink); background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.ask input { flex: 1; font-size: 16px; padding: 10px 12px; }
button.primary, button.secondary, button.option, button.verdict {
  font: inherit; border-radius: 8px; padding: 8px 14px; cursor: pointer; border: 1px solid var(--border); }
button.primary { background: var(--accent); color: var(--accent-ink); border-color: transparent; font-weight: 600; }
button.secondary, button.option, button.verdict { background: var(--surface-2); color: var(--ink); }
button.option { display: block; width: 100%; text-align: left; margin: 6px 0; }
button.option .detail { color: var(--muted); font-size: 13px; }
button:disabled { opacity: .5; cursor: default; }
button.verdict[aria-pressed="true"] { outline: 2px solid var(--accent); }
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
    <button role="tab" data-tab="history" aria-selected="false">History</button>
    <button role="tab" data-tab="dashboard" aria-selected="false">Dashboard</button>
    <button role="tab" data-tab="suggestions" aria-selected="false">Suggestions</button>
    <button role="tab" data-tab="sql" aria-selected="false" hidden>SQL</button>
    <button role="tab" data-tab="config" aria-selected="false" hidden>Config</button>
  </nav>
  <div class="user"><label for="user">You</label><input id="user" placeholder="your name (optional)"></div>
</header>
<main>
  <section id="tab-ask">
    <div class="card">
      <form class="ask" id="askform">
        <input id="question" placeholder="Ask about your data — e.g. Which hosts have critical alerts?" autocomplete="off">
        <button class="primary" type="submit">Ask</button>
      </form>
      <div class="chips" id="chips" aria-live="polite"></div>
      <div class="panel" id="chippanel" hidden></div>
    </div>
    <div id="conversation"></div>
  </section>
  <section id="tab-history" hidden><div class="card"><div id="history"></div></div></section>
  <section id="tab-dashboard" hidden><div id="dashboard"></div></section>
  <section id="tab-sql" hidden>
    <div class="sqlgrid">
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
    <div class="cfggrid">
      <div class="card"><h3>duckduck.json <span class="muted small mono" id="cfgpath"></span></h3>
        <p class="muted small" id="cfgnote"></p>
        <textarea class="editor mono" id="cfgtext" spellcheck="false" aria-label="duckduck.json"></textarea>
        <div class="row" style="margin-top:8px"><button class="secondary" id="cfgvalidate">Validate</button>
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
      <button class="secondary" id="evalbtn">Evaluate and calibrate</button>
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
  ({history: loadHistory, dashboard: loadDashboard, suggestions: loadSuggestions, sql: loadTables,
    config: loadConfig})[b.dataset.tab]?.();
}));
$("#user").value = store.get("duckduck-user") || "";
$("#user").addEventListener("change", () => store.set("duckduck-user", $("#user").value));

// ---- while typing: what the question seems to be about --------------------
// A pause in typing asks /api/preview (one decision-engine batch, nothing run). The chips show the systems
// and the entity it points at; clicking one lets the user choose. Their choice goes with the question:
// only_sources (nothing else is used, joins included) and entity (pinned, never asked).
const pre = {data: null, chosen: null, entity: null, open: null, seq: 0, timer: null, ctrl: null, thinking: false};
$("#question").addEventListener("input", () => {
  clearTimeout(pre.timer);
  if (!$("#question").value.trim()) { Object.assign(pre, {data: null, chosen: null, entity: null, open: null}); drawChips(); return; }
  pre.timer = setTimeout(runPreview, 600);
});
async function runPreview() {
  const q = $("#question").value.trim();
  if (q.length < 8 || q.split(/\s+/).length < 2) return;
  pre.ctrl?.abort(); pre.ctrl = new AbortController();
  const mine = ++pre.seq; pre.thinking = true; drawChips();
  try {
    const d = await api("/api/preview", {question: q}, pre.ctrl.signal);
    if (mine !== pre.seq) return;
    pre.data = d.error ? null : d;
  } catch (err) { if (err.name === "AbortError") return; }
  pre.thinking = false; drawChips();
}
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
      chips.push(`<button class="chip ${pre.chosen ? "mine" : ""}" type="button" data-open="systems" aria-expanded="${pre.open === "systems"}"><span class="dot"></span><span class="k">Systems</span> ${icons.map(icon).join("")}${esc(label)}</button>`);
    }
    const e = d.entity;
    if (shape !== "catalog" && e && (pre.entity || e.sure)) {
      const name = (pre.entity || e.choice).replace(/_/g, " ");
      chips.push(`<button class="chip ${pre.entity ? "mine" : ""}" type="button" data-open="entity" aria-expanded="${pre.open === "entity"}"><span class="dot"></span><span class="k">About</span> ${esc(name)}</button>`);
    }
    if (shape && !["list", "catalog"].includes(shape) && d.answer_shape.sure)
      chips.push(`<button class="chip" type="button" disabled><span class="dot"></span><span class="k">Answer</span> ${esc(SHAPE_WORDS[shape] || shape)}</button>`);
  }
  if (pre.thinking) chips.push(`<span class="chip thinking"><span class="dot"></span>reading your question…</span>`);
  box.innerHTML = chips.join("");
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
          <span>${icon(s.icon)}${esc(s.source)} ${s.relevant ? `<span class="tag">suggested</span>` : ""}</span>
          ${s.probability === null || s.probability === undefined ? "<span></span>" : `<span class="rel" title="relevance ${Math.round(s.probability * 100)}%"><span style="width:${Math.round(s.probability * 100)}%"></span></span>`}
          ${s.description ? `<span class="desc">${esc(s.description)}</span>` : ""}</label>`).join("")).join("") +
      `<div class="foot"><button class="secondary" type="button" id="useall">Let it choose</button>
        <button class="primary" type="button" id="paneldone">Done</button></div>`;
    panel.querySelectorAll("[data-source]").forEach(cb => cb.addEventListener("change", () => {
      const set = new Set(pre.chosen || suggested());
      cb.checked ? set.add(cb.dataset.source) : set.delete(cb.dataset.source);
      pre.chosen = set; drawChips();
    }));
    panel.querySelectorAll("[data-system]").forEach(cb => { cb.indeterminate = cb.dataset.some === "1"; });
    panel.querySelectorAll("[data-only]").forEach(b => b.addEventListener("click", () => {
      pre.chosen = new Set(d.systems.find(g => g.system === b.dataset.only).sources.map(s => s.source)); drawChips();
    }));
    panel.querySelectorAll("[data-system]").forEach(cb => cb.addEventListener("change", () => {
      const set = new Set(pre.chosen || suggested());
      d.systems.find(g => g.system === cb.dataset.system).sources.forEach(s => cb.checked ? set.add(s.source) : set.delete(s.source));
      pre.chosen = set; drawChips();
    }));
    $("#useall").addEventListener("click", () => { pre.chosen = null; pre.open = null; drawChips(); });
  } else {
    const e = d.entity, desc = e.descriptions || {};
    panel.innerHTML = `<p class="muted small" style="margin:0 0 6px">What should the answer be about?</p>` +
      e.ranked.map(([name, p]) => `<label class="srcrow"><input type="radio" name="ent" value="${esc(name)}" ${(pre.entity || (e.sure ? e.choice : "")) === name ? "checked" : ""}>
        <span>${esc(name.replace(/_/g, " "))}</span>
        <span class="rel" title="${Math.round(p * 100)}%"><span style="width:${Math.round(p * 100)}%"></span></span>
        ${desc[name] ? `<span class="desc">${esc(desc[name])}</span>` : ""}</label>`).join("") +
      `<div class="foot"><button class="secondary" type="button" id="useall">Let it decide</button>
        <button class="primary" type="button" id="paneldone">Done</button></div>`;
    panel.querySelectorAll("input[name=ent]").forEach(r => r.addEventListener("change", () => { pre.entity = r.value; drawChips(); }));
    $("#useall").addEventListener("click", () => { pre.entity = null; pre.open = null; drawChips(); });
  }
  $("#paneldone").addEventListener("click", () => { pre.open = null; drawChips(); });
}

// ---- ask ---------------------------------------------------------------
$("#askform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#question").value.trim(); if (!q) return;
  clearTimeout(pre.timer); pre.ctrl?.abort(); pre.thinking = false; pre.open = null; drawChips();
  if (pre.chosen && pre.chosen.size === 0) { toast("Choose at least one table, or let it choose."); return; }
  const body = {question: q, user: user()};
  if (pre.chosen) body.only_sources = [...pre.chosen];
  if (pre.entity) body.entity = pre.entity;
  $("#conversation").innerHTML = `<div class="card muted">Thinking…</div>`;
  try { render(await api("/api/ask", body)); }
  catch (err) { $("#conversation").innerHTML = `<div class="card error">${esc(err.message)}</div>`; }
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
                     catalog: "what data there is"};

function render(c) {
  const r = c.result, box = $("#conversation");
  let html = `<div class="card"><div class="q">${esc(r.question)}</div>`;
  if (r.intent && r.intent.english_question) html += `<div class="muted small">read as: ${esc(r.intent.english_question)}</div>`;
  if (c.history.length) html += `<div class="thread">${c.history.map(h =>
      `<div class="turn">Q: ${esc(h.asked)}<br>A: ${esc(h.reply)}${h.understood ? "" : " <em>(not understood)</em>"}</div>`).join("")}</div>`;
  html += `</div>`;
  if (r.status === "needs_clarification" && r.followup && r.followup.options.length) {
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
      <span class="muted small">${n} row${n === 1 ? "" : "s"}${c.truncated ? " (first 500 shown)" : ""}</span></div>
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
  $("#freeform")?.addEventListener("submit", (e) => { e.preventDefault(); const t = $("#freetext").value.trim(); if (t) reply(c.conversation_id, t); });
  wireFeedback(r);
}

// ---- feedback ----------------------------------------------------------
function options(obj, blank) { return (blank ? `<option value="">${blank}</option>` : "") +
  Object.entries(obj).map(([k, v]) => `<option value="${esc(k)}" title="${esc(v)}">${esc(k)}</option>`).join(""); }

function feedbackForm(r) {
  const m = META || {categories: {}, sources: {}, answer_shapes: {}, entities: {}, values: {}};
  return `<div class="card feedback" id="fb"><h3>Did this answer your question?</h3>
    <div class="row">
      <button class="verdict" data-v="answered">✓ Yes</button>
      <button class="verdict" data-v="partial">◐ Partly</button>
      <button class="verdict" data-v="not_answered">✗ No</button>
    </div>
    <div id="fbmore" hidden>
      <p class="muted small" style="margin:12px 0 4px">What went wrong?</p>
      <div class="grid2">${Object.entries(m.categories).map(([k, v]) =>
        `<label class="check"><input type="checkbox" value="${esc(k)}"> ${esc(v)}</label>`).join("")}</div>
      <p class="muted small" style="margin:12px 0 4px">Why? (your words)</p>
      <textarea id="fbreason" placeholder="e.g. I wanted how many, not the list"></textarea>
      <details><summary>What would have been right? (optional — this is what the system learns from)</summary>
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
    </div>
    <div class="row" style="margin-top:12px"><button class="primary" id="fbsend" disabled>Send feedback</button>
      <span class="muted small" id="fbdone"></span></div></div>`;
}

function wireFeedback(r) {
  const fb = $("#fb"); if (!fb) return;
  let verdict = null;
  fb.querySelectorAll("button.verdict").forEach(b => b.addEventListener("click", () => {
    verdict = b.dataset.v;
    fb.querySelectorAll("button.verdict").forEach(x => x.setAttribute("aria-pressed", x === b));
    $("#fbmore").hidden = verdict === "answered"; $("#fbsend").disabled = false;
  }));
  $("#fbfield")?.addEventListener("change", () => {
    const vals = (META.values[$("#fbfield").value] || []);
    $("#fbvalue").innerHTML = `<option value="">value</option>` + vals.map(v => `<option>${esc(v)}</option>`).join("");
  });
  $("#fbsend").addEventListener("click", async () => {
    const expected = {};
    const srcs = [...($("#fbsources")?.selectedOptions || [])].map(o => o.value);
    if (srcs.length) expected.sources = srcs;
    if ($("#fbshape")?.value) expected.answer_shape = $("#fbshape").value;
    if ($("#fbentity")?.value) expected.entity = $("#fbentity").value;
    if ($("#fbword")?.value.trim() && $("#fbfield")?.value && $("#fbvalue")?.value)
      expected.synonym = {word: $("#fbword").value.trim(), field: $("#fbfield").value, value: $("#fbvalue").value};
    const body = {search_id: r.search_id, verdict, user: user(),
      categories: verdict === "answered" ? [] : [...fb.querySelectorAll("#fbmore input[type=checkbox]:checked")].map(i => i.value),
      reason: verdict === "answered" ? "" : $("#fbreason").value, expected: verdict === "answered" ? null : expected};
    try { await api("/api/feedback", body); $("#fbsend").disabled = true; $("#fbdone").textContent = "Thanks — recorded."; }
    catch (err) { toast(err.message); }
  });
}

// ---- history -----------------------------------------------------------
function verdictBadge(v) {
  if (!v) return `<span class="muted small">not rated</span>`;
  const m = {answered: ["good", "✓", "answered"], partial: ["mid", "◐", "partly"], not_answered: ["bad", "✗", "not answered"]}[v];
  return `<span class="status ${m[0]}"><i aria-hidden="true">${m[1]}</i>${m[2]}</span>`;
}
async function loadHistory() {
  try {
    const rows = await api("/api/searches?limit=200");
    $("#history").innerHTML = rows.length ? `<div class="tablewrap"><table><thead><tr><th>When</th><th>Question</th><th>Result</th><th>Answer</th><th>Tables</th><th>Feedback</th></tr></thead><tbody>${
      rows.map(r => `<tr><td class="small muted">${esc((r.created_at || "").replace("T", " ").slice(0, 16))}</td>
        <td>${esc(r.question)}${r.user_name ? `<div class="muted small">${esc(r.user_name)}</div>` : ""}</td>
        <td class="small">${esc(r.status)}</td><td class="small">${esc(r.answer_shape || "")}</td>
        <td class="small">${esc((JSON.parse(r.sources || "[]")).join(", "))}</td>
        <td>${verdictBadge(r.verdict)}${r.reason ? `<div class="muted small">${esc(r.reason)}</div>` : ""}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="muted">No questions asked yet.</p>`;
  } catch (err) { $("#history").innerHTML = `<p class="error">${esc(err.message)}</p>`; }
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
$("#evalbtn").addEventListener("click", async () => {
  $("#evaluation").innerHTML = `<p class="muted">Evaluating…</p>`;
  try {
    const e = await api("/api/evaluate", {});
    if (!e.size) { $("#evaluation").innerHTML = `<p class="muted">${esc(e.note)}</p>`; return; }
    $("#evaluation").innerHTML = `<p class="small muted">${e.size} rated questions.</p>
      ${table(Object.entries(e.metrics).filter(([k]) => k.endsWith("accuracy") && k !== "answer_accuracy").map(([k, v]) => ({metric: k.replace(/_/g, " "), value: v === null ? null : Math.round(v * 1000) / 1000})))}
      ${e.misses.length ? `<details><summary>${e.misses.length} question(s) it still gets wrong</summary>${table(e.misses)}</details>` : ""}
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
  files: "local files", python: "Python module", duckdb: "DuckDB", api: "API"};
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

// ---- Config tab --------------------------------------------------------------------------
let CFG = null;
async function loadConfig() {
  try {
    CFG = await api("/api/config");
    $("#cfgpath").textContent = CFG.path || "";
    $("#cfgtext").value = JSON.stringify(CFG.config, null, 2);
    $("#cfgnote").textContent = (CFG.editable ? "Secrets show as \"***\" — leave them to keep the saved value, or type a new one. Saving keeps a .bak and reconnects everything."
      : "Read-only here: start the server with --edit-config to save. Secrets show as \"***\".");
    $("#cfgsave").disabled = !CFG.editable;
    $("#cfgmsgs").innerHTML = "";
    drawReference();
  } catch (err) { $("#cfgmsgs").innerHTML = `<p class="msg bad">${esc(err.message)}</p>`; }
}
function parsedConfig() {
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
  document.querySelector('nav button[data-tab="sql"]').hidden = !META?.features?.sql;
  document.querySelector('nav button[data-tab="config"]').hidden = !META?.features?.config;
}
api("/api/meta").then(m => { META = m; showFeatures(); }).catch(err => toast(err.message));
</script>
</body>
</html>
"""

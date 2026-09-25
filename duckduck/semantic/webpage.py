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
.toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: var(--ink);
         color: var(--page); padding: 8px 14px; border-radius: 8px; font-size: 14px; opacity: 0;
         transition: opacity .2s; pointer-events: none; }
.toast.show { opacity: 1; }
.error { color: var(--bad); }
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
    </div>
    <div id="conversation"></div>
  </section>
  <section id="tab-history" hidden><div class="card"><div id="history"></div></div></section>
  <section id="tab-dashboard" hidden><div id="dashboard"></div></section>
  <section id="tab-suggestions" hidden>
    <div class="card">
      <h3>Evaluation</h3>
      <p class="muted small">Replays every rated question (planning only, no data read) and suggests decision
        thresholds from what users said. <a href="#" id="evaljson">Download the evaluation set</a>.</p>
      <button class="secondary" id="evalbtn">Evaluate and calibrate</button>
      <div id="evaluation"></div>
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

async function api(path, body) {
  const headers = {"Content-Type": "application/json"};
  const token = store.get("duckduck-token"); if (token) headers["X-Duckduck-Token"] = token;
  const r = await fetch(path, body === undefined ? {headers} : {method: "POST", headers, body: JSON.stringify(body)});
  if (r.status === 401) {
    const t = prompt("This server needs its access token:");
    if (t) { store.set("duckduck-token", t); return api(path, body); }
  }
  const data = await r.json().catch(() => ({}));
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
  ({history: loadHistory, dashboard: loadDashboard, suggestions: loadSuggestions})[b.dataset.tab]?.();
}));
$("#user").value = store.get("duckduck-user") || "";
$("#user").addEventListener("change", () => store.set("duckduck-user", $("#user").value));

// ---- ask ---------------------------------------------------------------
$("#askform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#question").value.trim(); if (!q) return;
  $("#conversation").innerHTML = `<div class="card muted">Thinking…</div>`;
  try { render(await api("/api/ask", {question: q, user: user()})); }
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
      ${tables.map(s => `<span class="pill">${esc(s)}</span>`).join("")}
      <span class="muted small">${n} row${n === 1 ? "" : "s"}${c.truncated ? " (first 500 shown)" : ""}</span></div>
      ${table(r.results)}
      ${r.summary ? `<details><summary>Where it was looked for</summary>${table(r.summary)}</details>` : ""}
      ${(r.sections || []).filter(s => s.results && s.results.length).map(s =>
          `<details><summary>${esc(s.source)} — ${s.rows} row${s.rows === 1 ? "" : "s"}</summary>${table(s.results)}</details>`).join("")}
      ${r.sql ? `<details><summary>SQL</summary><pre>${esc(r.sql)}</pre></details>` : ""}
      <details><summary>How it was decided</summary>${table((r.decisions || []).map(d => ({
          decision: d.kind, about: d.subject, answer: typeof d.answer === "object" ? JSON.stringify(d.answer) : d.answer,
          probability: Math.round(d.probability * 100) / 100, by: d.decided_by})))}</details>
      ${(r.intent && r.intent.similar_cases && r.intent.similar_cases.length) ? `<details><summary>Similar questions confirmed before</summary>${table(r.intent.similar_cases.map(x => ({question: x.question, similarity: x.similarity, kind: x.kind, answer: x.answer_shape, tables: (x.sources || []).join(", ")})))}</details>` : ""}
    </div>`;
  }
  if (c.done && r.search_id) html += feedbackForm(r);
  box.innerHTML = html;
  box.querySelectorAll("button.option").forEach(b => b.addEventListener("click", () => reply(c.conversation_id, b.dataset.reply)));
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
$("#evaljson").addEventListener("click", async (e) => {
  e.preventDefault();
  const headers = {}; const t = store.get("duckduck-token"); if (t) headers["X-Duckduck-Token"] = t;
  const r = await fetch("/api/evaluation.json", {headers}); const blob = await r.blob();
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "feedback_evaluation.json"; a.click();
});

api("/api/meta").then(m => { META = m; }).catch(err => toast(err.message));
</script>
</body>
</html>
"""

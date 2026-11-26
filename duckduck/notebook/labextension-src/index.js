// duckduck's bridge into JupyterLab (the web app's Notebook tab embeds JupyterLab in an iframe).
//
// The page posts, from the same origin and only as the iframe's parent:
//   {type: 'duckduck:insert', id, code, sql?} → a code cell below the active one (the active cell itself when it's an
//                                          empty code cell; after the last cell when the active one is the intro
//                                          or a session-setup cell), scrolled into view; never run
//   {type: 'duckduck:open', id, path}    → opens that notebook
// and gets back {type: 'duckduck:inserted' | 'duckduck:opened', id, ok, path, error}. JupyterLab tells the page
// {type: 'duckduck:ready'} once restored and {type: 'duckduck:current', path, kernel} when the notebook changes.
//
// The notebook toolbar gets a PySpark / SQL / SQL · duckduck switch for the active cell (convert() in toggle.js)
// and, for an SQL cell, the name of the DataFrame it leaves. An SQL cell holds only its SQL; the cell's metadata
// ("duckduck": {language: "sql", engine, name}) makes it SQL, and the duckduck kernel runs it as
// name = spark.sql(...). SQL cells are highlighted as SQL and marked "SQL → name". A table clicked in the page
// arrives as SQL ({sql}) when the cell it goes to is an SQL cell, or the switch was last set to SQL.
import { autocompletion, moveCompletionSelection } from '@codemirror/autocomplete';
import { Prec } from '@codemirror/state';
import { keymap } from '@codemirror/view';
import { EditorExtensionRegistry, IEditorExtensionRegistry } from '@jupyterlab/codemirror';
import { INotebookTracker, NotebookActions } from '@jupyterlab/notebook';
import { LabIcon } from '@jupyterlab/ui-components';
import { Widget } from '@lumino/widgets';
import { KEY, NAME, autoNames, convert, isAuto, modeOf, nameOf } from './toggle';

// the cell-language switch differs per kernel. The local DuckDB kernel is plain Python + DuckDB SQL (pandas);
// the Glue kernels have PySpark, Spark SQL, and duckduck (reader.sql). modesFor(panel) picks the set.
const GLUE_MODES = [
  ['python', 'PySpark', 'Python code, run in the session'],
  ['sql', 'SQL', 'Spark SQL in the session, the result kept as a DataFrame (df = spark.sql(...))'],
  ['duckduck', 'SQL · duckduck', 'read through duckduck (reader.sql), the result kept as a DataFrame']
];
const LOCAL_MODES = [
  ['python', 'Python', 'Python with pandas — an SQL cell becomes name = duck.sql(...).df()'],
  ['duckdb', 'SQL', 'DuckDB SQL (duck.sql), the result kept as a pandas DataFrame — like the SQL tab']
];
const ALL_MODES = ['python', 'sql', 'duckduck', 'duckdb'];

function kernelName(panel) {
  const session = panel && panel.sessionContext && panel.sessionContext.session;
  return (session && session.kernel && session.kernel.name) || '';
}
// the local DuckDB kernel (not duckduck_local_pyspark, which is Spark)
function isLocalDuck(panel) {
  return kernelName(panel) === 'duckduck_local';
}
function modesFor(panel) {
  return isLocalDuck(panel) ? LOCAL_MODES : GLUE_MODES;
}
// map a mode to one this kernel offers (a notebook's kept choice, or a clicked table's engine, may be the other set)
function modeForPanel(panel, mode) {
  if (isLocalDuck(panel)) {
    return mode === 'sql' || mode === 'duckduck' ? 'duckdb' : mode;
  }
  return mode === 'duckdb' ? 'duckduck' : mode;
}
// SQL cells in their own colour (amber, readable in light and dark themes): a tinted editor, an amber edge, and
// "SQL → df" by the prompt; the change animates when the switch turns a cell into SQL
const STYLE = `
.jp-CodeCell.duckduck-sql { --duckduck-sql: #d97706; --duckduck-sql-bg: rgba(245, 158, 11, 0.10); }
[data-jp-theme-light='false'] .jp-CodeCell.duckduck-sql { --duckduck-sql: #fbbf24; --duckduck-sql-bg: rgba(251, 191, 36, 0.12); }
.jp-CodeCell.duckduck-sql .jp-InputArea-editor { background: var(--duckduck-sql-bg) !important;
  border: 1px solid var(--duckduck-sql) !important; border-left-width: 4px !important;
  transition: background-color 0.25s ease, border-color 0.25s ease; }
.jp-CodeCell.duckduck-sql .jp-InputArea-editor .cm-editor,
.jp-CodeCell.duckduck-sql .jp-InputArea-editor .cm-gutters { background: transparent !important; }
.jp-CodeCell.duckduck-sql .jp-InputPrompt { color: var(--duckduck-sql); }
.jp-CodeCell.duckduck-sql .jp-InputPrompt::after { content: 'SQL → ' attr(data-duckduck-df); display: block;
  font-size: 9px; color: var(--duckduck-sql); font-weight: 700; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.jp-CodeCell .jp-InputArea-editor { transition: background-color 0.25s ease, border-color 0.25s ease; }
.duckduck-langswitch select.duckduck-on { border-color: #d97706; color: #d97706; font-weight: 600; }
.duckduck-langswitch { display: flex; align-items: center; gap: 4px; }
.duckduck-langswitch select { height: 24px; }
.duckduck-langswitch input { width: 90px; height: 20px; font-family: var(--jp-code-font-family); font-size: 12px; }
.duckduck-langswitch .duckduck-arrow { color: var(--jp-ui-font-color2); font-size: 12px; }
.duckduck-tables-panel { display: flex; flex-direction: column; min-width: 240px; overflow: hidden;
  background: var(--jp-layout-color1); color: var(--jp-ui-font-color1); }
.duckduck-tp-head { font-size: var(--jp-ui-font-size0); font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--jp-ui-font-color2); padding: 8px 12px 4px; }
.duckduck-tp-search { margin: 0 10px 6px; padding: 4px 6px; font-size: var(--jp-ui-font-size1);
  background: var(--jp-input-background); color: var(--jp-ui-font-color1);
  border: 1px solid var(--jp-border-color2); border-radius: 3px; }
.duckduck-tp-search:focus { outline: none; border-color: var(--jp-brand-color1); }
.duckduck-tp-empty { padding: 8px 12px; color: var(--jp-ui-font-color2); font-size: var(--jp-ui-font-size0); }
.duckduck-tp-new { margin: 0 10px 6px; padding: 4px 8px; font-size: var(--jp-ui-font-size1); cursor: pointer;
  background: var(--jp-layout-color1); color: var(--jp-ui-font-color1); border: 1px solid var(--jp-border-color2);
  border-radius: 3px; text-align: left; }
.duckduck-tp-new:hover { border-color: var(--jp-brand-color1); color: var(--jp-brand-color1); }
.duckduck-tp-list { flex: 1; overflow: auto; padding-bottom: 8px; }
.duckduck-tp-group { display: flex; align-items: center; gap: 6px; width: 100%; border: 0; cursor: pointer;
  padding: 5px 10px 3px; font-size: var(--jp-ui-font-size0); font-weight: 600; text-align: left;
  color: var(--jp-ui-font-color2); position: sticky; top: 0; background: var(--jp-layout-color1); }
.duckduck-tp-group:hover { color: var(--jp-ui-font-color1); }
.duckduck-tp-caret { width: 10px; font-size: 9px; color: var(--jp-ui-font-color2); }
.duckduck-tp-svc { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.duckduck-tp-count { font-weight: 400; color: var(--jp-ui-font-color3); background: var(--jp-layout-color2);
  border-radius: 8px; padding: 0 6px; font-size: var(--jp-ui-font-size0); }
.duckduck-tp-row { display: block; width: 100%; text-align: left; border: 0; background: transparent;
  color: var(--jp-ui-font-color1); font-size: var(--jp-ui-font-size1); padding: 3px 12px 3px 26px; cursor: pointer;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.duckduck-tp-row::before { content: '▦'; margin-right: 6px; color: var(--jp-brand-color1); font-size: 11px; opacity: .75; }
.duckduck-tp-row:hover { background: var(--jp-layout-color2); }
.duckduck-tp-kind { color: var(--jp-ui-font-color3); font-size: var(--jp-ui-font-size0); }
`;

function addStyle() {
  if (document.getElementById('duckduck-notebook-style')) {
    return;
  }
  const style = document.createElement('style');
  style.id = 'duckduck-notebook-style';
  style.textContent = STYLE;
  document.head.appendChild(style);
}

// the "Tables" panel in JupyterLab's own left sidebar — the duckduck catalog, inside the IDE instead of a second
// list beside it. The page (window.parent) sends the tables (duckduck:tables) and inserts the one clicked
// (duckduck:use-table → the page's notebookInsert); works only embedded in the duckduck app (it has the catalog).
const tablesIcon = new LabIcon({
  name: 'duckduck:tables',
  // a database cylinder (three stacked discs), so the sidebar tab doesn't read as the Table of Contents icon
  svgstr: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
    + '<g class="jp-icon3" fill="#616161">'
    + '<path d="M8 1.5c-2.76 0-5 .95-5 2.12S5.24 5.75 8 5.75s5-.96 5-2.13S10.76 1.5 8 1.5Z"/>'
    + '<path d="M3 6.1v2.4c0 1.17 2.24 2.12 5 2.12s5-.95 5-2.12V6.1c-1.06.82-3.05 1.15-5 1.15S4.06 6.92 3 6.1Z"/>'
    + '<path d="M3 10.1v2.3c0 1.17 2.24 2.1 5 2.1s5-.93 5-2.1v-2.3c-1.06.82-3.05 1.15-5 1.15S4.06 10.92 3 10.1Z"/>'
    + '</g></svg>'
});

function escText(s) {
  return String(s == null ? '' : s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}
function escAttr(s) {
  return escText(s).replace(/"/g, '&quot;');
}

// embedded in the duckduck app's iframe (there's a parent window) vs. a standalone JupyterLab window
const EMBEDDED = typeof window !== 'undefined' && window.parent && window.parent !== window;
let gApp = null;  // the JupyterLab app, for inserting a cell locally in a standalone window

// a /api/tables row → the shape the panel shows (same flattening the page's nbTables() does)
function flattenTables(rows) {
  return (rows || []).map(t => ({
    name: t.saved_name || t.name,
    label: t.saved_name || t.address || t.name,
    kind: t.kind || 'table',
    service: t.service || 'other',
    saved: !!t.saved,
    sql: t.usage || ('SELECT * FROM ' + (t.saved_name || t.name) + ' LIMIT 100')
  }));
}

// a clicked table: embedded → ask the page to insert it; standalone → fetch the cell code and insert it here
function useTable(sql) {
  if (EMBEDDED) {
    post({ type: 'duckduck:use-table', sql });
    return;
  }
  fetch('/api/notebook/code', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sql })
  })
    .then(r => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
    .then(res => { if (gApp && gTracker) { insert(gApp, gTracker, res.code, res.sql); } })
    .catch(() => {});
}

class TablesPanel extends Widget {
  constructor() {
    super();
    this.id = 'duckduck-tables';
    this.title.caption = 'duckduck — tables';
    this.title.icon = tablesIcon;
    this.addClass('duckduck-tables-panel');
    this.tables = [];
    this.filter = '';
    this.collapsed = new Set();  // services folded shut (by name)
    const head = document.createElement('div');
    head.className = 'duckduck-tp-head';
    head.textContent = 'Tables';
    const search = document.createElement('input');
    search.type = 'search';
    search.placeholder = 'Filter tables';
    search.className = 'duckduck-tp-search';
    search.addEventListener('input', () => {
      this.filter = search.value.trim().toLowerCase();
      this.renderList();
    });
    const newBtn = document.createElement('button');
    newBtn.type = 'button';
    newBtn.className = 'duckduck-tp-new';
    newBtn.textContent = '＋ New table';
    newBtn.title = 'Build a new table in a grid and insert it as a DataFrame';
    newBtn.addEventListener('click', () => openGridEditor({
      title: 'New table', columns: ['col1', 'col2', 'col3'], rows: [], suggestName: 'new_table'
    }));
    this.empty = document.createElement('div');
    this.empty.className = 'duckduck-tp-empty';
    this.empty.textContent = 'Loading tables…';
    this.list = document.createElement('div');
    this.list.className = 'duckduck-tp-list';
    this.list.addEventListener('click', event => {
      const group = event.target.closest('[data-group]');
      if (group) {
        const svc = group.getAttribute('data-group');
        if (this.collapsed.has(svc)) {
          this.collapsed.delete(svc);
        } else {
          this.collapsed.add(svc);
        }
        this.renderList();
        return;
      }
      const row = event.target.closest('[data-sql]');
      if (row) {
        useTable(row.getAttribute('data-sql'));
      }
    });
    this.node.append(head, search, newBtn, this.empty, this.list);
    this.requestTables();
  }

  // embedded: the page pushes the catalog (duckduck:tables); standalone: fetch /api/tables directly (same origin)
  requestTables() {
    if (EMBEDDED) {
      post({ type: 'duckduck:tables?' });
      return;
    }
    fetch('/api/tables', { headers: { 'Content-Type': 'application/json' } })
      .then(r => (r.ok ? r.json() : Promise.reject(new Error(r.status === 401 ? 'token' : String(r.status)))))
      .then(rows => this.setTables(flattenTables(rows)))
      .catch(err => {
        this.empty.textContent = err && err.message === 'token'
          ? 'This server needs a token — open the Notebook tab in the duckduck app to see your tables.'
          : 'Couldn’t load tables here — open the Notebook tab in the duckduck app.';
        this.setTables([]);
      });
  }

  setTables(tables) {
    this.tables = Array.isArray(tables) ? tables : [];
    this.renderList();
  }

  // a plain registered name is "<service>_<table>"; the group header already shows the service, so drop the prefix
  shortLabel(t) {
    const label = t.label || t.name || '';
    const svc = t.service || '';
    if (svc && label.toLowerCase().startsWith(svc.toLowerCase() + '_')) {
      return label.slice(svc.length + 1);
    }
    return label;
  }

  renderList() {
    this.empty.style.display = this.tables.length ? 'none' : '';
    const f = this.filter;
    const shown = this.tables.filter(
      t => !f || `${t.name || ''} ${t.label || ''} ${t.service || ''}`.toLowerCase().includes(f)
    );
    const groups = {};
    shown.forEach(t => {
      (groups[t.service || 'other'] = groups[t.service || 'other'] || []).push(t);
    });
    const html = Object.keys(groups)
      .sort()
      .map(svc => {
        const open = !!f || !this.collapsed.has(svc);  // a filter force-opens every matching group
        const rows = open
          ? groups[svc]
              .map(t =>
                `<button type="button" class="duckduck-tp-row" data-sql="${escAttr(t.sql)}" ` +
                `title="${escAttr(t.sql)}">${escText(this.shortLabel(t))}` +
                (t.kind && t.kind !== 'table' ? ` <span class="duckduck-tp-kind">${escText(t.kind)}</span>` : '') +
                '</button>'
              )
              .join('')
          : '';
        return (
          `<button type="button" class="duckduck-tp-group${open ? '' : ' collapsed'}" ` +
          `data-group="${escAttr(svc)}" aria-expanded="${open}">` +
          `<span class="duckduck-tp-caret">${open ? '▾' : '▸'}</span>` +
          `<span class="duckduck-tp-svc">${escText(svc)}</span>` +
          `<span class="duckduck-tp-count">${groups[svc].length}</span></button>` +
          rows
        );
      })
      .join('');
    this.list.innerHTML = html || (f && this.tables.length ? '<div class="duckduck-tp-empty">Nothing matches.</div>' : '');
  }

  onAfterShow() {
    this.requestTables();  // in case the catalog changed while the panel was hidden
  }
}

// ---- inline autocomplete in SQL cells: the DataFrames the notebook's SQL cells leave + the catalog tables -------
// A CodeMirror 6 completion source, scoped to SQL cells (model mimeType text/x-sql). It reads live: the other SQL
// cells' DataFrame names (their metadata) and the Tables panel's catalog, so `FROM `/`JOIN ` offers them; and the
// columns of the tables this query reads — learned from the SQL cells' result outputs, which the kernel tags with
// each result's columns (so a column shows up once its cell has run, the same as the SQL tab's column memory).
let gTracker = null;  // the notebook tracker, for the current notebook's SQL-cell names
let gPanel = null;    // the Tables panel, for the catalog names

const SQL_KEYWORDS = ['SELECT', 'FROM', 'WHERE', 'JOIN', 'LEFT JOIN', 'INNER JOIN', 'GROUP BY', 'ORDER BY',
  'LIMIT', 'UNION ALL', 'UNION', 'ON', 'USING', 'AS', 'AND', 'OR', 'NOT', 'IN', 'DISTINCT', 'COUNT(*)',
  'HAVING', 'WITH'];

// a top-level `name = <rhs>` in a Python cell whose rhs looks like it yields a DataFrame — so a df built in a
// Python cell (duck.sql(...).df(), pd.DataFrame(...), .toPandas()…) is offered too, not only SQL cells' DataFrames
const PY_ASSIGN = /^([A-Za-z_]\w*)\s*=\s*(.*)$/;
const PY_DATAFRAME = /\b(?:duck|reader|spark)\s*\.\s*sql\(|\.df\(\)|\.toPandas\(\)|\b(?:pd|pandas)\s*\.\s*(?:DataFrame|read_\w+|concat|merge|json_normalize)\b/;

function pythonDataFrames(seen, items) {
  const panel = gTracker && gTracker.currentWidget;
  if (!panel || !panel.content) {
    return;
  }
  panel.content.widgets.forEach(cell => {
    if (!cell.model || cell.model.type !== 'code') {
      return;
    }
    const meta = cell.model.getMetadata ? cell.model.getMetadata(KEY) : undefined;
    if (meta && meta.language === 'sql') {
      return;  // SQL cells are handled by their metadata
    }
    cell.model.sharedModel.getSource().split('\n').forEach(line => {
      const m = PY_ASSIGN.exec(line);  // top-level (no indent): a module-level name, not a local inside a def
      if (m && !m[1].startsWith('_') && !seen.has(m[1]) && PY_DATAFRAME.test(m[2])) {
        seen.add(m[1]);
        items.push({ label: m[1], type: 'variable', detail: 'DataFrame', boost: 2 });
      }
    });
  });
}

// the names you can read in an SQL cell now: the DataFrames the notebook's SQL cells leave (metadata) and the ones
// Python cells build, then the catalog tables
function sqlNames() {
  const items = [];
  const seen = new Set();
  const panel = gTracker && gTracker.currentWidget;
  if (panel && panel.content) {
    panel.content.widgets.forEach(cell => {
      if (!cell.model || cell.model.type !== 'code') {
        return;
      }
      const meta = cell.model.getMetadata ? cell.model.getMetadata(KEY) : undefined;
      if (meta && meta.language === 'sql' && meta.name && !seen.has(meta.name)) {
        seen.add(meta.name);
        items.push({ label: meta.name, type: 'variable', detail: 'DataFrame', boost: 2 });
      }
    });
  }
  pythonDataFrames(seen, items);
  if (gPanel && Array.isArray(gPanel.tables)) {
    gPanel.tables.forEach(t => {
      const label = t.label || t.name;
      if (label && !seen.has(label)) {
        seen.add(label);
        items.push({ label, type: 'class', detail: t.service || 'table', boost: 1 });
      }
    });
  }
  return items;
}

// a SQL string's single-quoted literals and comments blanked (same length), so a word inside one isn't matched
// (double-quoted names are kept — in DuckDB "My Table" is an identifier, which FROM/JOIN may name)
function maskSql(s) {
  return (s || '')
    .replace(/'(?:[^']|'')*'/g, m => ' '.repeat(m.length))
    .replace(/--[^\n]*/g, m => ' '.repeat(m.length))
    .replace(/\/\*[\s\S]*?\*\//g, m => ' '.repeat(m.length));
}

// {DataFrame name: [columns]} learned from the notebook's SQL-cell outputs — the kernel tags each result's
// display_data with its columns, so columns appear once a cell has run (like the SQL tab's own column memory)
function sqlColumns() {
  const map = {};
  const panel = gTracker && gTracker.currentWidget;
  if (!panel || !panel.content) {
    return map;
  }
  panel.content.widgets.forEach(cell => {
    if (!cell.model || cell.model.type !== 'code') {
      return;
    }
    const outputs = cell.model.outputs;
    const meta = cell.model.getMetadata ? cell.model.getMetadata(KEY) : undefined;
    for (let i = 0; outputs && i < outputs.length; i++) {
      const out = outputs.get(i);
      const dd = out && out.metadata && out.metadata[KEY];
      if (dd && Array.isArray(dd.columns) && dd.columns.length) {
        const name = dd.name || (meta && meta.name);
        if (name) {
          map[name] = dd.columns.slice();
        }
      }
    }
  });
  return map;
}

// aliases that are really keywords, never the name after a table (FROM t WHERE …)
const ALIAS_STOP = new Set(['where', 'on', 'using', 'group', 'order', 'limit', 'having', 'join', 'inner', 'left',
  'right', 'full', 'cross', 'natural', 'union', 'and', 'or', 'select', 'as', 'offset', 'window', 'qualify']);
const FROM_REF = new RegExp(
  '\\b(?:from|join)\\s+' +
  '("[^"]+"|`[^`]+`|[A-Za-z_][\\w$]*(?:\\s*\\.\\s*(?:"[^"]+"|`[^`]+`|[A-Za-z_][\\w$]*))*)' +  // the table
  '(?:\\s+(?:as\\s+)?("[^"]+"|`[^`]+`|[A-Za-z_]\\w*))?',                                       // an optional alias
  'gi');

// the tables/DataFrames an SQL query reads → {alias or name (lowercased): the name}. `FROM a.b c` → the last part
function refMap(query) {
  const masked = maskSql(query);
  const map = {};
  let m;
  FROM_REF.lastIndex = 0;
  while ((m = FROM_REF.exec(masked))) {
    const parts = m[1].split('.').map(p => p.trim().replace(/^["`]|["`]$/g, ''));
    const name = parts[parts.length - 1];
    if (!name) {
      continue;
    }
    map[name.toLowerCase()] = name;
    if (m[2]) {
      const alias = m[2].replace(/^["`]|["`]$/g, '');
      if (!ALIAS_STOP.has(alias.toLowerCase())) {
        map[alias.toLowerCase()] = name;
      }
    }
  }
  return map;
}

// a name's columns in the learned map, matched case-insensitively
function columnsOf(name, cols) {
  if (cols[name]) {
    return cols[name];
  }
  const lc = name.toLowerCase();
  const key = Object.keys(cols).find(k => k.toLowerCase() === lc);
  return key ? cols[key] : null;
}

// the columns for a reference used in the query (an alias or a table name)
function columnsForRef(ref, query) {
  return columnsOf(refMap(query)[ref.toLowerCase()] || ref, sqlColumns());
}

// every column of the tables this query reads (that we've learned)
function queryColumns(query) {
  const cols = sqlColumns();
  const out = [];
  const seen = new Set();
  new Set(Object.values(refMap(query))).forEach(name => {
    (columnsOf(name, cols) || []).forEach(c => {
      if (!seen.has(c)) {
        seen.add(c);
        out.push(c);
      }
    });
  });
  return out;
}

// the completion source for one editor (its model is captured so we only fire on SQL cells)
function sqlCompletionSource(model) {
  return context => {
    if (!model || model.mimeType !== 'text/x-sql') {
      return null;  // only SQL cells
    }
    const word = context.matchBefore(/[\w]+/);
    const before = context.state.sliceDoc(Math.max(0, context.pos - 200), context.pos);
    // `alias.`/`name.` → that table's columns (only; an unresolved dotted name offers nothing, rather than table
    // names that would double the prefix into security.security.proxy_logs)
    const dot = /([\w"`]+)\s*\.\s*[\w]*$/.exec(before);
    if (dot) {
      const cols = columnsForRef(dot[1].replace(/["`]/g, ''), context.state.doc.toString());
      if (!cols || !cols.length) {
        return null;
      }
      return {
        from: word ? word.from : context.pos,
        options: cols.map(c => ({ label: c, type: 'property', detail: 'column' })),
        validFor: /^[\w]*$/
      };
    }
    const afterRef = /(?:\bfrom\b|\bjoin\b|,)\s*[\w]*$/i.test(before);  // a table position
    if (!context.explicit && !afterRef && (!word || word.from === word.to)) {
      return null;
    }
    const names = sqlNames();
    // after FROM/JOIN only names; elsewhere names + the columns of the tables this query reads + keywords
    const options = afterRef
      ? names
      : names
          .concat(queryColumns(context.state.doc.toString())
            .map(c => ({ label: c, type: 'property', detail: 'column', boost: 1 })))
          .concat(SQL_KEYWORDS.map(k => ({ label: k, type: 'keyword' })));
    if (!options.length) {
      return null;
    }
    return { from: word ? word.from : context.pos, options, validFor: /^[\w]*$/ };
  };
}

function metaOf(cell) {
  return cell.model.getMetadata ? cell.model.getMetadata(KEY) : undefined;
}

// a cell's source and metadata replaced together
function setCell(cell, result) {
  const model = cell.model;
  model.sharedModel.transact(() => {
    if (result.meta) {
      model.setMetadata(KEY, result.meta);
    } else if (model.getMetadata(KEY) !== undefined) {
      model.deleteMetadata(KEY);
    }
    if (model.sharedModel.getSource() !== result.source) {
      model.sharedModel.setSource(result.source);
    }
  });
}

// automatic DataFrame names (toggle.js autoNames): kept in each SQL cell's metadata, which the kernel reads
function nameCells(notebook) {
  const cells = notebook.widgets.filter(cell => cell.model.type === 'code' && modeOf(cell.model.sharedModel.getSource(),
    metaOf(cell)) !== 'python' && metaOf(cell));
  const names = autoNames(cells.map(cell => ({ source: cell.model.sharedModel.getSource(), meta: metaOf(cell) })));
  cells.forEach((cell, i) => {
    const meta = metaOf(cell);
    if (isAuto(meta) && (meta.name !== names[i] || meta.auto !== true)) {
      cell.model.setMetadata(KEY, { ...meta, name: names[i], auto: true });
    }
  });
}

// SQL cells: their names, a class, the DataFrame's name by the prompt, SQL highlighting (the notebook may set the
// kernel's mime type back: done again on change)
function decorate(panel) {
  const notebook = panel.content;
  const python = notebook.codeMimetype || 'text/x-ipython';
  nameCells(notebook);
  notebook.widgets.forEach(cell => {
    if (cell.model.type !== 'code') {
      return;
    }
    const source = cell.model.sharedModel.getSource();
    const meta = metaOf(cell);
    const sql = modeOf(source, meta) !== 'python';
    cell.toggleClass('duckduck-sql', sql);
    const prompt = cell.node.querySelector('.jp-InputPrompt');
    if (prompt) {
      prompt.setAttribute('data-duckduck-df', sql ? nameOf(source, meta) : '');
    }
    const mime = sql ? 'text/x-sql' : python;
    if (cell.model.mimeType !== mime) {
      cell.model.mimeType = mime;
    }
  });
}

class LanguageSwitch extends Widget {
  constructor(panel) {
    const node = document.createElement('div');
    node.className = 'duckduck-langswitch';
    const select = document.createElement('select');
    select.className = 'jp-Notebook-toolbarCellTypeDropdown';
    select.title = 'The active cell: code or SQL (an SQL cell keeps its result as a DataFrame)';
    select.setAttribute('aria-label', 'Cell language');
    modesFor(panel).forEach(([value, label, title]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      option.title = title;
      select.appendChild(option);
    });
    const arrow = document.createElement('span');
    arrow.className = 'duckduck-arrow';
    arrow.textContent = '→';
    const name = document.createElement('input');
    name.type = 'text';
    name.placeholder = 'automatic';
    name.spellcheck = false;
    name.title = 'The DataFrame this SQL cell leaves, for the PySpark cells after it — named after its table '
      + 'until you type a name (empty: automatic again)';
    name.setAttribute('aria-label', 'DataFrame name');
    node.append(select, arrow, name);
    super({ node });
    this.select = select;
    this.name = name;
    this.arrow = arrow;
    this.panel = panel;
    this.addClass('jp-HTMLSelect');
    this.addClass('jp-DefaultStyle');
    select.addEventListener('change', () => this.apply(select.value));
    name.addEventListener('change', () => this.rename(name.value.trim()));
    name.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        name.blur();
      }
      event.stopPropagation(); // typing a name isn't a notebook shortcut
    });
    const notebook = panel.content;
    notebook.activeCellChanged.connect(this.refresh, this);
    let pending = null;
    notebook.modelContentChanged.connect(() => {
      clearTimeout(pending);
      pending = setTimeout(() => {
        this.refresh();
        decorate(panel);
      }, 150);
    });
    panel.context.ready.then(() => {
      // the language this notebook's new cells start in: the last one picked, kept in the notebook's metadata
      const kept = panel.model && panel.model.getMetadata ? panel.model.getMetadata(KEY) : undefined;
      if (kept && ALL_MODES.includes(kept.cell_language)) {
        panel.__duckduckMode = modeForPanel(panel, kept.cell_language);
      }
      panel.model.cells.changed.connect((_, change) => this.newCells(change));
      this.refresh();
      decorate(panel);
    });
    // the kernel decides which languages the switch offers; rebuild them (and the decoration) when it changes
    panel.sessionContext.kernelChanged.connect(() => setTimeout(() => {
      this.fillModes();
      this.refresh();
      decorate(panel);
    }, 500));
  }

  // (re)populate the dropdown from the kernel's language set
  fillModes() {
    const want = modesFor(this.panel);
    if (this.select.options.length === want.length
        && [...this.select.options].every((o, i) => o.value === want[i][0])) {
      return;  // already the right set
    }
    this.select.textContent = '';
    want.forEach(([value, label, title]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      option.title = title;
      this.select.appendChild(option);
    });
  }

  // a new, empty code cell starts in the language picked last (Shift+Enter at the end, b, the + button…)
  newCells(change) {
    const mode = this.panel.__duckduckMode;
    if (change.type !== 'add' || !mode || mode === 'python') {
      return;
    }
    const models = change.newValues || [];
    setTimeout(() => {  // after whatever added it has filled it (a clicked table, a paste): only empty ones
      let changed = false;
      models.forEach(model => {
        if (model.type !== 'code' || model.sharedModel.getSource().trim() || model.getMetadata(KEY) !== undefined) {
          return;
        }
        model.setMetadata(KEY, convert('', undefined, mode).meta);
        changed = true;
      });
      if (changed) {
        decorate(this.panel);
        this.refresh();
      }
    }, 0);
  }

  refresh() {
    this.fillModes();  // keep the dropdown's languages in step with the current kernel
    const cell = this.panel.content.activeCell;
    const code = cell && cell.model.type === 'code';
    const source = code ? cell.model.sharedModel.getSource() : '';
    const mode = code ? modeOf(source, metaOf(cell)) : 'python';
    this.select.disabled = !cell;
    this.select.value = modeForPanel(this.panel, mode);  // show it in this kernel's own set
    const sql = mode !== 'python';
    this.select.classList.toggle('duckduck-on', sql);
    this.name.hidden = this.arrow.hidden = !sql;
    if (sql && document.activeElement !== this.name) {
      const meta = metaOf(cell);
      this.name.value = nameOf(source, meta) || '';
      this.name.style.fontStyle = isAuto(meta) ? 'italic' : 'normal';
    }
  }

  apply(mode) {
    const notebook = this.panel.content;
    if (!notebook.activeCell) {
      return;
    }
    if (notebook.activeCell.model.type !== 'code') {
      NotebookActions.changeCellType(notebook, 'code');
    }
    const cell = notebook.activeCell;
    const source = cell.model.sharedModel.getSource();
    const meta = metaOf(cell);
    if (modeOf(source, meta) !== mode) {
      setCell(cell, convert(source, meta, mode));
    }
    this.panel.__duckduckMode = mode;
    const kept = this.panel.model.getMetadata(KEY) || {};
    if (kept.cell_language !== mode) {
      this.panel.model.setMetadata(KEY, { ...kept, cell_language: mode });
    }
    decorate(this.panel);
    this.refresh();
    cell.editor && cell.editor.focus();
  }

  rename(value) {
    const cell = this.panel.content.activeCell;
    if (!cell || cell.model.type !== 'code') {
      return;
    }
    const source = cell.model.sharedModel.getSource();
    const meta = metaOf(cell);
    if (modeOf(source, meta) === 'python') {
      return;
    }
    if (value && (!NAME.test(value) || value.startsWith('__'))) {
      this.name.value = nameOf(source, meta) || ''; // not a Python name: back to what it was
      return;
    }
    const sql = convert(source, meta, modeOf(source, meta)); // a %%sql line moves into the metadata
    sql.meta.name = value || null; // empty: automatic again
    sql.meta.auto = !value;
    setCell(cell, sql);
    decorate(this.panel);
    this.refresh();
  }
}

function post(message) {
  if (window.parent && window.parent !== window) {
    window.parent.postMessage(message, window.location.origin);
  }
}

function current(tracker) {
  const panel = tracker.currentWidget;
  if (!panel) {
    return { type: 'duckduck:current', path: null, kernel: null };
  }
  const session = panel.sessionContext;
  return { type: 'duckduck:current', path: panel.context.path, kernel: session ? session.kernelDisplayName : null };
}

async function insert(app, tracker, code, sql) {
  const panel = tracker.currentWidget;
  if (!panel) {
    throw new Error('open a notebook first');
  }
  await panel.context.ready;
  app.shell.activateById(panel.id);
  const notebook = panel.content;
  // on the notebook's intro or its session-setup cells (magics, connect): go to the end instead, so the
  // reading cells come after the session is set up
  const setup = cell => {
    if (!cell || cell.model.type !== 'code') {
      return true;
    }
    const source = cell.model.sharedModel.getSource();
    return modeOf(source, metaOf(cell)) === 'python' && /^\s*%|connect\(/.test(source);
  };
  if (setup(notebook.activeCell) && notebook.widgets.length) {
    notebook.activeCellIndex = notebook.widgets.length - 1;
  }
  const active = notebook.activeCell;
  const empty = active && active.model.type === 'code' && active.model.sharedModel.getSource().trim() === '';
  // SQL after an SQL cell, or into an empty cell when the switch was last set to SQL; the page's SQL already
  // names the engine the table needs (Spark SQL for a Glue table, duckduck for the rest)
  const wanted = active && !empty ? modeOf(active.model.sharedModel.getSource(), metaOf(active))
    : panel.__duckduckMode || 'python';
  if (!empty) {
    NotebookActions.insertBelow(notebook);
    if (notebook.activeCell && notebook.activeCell.model.type !== 'code') {
      NotebookActions.changeCellType(notebook, 'code');
    }
  }
  const cell = notebook.activeCell;
  // the page's SQL starts with %%sql [name] [--duckduck]: that line becomes the cell's metadata
  let result = { source: code, meta: null };
  if (sql && wanted !== 'python') {
    // the local DuckDB kernel has one SQL engine (duck.sql); on Glue keep the engine the page's SQL named
    result = convert(sql, undefined, isLocalDuck(panel) ? 'duckdb' : undefined);
    result.meta.auto = true; // named after its table, like a typed one
  }
  setCell(cell, result);
  decorate(panel);
  notebook.mode = 'command';
  if (cell.node && cell.node.scrollIntoView) {
    cell.node.scrollIntoView({ block: 'nearest' });
  }
  return { path: panel.context.path, index: notebook.activeCellIndex };
}

// ---- the editable grid: edit a result's shown rows into a new DataFrame, or build a new table from scratch —
// both insert a `name = pd.DataFrame(...)` cell for the user to run. The output's ✎ Edit calls the global
// window.__duckduckEditGrid (set in activate); the Tables panel's ＋ New table calls openGridEditor directly.
// Plain JS, no dependency, like the chart builder. Only the shown rows (≤ the output's limit) travel here.

function pyStr(s) {
  return "'" + String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r').replace(/\t/g, '\\t') + "'";
}
// a column is numeric when every non-empty cell parses as a number
function columnIsNumeric(rows, ci) {
  let any = false;
  for (const r of rows) {
    const v = r[ci];
    if (v === '' || v == null) {
      continue;
    }
    if (isNaN(Number(v))) {
      return false;
    }
    any = true;
  }
  return any;
}
function pyCell(v, isNum) {
  if (v === '' || v == null) {
    return 'None';
  }
  return isNum ? String(Number(v)) : pyStr(v);
}
// Python for `name = pd.DataFrame([...rows...], columns=[...])` (an all-number column → numeric, empty → None).
// No `import pandas as pd`: the local DuckDB kernel puts `pd` in the namespace at startup (like `duck`).
function dataframeCode(name, columns, rows) {
  const nums = columns.map((_, ci) => columnIsNumeric(rows, ci));
  const body = rows.map(r => '    [' + columns.map((_, ci) => pyCell(r[ci], nums[ci])).join(', ') + ']').join(',\n');
  const data = rows.length ? '[\n' + body + '\n]' : '[]';
  return `${name} = pd.DataFrame(${data}, columns=[${columns.map(pyStr).join(', ')}])\n${name}`;
}

// insert a plain Python code cell below the active one
function insertCodeCell(code) {
  const panel = gTracker && gTracker.currentWidget;
  if (!panel) {
    return false;
  }
  if (gApp) {
    gApp.shell.activateById(panel.id);
  }
  const notebook = panel.content;
  NotebookActions.insertBelow(notebook);
  if (notebook.activeCell && notebook.activeCell.model.type !== 'code') {
    NotebookActions.changeCellType(notebook, 'code');
  }
  const cell = notebook.activeCell;
  if (cell.model.getMetadata(KEY) !== undefined) {
    cell.model.deleteMetadata(KEY);  // a plain Python cell, never SQL
  }
  cell.model.sharedModel.setSource(code);  // synchronous, so newCells() sees it filled and skips it
  decorate(panel);
  notebook.mode = 'command';
  if (cell.node && cell.node.scrollIntoView) {
    cell.node.scrollIntoView({ block: 'nearest' });
  }
  return true;
}

function gridButton(label) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = label;
  b.style.cssText = 'font:inherit;font-size:11px;padding:3px 9px;cursor:pointer;border:1px solid var(--jp-border-color2);'
    + 'border-radius:4px;background:var(--jp-layout-color1);color:var(--jp-ui-font-color1)';
  return b;
}

// a modal shell for the extension's own dialogs (the output has its own in _VIEWER_JS)
function appModal(titleText) {
  const ov = document.createElement('div');
  ov.style.cssText = 'position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.45);display:flex;'
    + 'align-items:center;justify-content:center;padding:24px';
  const md = document.createElement('div');
  md.style.cssText = 'background:var(--jp-layout-color1);color:var(--jp-ui-font-color1);border-radius:8px;'
    + 'max-width:95vw;max-height:90vh;min-width:360px;display:flex;flex-direction:column;box-shadow:0 10px 40px rgba(0,0,0,.4)';
  const head = document.createElement('div');
  head.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 12px;'
    + 'border-bottom:1px solid var(--jp-border-color2)';
  const title = document.createElement('strong');
  title.style.cssText = 'font-size:13px;margin-right:auto';
  title.textContent = titleText;
  head.appendChild(title);
  const body = document.createElement('div');
  body.style.cssText = 'overflow:auto;padding:12px';
  md.append(head, body);
  ov.appendChild(md);
  function close() {
    document.removeEventListener('keydown', onkey);
    ov.remove();
  }
  function onkey(e) {
    if (e.key === 'Escape') {
      close();
    }
  }
  const closeBtn = gridButton('✕ Close');
  closeBtn.onclick = close;
  ov.addEventListener('click', e => {
    if (e.target === ov) {
      close();
    }
  });
  document.addEventListener('keydown', onkey);
  return { overlay: ov, head, title, body, close, closeBtn, open: () => document.body.appendChild(ov) };
}

// the editable grid. columns: string[], rows: any[][]. "Use as DataFrame" inserts a pd.DataFrame cell.
function openGridEditor(opts) {
  const columns = opts.columns && opts.columns.length ? opts.columns.map(String) : ['col1', 'col2'];
  const rows = opts.rows && opts.rows.length
    ? opts.rows.map(r => columns.map((_, ci) => (r[ci] == null ? '' : r[ci])))
    : [columns.map(() => ''), columns.map(() => ''), columns.map(() => '')];
  const m = appModal(opts.title || 'Table editor');
  const name = document.createElement('input');
  name.type = 'text';
  name.value = /^[A-Za-z_]\w*$/.test(opts.suggestName || '') ? opts.suggestName : 'df';
  name.style.cssText = 'font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--jp-border-color2);'
    + 'border-radius:4px;background:var(--jp-layout-color0);color:var(--jp-ui-font-color1);width:160px';
  const nameLab = document.createElement('label');
  nameLab.style.cssText = 'font-size:11px;color:var(--jp-ui-font-color2);display:flex;gap:4px;align-items:center;margin-right:auto';
  nameLab.append(document.createTextNode('DataFrame'), name);
  const addRow = gridButton('＋ Row');
  const addCol = gridButton('＋ Column');
  const use = gridButton('Use as DataFrame');
  use.style.borderColor = use.style.color = 'var(--jp-brand-color1)';
  m.head.append(nameLab, addRow, addCol, use, m.closeBtn);
  const wrap = document.createElement('div');
  wrap.style.cssText = 'overflow:auto;max-height:64vh';
  const table = document.createElement('table');
  table.style.cssText = 'border-collapse:collapse;font-size:12px';
  wrap.appendChild(table);
  const note = document.createElement('div');
  note.style.cssText = 'font-size:11px;color:var(--jp-ui-font-color2);margin-top:8px';
  note.textContent = 'Edit cells, rename headers, add/remove rows and columns. A column whose values are all numbers '
    + 'becomes numeric; empty cells become None. "Use as DataFrame" inserts a pd.DataFrame cell to run.';
  m.body.append(wrap, note);
  const cellStyle = 'border:1px solid var(--jp-border-color2);padding:0';
  const inp = 'width:140px;border:0;background:transparent;color:inherit;font:inherit;font-size:12px;padding:3px 6px;box-sizing:border-box';
  function render() {
    let html = '<thead><tr><th style="' + cellStyle + ';background:var(--jp-layout-color2)"></th>';
    columns.forEach((c, ci) => {
      html += '<th style="' + cellStyle + ';background:var(--jp-layout-color2)"><div style="display:flex;align-items:center">'
        + '<input data-col="' + ci + '" value="' + escAttr(c) + '" style="' + inp + ';font-weight:600" />'
        + '<button data-delcol="' + ci + '" title="Remove column" style="border:0;background:transparent;cursor:pointer;'
        + 'color:var(--jp-ui-font-color3);padding:0 6px">✕</button></div></th>';
    });
    html += '</tr></thead><tbody>';
    rows.forEach((r, ri) => {
      html += '<tr><td style="' + cellStyle + ';background:var(--jp-layout-color2);text-align:center;padding:0 6px">'
        + '<button data-delrow="' + ri + '" title="Remove row" style="border:0;background:transparent;cursor:pointer;'
        + 'color:var(--jp-ui-font-color3)">✕</button></td>';
      columns.forEach((_, ci) => {
        html += '<td style="' + cellStyle + '"><input data-r="' + ri + '" data-c="' + ci + '" value="'
          + escAttr(r[ci] == null ? '' : r[ci]) + '" style="' + inp + '" /></td>';
      });
      html += '</tr>';
    });
    table.innerHTML = html + '</tbody>';
  }
  render();
  table.addEventListener('input', e => {
    const t = e.target;
    if (t.hasAttribute('data-r')) {
      rows[+t.getAttribute('data-r')][+t.getAttribute('data-c')] = t.value;
    } else if (t.hasAttribute('data-col')) {
      columns[+t.getAttribute('data-col')] = t.value;
    }
  });
  table.addEventListener('click', e => {
    const dc = e.target.closest('[data-delcol]');
    const dr = e.target.closest('[data-delrow]');
    if (dc && columns.length > 1) {
      const ci = +dc.getAttribute('data-delcol');
      columns.splice(ci, 1);
      rows.forEach(r => r.splice(ci, 1));
      render();
    } else if (dr) {
      rows.splice(+dr.getAttribute('data-delrow'), 1);
      render();
    }
  });
  addRow.onclick = () => {
    rows.push(columns.map(() => ''));
    render();
  };
  addCol.onclick = () => {
    columns.push('col' + (columns.length + 1));
    rows.forEach(r => r.push(''));
    render();
  };
  use.onclick = () => {
    const cols = columns.map((c, i) => String(c).trim() || ('col' + (i + 1)));
    const nm = /^[A-Za-z_]\w*$/.test(String(name.value).trim()) ? String(name.value).trim() : 'df';
    if (insertCodeCell(dataframeCode(nm, cols, rows))) {
      m.close();
    }
  };
  m.open();
  name.focus();
}

const plugin = {
  id: 'duckduck-notebook:bridge',
  description: 'Adds the cells the duckduck page sends to the open notebook.',
  autoStart: true,
  requires: [INotebookTracker, IEditorExtensionRegistry],
  activate: (app, tracker, extensions) => {
    // the duckduck catalog, as a panel in JupyterLab's own left sidebar (not a second list beside the IDE)
    const tablesPanel = new TablesPanel();
    app.shell.add(tablesPanel, 'left', { rank: 1000 });
    // inline autocomplete in SQL cells: a CodeMirror source fed by the notebook's DataFrames + the catalog
    gApp = app;
    gTracker = tracker;
    gPanel = tablesPanel;
    // the output's ✎ Edit button (in _VIEWER_JS, sandboxed from this module) reaches the editable grid through
    // this global — the extension runs in the JupyterLab page, so it can read the kernel's rows and insert a cell
    window.__duckduckEditGrid = payload => {
      const p = payload || {};
      openGridEditor({
        title: 'Edit — ' + (p.name || 'result'),
        columns: Array.isArray(p.columns) ? p.columns : [],
        rows: Array.isArray(p.rows) ? p.rows : [],
        suggestName: (/^[A-Za-z_]\w*$/.test(p.name || '') ? p.name : 'df') + '_edited'
      });
    };
    extensions.addExtension({
      name: 'duckduck:sql-autocomplete',
      factory: options => EditorExtensionRegistry.createImmutableExtension([
        autocompletion({ activateOnTyping: true, override: [sqlCompletionSource(options.model)] }),
        // keep ↑/↓ on the open dropdown — at the highest precedence, so JupyterLab's "arrow at the edge of the
        // editor moves to the next/previous cell" doesn't steal them. moveCompletionSelection acts only while a
        // completion is showing; with the dropdown closed it returns false and the arrow falls through as usual.
        Prec.highest(keymap.of([
          { key: 'ArrowDown', run: moveCompletionSelection(true) },
          { key: 'ArrowUp', run: moveCompletionSelection(false) }
        ]))
      ])
    });
    window.addEventListener('message', async event => {
      if (event.origin !== window.location.origin || event.source !== window.parent) {
        return;
      }
      const data = event.data || {};
      if (data.type === 'duckduck:insert' && typeof data.code === 'string') {
        try {
          const done = await insert(app, tracker, data.code, typeof data.sql === 'string' ? data.sql : null);
          post({ type: 'duckduck:inserted', id: data.id, ok: true, ...done });
        } catch (err) {
          post({ type: 'duckduck:inserted', id: data.id, ok: false, error: String(err.message || err) });
        }
      } else if (data.type === 'duckduck:open' && typeof data.path === 'string') {
        try {
          await app.commands.execute('docmanager:open', { path: data.path, factory: 'Notebook' });
          post({ type: 'duckduck:opened', id: data.id, ok: true, path: data.path });
        } catch (err) {
          post({ type: 'duckduck:opened', id: data.id, ok: false, error: String(err.message || err) });
        }
      } else if (data.type === 'duckduck:tables' && Array.isArray(data.tables)) {
        tablesPanel.setTables(data.tables);
      } else if (data.type === 'duckduck:hello') {
        post({ type: 'duckduck:ready' });
        post(current(tracker));
      }
    });
    addStyle();
    // names settled just before a cell runs: JupyterLab emits this synchronously, then reads the cell's metadata
    // for the execute request — so a name the debounced decorate() hasn't caught up with yet is still right
    NotebookActions.executionScheduled.connect((_, args) => {
      if (args && args.notebook) {
        nameCells(args.notebook);
      }
    });
    app.docRegistry.addWidgetExtension('Notebook', {
      createNew: panel => {
        const widget = new LanguageSwitch(panel);
        if (!panel.toolbar.insertAfter('cellType', 'duckduckLanguage', widget)) {
          panel.toolbar.addItem('duckduckLanguage', widget);
        }
        return widget;
      }
    });
    tracker.currentChanged.connect(() => {
      post(current(tracker));
      const session = tracker.currentWidget && tracker.currentWidget.sessionContext;
      if (session && !session.__duckduck) {
        session.__duckduck = true;
        session.kernelChanged.connect(() => post(current(tracker)));
        session.ready.then(() => post(current(tracker)));
      }
    });
    app.restored.then(() => {
      post({ type: 'duckduck:ready' });
      post(current(tracker));
    });
  }
};

export default plugin;

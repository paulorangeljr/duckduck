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
import { INotebookTracker, NotebookActions } from '@jupyterlab/notebook';
import { Widget } from '@lumino/widgets';
import { KEY, NAME, autoNames, convert, isAuto, modeOf, nameOf } from './toggle';

const MODES = [
  ['python', 'PySpark', 'Python code, run in the session'],
  ['sql', 'SQL', 'Spark SQL in the session, the result kept as a DataFrame (df = spark.sql(...))'],
  ['duckduck', 'SQL · duckduck', 'read through duckduck (reader.sql), the result kept as a DataFrame']
];
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
    select.title = 'The active cell: PySpark or SQL (an SQL cell keeps its result as a DataFrame)';
    select.setAttribute('aria-label', 'Cell language');
    MODES.forEach(([value, label, title]) => {
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
      if (kept && ['python', 'sql', 'duckduck'].includes(kept.cell_language)) {
        panel.__duckduckMode = kept.cell_language;
      }
      panel.model.cells.changed.connect((_, change) => this.newCells(change));
      this.refresh();
      decorate(panel);
    });
    panel.sessionContext.kernelChanged.connect(() => setTimeout(() => decorate(panel), 500));
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
    const cell = this.panel.content.activeCell;
    const code = cell && cell.model.type === 'code';
    const source = code ? cell.model.sharedModel.getSource() : '';
    const mode = code ? modeOf(source, metaOf(cell)) : 'python';
    this.select.disabled = !cell;
    this.select.value = mode;
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
    result = convert(sql, undefined, undefined);
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

const plugin = {
  id: 'duckduck-notebook:bridge',
  description: 'Adds the cells the duckduck page sends to the open notebook.',
  autoStart: true,
  requires: [INotebookTracker],
  activate: (app, tracker) => {
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

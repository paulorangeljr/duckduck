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
// The notebook toolbar gets a PySpark / SQL / SQL · duckduck switch for the active cell (convert() in toggle.js):
// an SQL cell starts with %%sql, which the duckduck kernel runs as df = spark.sql(...). SQL cells are highlighted
// as SQL and marked with a stripe. A table clicked in the page arrives as SQL ({sql}) when the cell it goes to is
// an SQL cell, or the switch was last set to SQL.
import { INotebookTracker, NotebookActions } from '@jupyterlab/notebook';
import { Widget } from '@lumino/widgets';
import { convert, modeOf } from './toggle';

const MODES = [
  ['python', 'PySpark', 'Python code, run in the session'],
  ['sql', 'SQL', '%%sql: Spark SQL in the session, the result kept as a DataFrame (df = spark.sql(...))'],
  ['duckduck', 'SQL · duckduck', '%%sql --duckduck: read through duckduck (reader.sql), the result kept as a DataFrame']
];
const STYLE = `
.jp-CodeCell.duckduck-sql .jp-InputArea-editor { border-left: 3px solid var(--jp-warn-color1, #e8a33d); }
.jp-CodeCell.duckduck-sql .cm-content .cm-line:first-child { opacity: 0.6; font-style: italic; }
.jp-CodeCell.duckduck-sql .jp-InputPrompt::after { content: 'SQL'; display: block; font-size: 9px;
  color: var(--jp-warn-color1, #e8a33d); font-weight: 600; }
.duckduck-langswitch select { height: 24px; }
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

// SQL cells: a class and SQL highlighting (the notebook may set the kernel's mime type back: done again on change)
function decorate(panel) {
  const notebook = panel.content;
  const python = notebook.codeMimetype || 'text/x-ipython';
  notebook.widgets.forEach(cell => {
    if (cell.model.type !== 'code') {
      return;
    }
    const sql = modeOf(cell.model.sharedModel.getSource()) !== 'python';
    cell.toggleClass('duckduck-sql', sql);
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
    node.appendChild(select);
    super({ node });
    this.select = select;
    this.panel = panel;
    this.addClass('jp-HTMLSelect');
    this.addClass('jp-DefaultStyle');
    select.addEventListener('change', () => this.apply(select.value));
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
      this.refresh();
      decorate(panel);
    });
    panel.sessionContext.kernelChanged.connect(() => setTimeout(() => decorate(panel), 500));
  }

  refresh() {
    const cell = this.panel.content.activeCell;
    const code = cell && cell.model.type === 'code';
    this.select.disabled = !cell;
    this.select.value = code ? modeOf(cell.model.sharedModel.getSource()) : 'python';
  }

  apply(mode) {
    const notebook = this.panel.content;
    if (!notebook.activeCell) {
      return;
    }
    if (notebook.activeCell.model.type !== 'code') {
      NotebookActions.changeCellType(notebook, 'code');
    }
    const model = notebook.activeCell.model.sharedModel;
    const now = model.getSource();
    if (modeOf(now) !== mode) {
      model.setSource(convert(now, mode));
    }
    this.panel.__duckduckMode = mode;
    decorate(this.panel);
    this.refresh();
    notebook.activeCell.editor && notebook.activeCell.editor.focus();
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
    return modeOf(source) === 'python' && /^\s*%|connect\(/.test(source);
  };
  if (setup(notebook.activeCell) && notebook.widgets.length) {
    notebook.activeCellIndex = notebook.widgets.length - 1;
  }
  const active = notebook.activeCell;
  const empty = active && active.model.type === 'code' && active.model.sharedModel.getSource().trim() === '';
  // SQL after an SQL cell, or into an empty cell when the switch was last set to SQL; the page's SQL already
  // names the engine the table needs (Spark SQL for a Glue table, duckduck for the rest)
  const wanted = active && !empty ? modeOf(active.model.sharedModel.getSource()) : panel.__duckduckMode || 'python';
  if (!empty) {
    NotebookActions.insertBelow(notebook);
    if (notebook.activeCell && notebook.activeCell.model.type !== 'code') {
      NotebookActions.changeCellType(notebook, 'code');
    }
  }
  const cell = notebook.activeCell;
  cell.model.sharedModel.setSource(sql && wanted !== 'python' ? sql : code);
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

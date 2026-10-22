// duckduck's bridge into JupyterLab (the web app's Notebook tab embeds JupyterLab in an iframe).
//
// The page posts, from the same origin and only as the iframe's parent:
//   {type: 'duckduck:insert', id, code}  → a code cell below the active one (the active cell itself when it's an
//                                          empty code cell; after the last cell when the active one is the intro
//                                          or a session-setup cell), scrolled into view; never run
//   {type: 'duckduck:open', id, path}    → opens that notebook
// and gets back {type: 'duckduck:inserted' | 'duckduck:opened', id, ok, path, error}. JupyterLab tells the page
// {type: 'duckduck:ready'} once restored and {type: 'duckduck:current', path, kernel} when the notebook changes.
import { INotebookTracker, NotebookActions } from '@jupyterlab/notebook';

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

async function insert(app, tracker, code) {
  const panel = tracker.currentWidget;
  if (!panel) {
    throw new Error('open a notebook first');
  }
  await panel.context.ready;
  app.shell.activateById(panel.id);
  const notebook = panel.content;
  // on the notebook's intro or its session-setup cells (magics, connect): go to the end instead, so the
  // reading cells come after the session is set up
  const setup = cell =>
    !cell || cell.model.type !== 'code' || /^\s*%|connect\(/.test(cell.model.sharedModel.getSource());
  if (setup(notebook.activeCell) && notebook.widgets.length) {
    notebook.activeCellIndex = notebook.widgets.length - 1;
  }
  const active = notebook.activeCell;
  const empty = active && active.model.type === 'code' && active.model.sharedModel.getSource().trim() === '';
  if (!empty) {
    NotebookActions.insertBelow(notebook);
    if (notebook.activeCell && notebook.activeCell.model.type !== 'code') {
      NotebookActions.changeCellType(notebook, 'code');
    }
  }
  const cell = notebook.activeCell;
  cell.model.sharedModel.setSource(code);
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
          const done = await insert(app, tracker, data.code);
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

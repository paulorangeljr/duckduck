// node toggle.test.mjs — the switch's conversions and the automatic DataFrame names (run by tests/test_notebook.py)
import assert from 'node:assert/strict';
import { autoNames, baseName, convert, modeOf, nameOf, parseTableCode, tableCell, tableCode,
  toPython, toSql } from './toggle.js';

const sql = (source, extra) => ({ source, meta: { language: 'sql', engine: 'spark', name: null, auto: true, ...extra } });

// PySpark → SQL → PySpark
const call = 'assets = reader.sql("""\nSELECT * FROM assets LIMIT 100\n""")  # a note\nassets.show(20)';
assert.deepEqual(toSql(call, null, 'sql'),
  { source: 'SELECT * FROM assets LIMIT 100', meta: { language: 'sql', engine: 'spark', name: 'assets', auto: false } });
assert.equal(toSql(call, null, undefined).meta.engine, 'duckduck');
const typed = toSql('SELECT * FROM security.proxy_logs', null, 'sql');
assert.deepEqual(typed.meta, { language: 'sql', engine: 'spark', name: 'proxy_logs', auto: true });
assert.equal(modeOf(typed.source, typed.meta), 'sql');
assert.equal(toPython(typed.source, typed.meta).source,
  'proxy_logs = spark.sql("""\nSELECT * FROM security.proxy_logs\n""")\nproxy_logs.show(20)');
assert.deepEqual(toPython('', typed.meta), { source: '', meta: null });
// a hand-written %%sql line moves into the metadata
assert.deepEqual(toSql('%%sql hosts --duckduck\nSELECT 1', null).meta,
  { language: 'sql', engine: 'duckduck', name: 'hosts', auto: false });
assert.equal(modeOf('x = 1', null), 'python');

// the local DuckDB kernel: pandas via duck.sql(...).df(), round-tripping both ways
const duckCell = toSql('SELECT * FROM events', null, 'duckdb');
assert.deepEqual(duckCell.meta, { language: 'sql', engine: 'duckdb', name: 'events_df', auto: true });
assert.equal(modeOf(duckCell.source, duckCell.meta), 'duckdb');
assert.equal(toPython(duckCell.source, { ...duckCell.meta, name: 'events', auto: false }).source,
  'events = duck.sql("""\nSELECT * FROM events\n""").df()\nevents');
const duckCode = 'ev = duck.sql("""\nSELECT 1 AS a\n""").df()\nev';
assert.deepEqual(toSql(duckCode, null, undefined).meta,
  { language: 'sql', engine: 'duckdb', name: 'ev', auto: false });

// names: after the table, never the table read itself, df_N without one, unique, stable
assert.equal(baseName('SELECT * FROM security.proxy_logs'), 'proxy_logs');
assert.equal(baseName('select a from hosts'), 'hosts_df');
assert.equal(baseName("-- from nowhere\nSELECT 'from x' FROM `db`.`My Table`"), 'my_table');
assert.equal(baseName('SELECT 1'), null);
assert.deepEqual(autoNames([sql('SELECT * FROM a.t'), sql('SELECT * FROM b.t'), sql('SELECT 1'), sql('SELECT 2')]),
  ['t', 't_2', 'df_1', 'df_2']);
assert.deepEqual(autoNames([sql('SELECT * FROM b.t', { name: 't_2' }), sql('SELECT * FROM a.t', { name: 't' })]),
  ['t_2', 't']);
assert.deepEqual(autoNames([sql('SELECT * FROM a.t', { name: 'x' }), sql('SELECT 1', { name: 't', auto: false })]),
  ['t_2', 't']);
assert.deepEqual(autoNames([sql('SELECT 1', { name: 'df', auto: undefined })]), ['df_1']);

// a table cell: the rows live in the metadata, the source is one short line
const grid = tableCell('owners', ['host', 'risk', 'note'],
  [['web-01', '91', "O'Brien"], ['db-02', '', 'x\ty'], ['api-03', '7', '']]);
assert.equal(grid.source, '# \u25a6 owners \u2014 3 rows \u00d7 3 columns');
assert.equal(grid.meta.language, 'table');
assert.equal(modeOf(grid.source, grid.meta), 'table');
assert.equal(nameOf(grid.source, grid.meta), 'owners');
// "Show the code" writes the pandas that builds it — a column of numbers is numeric, empty cells are None
const shown = convert(grid.source, grid.meta, 'python');
assert.equal(shown.meta, null);
assert.match(shown.source, /^owners = pd\.DataFrame\(\[/);
assert.match(shown.source, /\['web-01', 91, 'O\\'Brien'\]/);
assert.match(shown.source, /\['db-02', None, 'x\\ty'\]/);
assert.ok(shown.source.endsWith("columns=['host', 'risk', 'note'])\nowners"));
// and back again, exactly
assert.deepEqual(convert(shown.source, shown.meta, 'table').meta, grid.meta);
// a table cell is never turned into SQL, and ordinary code is never turned into a table
assert.deepEqual(convert(grid.source, grid.meta, 'duckdb').meta, grid.meta);
assert.deepEqual(convert('x = 1', undefined, 'table'), { source: 'x = 1', meta: null });
assert.equal(parseTableCode('df = pd.DataFrame(open("x"), columns=["a"])'), null);
assert.equal(parseTableCode("df = pd.DataFrame([['a]], columns=['a'])"), null);
assert.equal(tableCode('t', [], []), "t = pd.DataFrame([], columns=[])\nt");

console.log('ok');

// node toggle.test.mjs — the switch's conversions and the automatic DataFrame names (run by tests/test_notebook.py)
import assert from 'node:assert/strict';
import { autoNames, baseName, modeOf, toPython, toSql } from './toggle.js';

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
console.log('ok');

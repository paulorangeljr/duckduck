// A cell's language, and turning it from PySpark into SQL and back (the toolbar's PySpark / SQL switch).
//
// An SQL cell holds only its SQL; what makes it SQL is the cell's metadata:
//   "duckduck": {"language": "sql", "engine": "spark" | "duckduck" | "duckdb", "name": "proxy_logs", "auto": true}
// JupyterLab sends a cell's metadata with each execution, and the duckduck kernel runs such a cell as
// name = spark.sql(<the SQL>) (reader.sql with engine duckduck; duck.sql(...).df() → pandas with engine duckdb,
// the local DuckDB kernel). A cell whose first line is %%sql (the older
// form, still run as such) counts as SQL too, and loses that line when switched.
// To PySpark, the cell becomes the code it stood for:   name = spark.sql("""\n<sql>\n""")\nname.show(20)
// and such a cell (or reader.sql's) goes back to SQL as it was.

export const DEFAULT_NAME = 'df';
export const KEY = 'duckduck';

const HEADER = /^%%sql\b(.*)$/;
export const NAME = /^[A-Za-z_]\w*$/;

function headerLine(source) {
  const lines = source.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith('%%sql')) {
      return { index: i, line, lines };
    }
    if (line.trim() && !line.trim().startsWith('#')) {
      return null;
    }
  }
  return null;
}

export function parseHeader(line) {
  const out = { name: DEFAULT_NAME, engine: 'spark', flags: [] };
  const words = (HEADER.exec(line.trim()) || ['', ''])[1].trim().split(/\s+/).filter(Boolean);
  words.forEach((word, i) => {
    if (word === '--duckduck') {
      out.engine = 'duckduck';
    } else if (i === 0 && NAME.test(word)) {
      out.name = word;
    } else {
      out.flags.push(word);
    }
  });
  return out;
}

function isSql(meta) {
  return !!meta && meta.language === 'sql';
}

// 'python' | 'sql' | 'duckduck' | 'duckdb' (duckdb = the local DuckDB kernel: duck.sql(...).df() → pandas)
export function modeOf(source, meta) {
  if (isTable(meta)) {
    return TABLE;
  }
  if (isSql(meta)) {
    return meta.engine === 'duckduck' ? 'duckduck' : meta.engine === 'duckdb' ? 'duckdb' : 'sql';
  }
  const found = headerLine(source || '');
  if (!found) {
    return 'python';
  }
  return parseHeader(found.line).engine === 'duckduck' ? 'duckduck' : 'sql';
}

// the DataFrame an SQL cell leaves (null for a PySpark cell)
export function nameOf(source, meta) {
  if (isTable(meta)) {
    return meta.name || DEFAULT_NAME;
  }
  if (isSql(meta)) {
    return meta.name || DEFAULT_NAME;
  }
  const found = headerLine(source || '');
  return found ? parseHeader(found.line).name : null;
}

const CALL =
  /^\s*([A-Za-z_]\w*)\s*=\s*(spark|reader|duck)\.sql\(\s*[rR]?("""|''')([\s\S]*?)\3\s*\)(?:\.df\(\))?[ \t]*(?:#[^\n]*)?(?:\n[ \t]*\1(?:\.(?:show|head)\([^)\n]*\))?[ \t]*(?:#[^\n]*)?)?\s*$/;

// the engine a `<name> = <x>.sql(...)` call stands for
function callEngine(x) {
  return x === 'reader' ? 'duckduck' : x === 'duck' ? 'duckdb' : 'spark';
}

function trimmed(text) {
  return text.replace(/^\n+|\s+$/g, '');
}

// → {source, meta}: the SQL alone, the language in the metadata. engine: 'sql' | 'duckduck' (undefined: keep
// what the cell said)
export function toSql(source, meta, engine) {
  let text = source || '';
  let name = isSql(meta) ? meta.name || null : null;
  let auto = isSql(meta) ? isAuto(meta) : true;
  let was = isSql(meta) ? meta.engine : undefined;
  const found = headerLine(text);
  if (found) {
    const h = parseHeader(found.line);
    auto = !/^%%sql\s+[A-Za-z_]/.test(found.line.trim()); // a name written on the line is kept
    name = auto ? null : h.name;
    was = h.engine;
    found.lines.splice(found.index, 1);
    text = trimmed(found.lines.join('\n'));
  } else if (!isSql(meta)) {
    // drop a leading "# fill in the '<…>' values first" note
    const body = text.replace(/^# fill in[^\n]*\n/, '');
    const call = CALL.exec(body);
    if (call) {
      name = call[1];
      auto = false;
      was = callEngine(call[2]);
      text = trimmed(call[4]);
    }
  }
  const wanted = engine === undefined ? was || 'spark'
    : engine === 'duckduck' ? 'duckduck' : engine === 'duckdb' ? 'duckdb' : 'spark';
  if (auto) {
    // a first guess: decorate() makes it unique in the notebook
    name = name || baseName(text) || 'df_1';
  }
  return { source: text, meta: { language: 'sql', engine: wanted, name, auto } };
}

// → {source, meta: null}
export function toPython(source, meta) {
  let text = source || '';
  let name = DEFAULT_NAME;
  let engine = 'spark';
  let before = '';
  const found = headerLine(text);
  if (found) {
    const h = parseHeader(found.line);
    name = h.name;
    engine = h.engine;
    before = found.lines.slice(0, found.index).join('\n');
    text = found.lines.slice(found.index + 1).join('\n');
  } else if (isSql(meta)) {
    name = meta.name || DEFAULT_NAME;
    engine = meta.engine === 'duckduck' ? 'duckduck' : meta.engine === 'duckdb' ? 'duckdb' : 'spark';
  } else {
    return { source: text, meta: null };
  }
  const sql = trimmed(text);
  if (!sql) {
    return { source: before, meta: null };
  }
  const quote = sql.includes('"""') ? "'''" : '"""';
  // the local DuckDB kernel: pandas via duck.sql(...).df(), the DataFrame shown as the last expression
  const code = engine === 'duckdb'
    ? `${name} = duck.sql(${quote}\n${sql}\n${quote}).df()\n${name}`
    : `${name} = ${engine === 'duckduck' ? 'reader' : 'spark'}.sql(${quote}\n${sql}\n${quote})\n${name}.show(20)`;
  return { source: before ? before + '\n' + code : code, meta: null };
}

// ---- automatic DataFrame names ---------------------------------------------------------------------------------
// An SQL cell's name is automatic (meta.auto) until someone types one: named after the first table it reads —
// security.proxy_logs → proxy_logs; an unqualified name (a DataFrame of the session, perhaps) → hosts_df, so the
// result never replaces what it reads — else df_1, df_2…; unique in the notebook (proxy_logs_2…). A name already
// given stays while it still fits (other cells changing never rename it).

const FROM = /\bfrom\s+((?:`[^`]+`|"[^"]+"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:`[^`]+`|"[^"]+"|[A-Za-z_][\w$]*))*)/i;

function withoutComments(sql) {
  return sql.replace(/--[^\n]*/g, ' ').replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/'(?:[^']|'')*'/g, "''");
}

// the name an SQL cell's DataFrame is named after (null: no table)
export function baseName(sql) {
  const match = FROM.exec(withoutComments(sql || ''));
  if (!match) {
    return null;
  }
  const parts = match[1].split('.').map(p => p.trim().replace(/^[`"]|[`"]$/g, ''));
  let name = parts[parts.length - 1].replace(/\W+/g, '_').replace(/^_+|_+$/g, '').toLowerCase();
  if (!name) {
    return null;
  }
  if (/^\d/.test(name)) {
    name = 't_' + name;
  }
  return parts.length === 1 ? name + '_df' : name;
}

export function isAuto(meta) {
  return isSql(meta) && (meta.auto === true || (meta.auto === undefined && (meta.name || DEFAULT_NAME) === DEFAULT_NAME));
}

function escaped(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// cells: [{source, meta}] of the notebook's SQL cells, in order → their names (the given ones kept)
export function autoNames(cells) {
  const taken = new Set(cells.filter(c => !isAuto(c.meta)).map(c => c.meta.name || DEFAULT_NAME));
  return cells.map(cell => {
    if (!isAuto(cell.meta)) {
      return cell.meta.name || DEFAULT_NAME;
    }
    const base = baseName(cell.source);
    const now = cell.meta.name;
    const fits = now && !taken.has(now) &&
      (base ? new RegExp('^' + escaped(base) + '(_\\d+)?$').test(now) : /^df_\d+$/.test(now));
    let name = now;
    if (!fits) {
      if (base) {
        name = base;
        for (let n = 2; taken.has(name); n++) {
          name = `${base}_${n}`;
        }
      } else {
        let n = 1;
        while (taken.has(`df_${n}`)) {
          n++;
        }
        name = `df_${n}`;
      }
    }
    taken.add(name);
    return name;
  });
}

export function convert(source, meta, mode) {
  if (mode === TABLE) {
    return toTable(source, meta);
  }
  if (isTable(meta)) {
    // a table cell has two readings only: itself, or the pandas code that builds it
    return mode === 'python'
      ? { source: tableCode(meta.name || DEFAULT_NAME, meta.columns || [], meta.rows || []), meta: null }
      : { source: source || '', meta };
  }
  return mode === 'python' ? toPython(source, meta) : toSql(source, meta, mode);
}


// ---- table cells: the rows live in the cell, not in a wall of code ---------------------------------------------
// A table cell keeps its rows in the cell's metadata and shows one short line of source, so a notebook reads
// "▦ owners — 3 rows × 2 columns" instead of a screenful of pd.DataFrame literals. The kernel rebuilds the frame
// from that metadata (sqlcells.table_code), the ▦ button opens the grid on it, and "Show the code" turns it back
// into the plain pandas cell — the same conversion the language switch does.
export const TABLE = 'table';

export function isTable(meta) {
  return !!meta && meta.language === TABLE;
}

export function tableSource(name, columns, rows) {
  const r = (rows || []).length;
  const c = (columns || []).length;
  return `# ▦ ${name} — ${r} row${r === 1 ? '' : 's'} × ${c} column${c === 1 ? '' : 's'}`;
}

export function tableCell(name, columns, rows) {
  const nm = NAME.test(String(name || '')) ? String(name) : DEFAULT_NAME;
  const cols = (columns || []).map(String);
  const body = (rows || []).map(r => cols.map((_, ci) => (r[ci] == null ? '' : String(r[ci]))));
  return { source: tableSource(nm, cols, body), meta: { language: TABLE, name: nm, columns: cols, rows: body } };
}

// a column is numeric when every value it has reads as a number (empty cells become None)
export function columnIsNumeric(rows, ci) {
  let any = false;
  for (const r of rows || []) {
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

export function pyStr(s) {
  return "'" + String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r').replace(/\t/g, '\\t') + "'";
}

function pyCell(v, isNum) {
  if (v === '' || v == null) {
    return 'None';
  }
  return isNum ? String(Number(v)) : pyStr(v);
}

export function tableCode(name, columns, rows) {
  const cols = (columns || []).map(String);
  const body = rows || [];
  const nums = cols.map((_, ci) => columnIsNumeric(body, ci));
  const lines = body.map(r => '    [' + cols.map((_, ci) => pyCell(r[ci], nums[ci])).join(', ') + ']').join(',\n');
  const data = body.length ? '[\n' + lines + '\n]' : '[]';
  return `${name} = pd.DataFrame(${data}, columns=[${cols.map(pyStr).join(', ')}])\n${name}`;
}

// a Python list literal as tableCode writes it → the value, or null for anything else (never an eval)
function pyLiteral(text) {
  const s = String(text || '');
  let out = '';
  let i = 0;
  while (i < s.length) {
    const ch = s[i];
    if (ch === "'" || ch === '"') {
      let value = '';
      i += 1;
      while (i < s.length && s[i] !== ch) {
        if (s[i] === '\\') {
          const next = s[i + 1];
          value += next === 'n' ? '\n' : next === 'r' ? '\r' : next === 't' ? '\t' : next;
          i += 2;
        } else {
          value += s[i];
          i += 1;
        }
      }
      if (i >= s.length) {
        return null;  // unterminated
      }
      i += 1;
      out += JSON.stringify(value);
    } else if (/[A-Za-z_]/.test(ch)) {
      let word = '';
      while (i < s.length && /\w/.test(s[i])) {
        word += s[i];
        i += 1;
      }
      if (word === 'None') {
        out += 'null';
      } else if (word === 'True') {
        out += 'true';
      } else if (word === 'False') {
        out += 'false';
      } else {
        return null;  // a name we did not write
      }
    } else {
      out += ch;
      i += 1;
    }
  }
  try {
    return JSON.parse(out);
  } catch (e) {
    return null;
  }
}

export function parseTableCode(source) {
  const text = String(source || '').trim();
  const m = /^([A-Za-z_]\w*)\s*=\s*pd\.DataFrame\(([\s\S]*),\s*columns\s*=\s*(\[[\s\S]*?\])\s*\)\s*(?:\n\s*\1\s*)?$/.exec(text);
  if (!m) {
    return null;
  }
  const rows = pyLiteral(m[2]);
  const columns = pyLiteral(m[3]);
  if (!Array.isArray(rows) || !Array.isArray(columns) || !columns.length || !rows.every(r => Array.isArray(r))) {
    return null;
  }
  return {
    name: m[1],
    columns: columns.map(String),
    rows: rows.map(r => columns.map((_, ci) => (r[ci] == null ? '' : String(r[ci]))))
  };
}

// only the code a table cell writes can become one again; anything else is left exactly as it is
export function toTable(source, meta) {
  if (isTable(meta)) {
    return { source: source || '', meta };
  }
  const read = parseTableCode(source);
  return read ? tableCell(read.name, read.columns, read.rows) : { source: source || '', meta: meta || null };
}

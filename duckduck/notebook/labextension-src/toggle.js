// A cell's language, and turning it from PySpark into SQL and back (the toolbar's PySpark / SQL switch).
//
// An SQL cell holds only its SQL; what makes it SQL is the cell's metadata:
//   "duckduck": {"language": "sql", "engine": "spark" | "duckduck", "name": "proxy_logs", "auto": true}
// JupyterLab sends a cell's metadata with each execution, and the duckduck kernel runs such a cell as
// name = spark.sql(<the SQL>) (reader.sql with engine duckduck). A cell whose first line is %%sql (the older
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

// 'python' | 'sql' | 'duckduck'
export function modeOf(source, meta) {
  if (isSql(meta)) {
    return meta.engine === 'duckduck' ? 'duckduck' : 'sql';
  }
  const found = headerLine(source || '');
  if (!found) {
    return 'python';
  }
  return parseHeader(found.line).engine === 'duckduck' ? 'duckduck' : 'sql';
}

// the DataFrame an SQL cell leaves (null for a PySpark cell)
export function nameOf(source, meta) {
  if (isSql(meta)) {
    return meta.name || DEFAULT_NAME;
  }
  const found = headerLine(source || '');
  return found ? parseHeader(found.line).name : null;
}

const CALL =
  /^\s*([A-Za-z_]\w*)\s*=\s*(spark|reader)\.sql\(\s*[rR]?("""|''')([\s\S]*?)\3\s*\)[ \t]*(?:#[^\n]*)?(?:\n[ \t]*\1\.show\([^)\n]*\)[ \t]*(?:#[^\n]*)?)?\s*$/;

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
      was = call[2] === 'reader' ? 'duckduck' : 'spark';
      text = trimmed(call[4]);
    }
  }
  const wanted = engine === undefined ? was || 'spark' : engine === 'duckduck' ? 'duckduck' : 'spark';
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
    engine = meta.engine === 'duckduck' ? 'duckduck' : 'spark';
  } else {
    return { source: text, meta: null };
  }
  const sql = trimmed(text);
  if (!sql) {
    return { source: before, meta: null };
  }
  const quote = sql.includes('"""') ? "'''" : '"""';
  const call = engine === 'duckduck' ? 'reader' : 'spark';
  const code = `${name} = ${call}.sql(${quote}\n${sql}\n${quote})\n${name}.show(20)`;
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
  return mode === 'python' ? toPython(source, meta) : toSql(source, meta, mode);
}

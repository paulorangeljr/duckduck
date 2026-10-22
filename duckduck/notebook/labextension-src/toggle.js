// A cell's language, and turning it from PySpark into SQL and back (the toolbar's PySpark / SQL switch).
//
// An SQL cell starts with  %%sql [name] [--duckduck] [--show N]  — the duckduck kernel runs it as
// name = spark.sql(...) (reader.sql(...) with --duckduck); name is df when none is written.
// To PySpark, the cell becomes that code, explicitly:   name = spark.sql("""\n<sql>\n""")\nname.show(20)
// and such a cell (or reader.sql's) goes back to SQL as it was.

export const DEFAULT_NAME = 'df';

const HEADER = /^%%sql\b(.*)$/;
const NAME = /^[A-Za-z_]\w*$/;

function firstLine(source) {
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
  const out = { name: DEFAULT_NAME, engine: 'sql', flags: [] };
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

export function header(name, engine, flags) {
  const parts = ['%%sql'];
  if (name && name !== DEFAULT_NAME) {
    parts.push(name);
  }
  if (engine === 'duckduck') {
    parts.push('--duckduck');
  }
  return parts.concat(flags || []).join(' ');
}

// 'python' | 'sql' | 'duckduck'
export function modeOf(source) {
  const found = firstLine(source || '');
  return found ? parseHeader(found.line).engine : 'python';
}

const CALL =
  /^\s*([A-Za-z_]\w*)\s*=\s*(spark|reader)\.sql\(\s*[rR]?("""|''')([\s\S]*?)\3\s*\)[ \t]*(?:#[^\n]*)?(?:\n[ \t]*\1\.show\([^)\n]*\)[ \t]*(?:#[^\n]*)?)?\s*$/;

export function toSql(source, engine) {
  const text = source || '';
  const found = firstLine(text);
  if (found) {
    const h = parseHeader(found.line);
    found.lines[found.index] = header(h.name, engine, h.flags);
    return found.lines.join('\n');
  }
  // drop a leading "# fill in the '<…>' values first" note: it's said again as an SQL comment
  const body = text.replace(/^# fill in[^\n]*\n/, '');
  const call = CALL.exec(body);
  if (call) {
    return header(call[1], engine) + '\n' + call[4].replace(/^\n+|\s+$/g, '');
  }
  return header(DEFAULT_NAME, engine) + (text.trim() ? '\n' + text : '\n');
}

export function toPython(source) {
  const text = source || '';
  const found = firstLine(text);
  if (!found) {
    return text;
  }
  const h = parseHeader(found.line);
  const before = found.lines.slice(0, found.index).join('\n');
  const sql = found.lines.slice(found.index + 1).join('\n').replace(/^\n+|\s+$/g, '');
  if (!sql) {
    return before;
  }
  const quote = sql.includes('"""') ? "'''" : '"""';
  const call = h.engine === 'duckduck' ? 'reader' : 'spark';
  const code = `${h.name} = ${call}.sql(${quote}\n${sql}\n${quote})\n${h.name}.show(20)`;
  return before ? before + '\n' + code : code;
}

export function convert(source, mode) {
  return mode === 'python' ? toPython(source) : toSql(source, mode);
}

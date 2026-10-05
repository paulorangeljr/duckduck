"""
The SQL tab's query as code to run on your own computer — a Python script and a Jupyter notebook.

``python_code(console, query, language)`` plans the query (``DuckAPI.explain``: nothing is read) to learn which
connectors and saved tables it reads, then writes what it takes to run the same thing elsewhere:

- ``install``: the ``pip install`` line, with the extras those connectors need (and the database driver);
- ``config``: a ``duckduck.json`` with only those connectors and saved tables — masked (``system.mask``): a
  secret written in the server's file shows as ``***``, never leaves it; credentials kept in AWS Secrets
  Manager / Azure Key Vault are read on your computer with your own login;
- ``script`` (``run_query.py``) and ``notebook`` (an ``.ipynb``, cells in ``cells``): ``DuckAPI().auto_register``
  + ``duck.sql(QUERY).df()`` — or ``run_kql`` for KQL —, the push-down check first in the notebook.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple

from ..system import MASK, mask

#: connector → the package extra it needs (base install covers the rest: files, python, servicenow, insightvm…)
CONNECTOR_EXTRAS = {"database": "database", "glue": "aws", "athena": "aws", "airflow": "aws", "adx": "adx"}
#: authentication type → the extra that reads its secrets
AUTH_EXTRAS = {"aws": "aws", "azure": "azure"}
#: SQLAlchemy URL scheme (dialect+driver) → the driver package, when it isn't an extra of its own
DRIVERS = {"oracle": "oracledb", "mssql+pyodbc": "pyodbc", "mssql+pymssql": "pymssql",
           "postgresql": "psycopg2-binary", "postgresql+psycopg2": "psycopg2-binary", "postgresql+psycopg": "psycopg",
           "mysql+pymysql": "PyMySQL", "mysql+mysqldb": "mysqlclient", "mariadb+pymysql": "PyMySQL",
           "databricks": "databricks-sqlalchemy", "snowflake": "snowflake-sqlalchemy"}

SCRIPT_NAME = "run_query.py"
NOTEBOOK_NAME = "query.ipynb"


def python_code(console: Any, query: str, language: str = "sql") -> Dict[str, Any]:
    """Everything to run ``query`` on another computer — see the module docstring."""
    language = "kql" if str(language).lower() == "kql" else "sql"
    query = (query or "").strip()
    duck = console.duck
    notes: List[str] = []
    explained = console.explain(query, language=language) if query else {"error": "no query"}
    if explained.get("error"):
        notes.append(f"The query doesn't plan here yet ({explained['error']}) — the code is written anyway, "
                     f"with every connector its text names.")
    sql = (explained.get("translation") or {}).get("sql") or query
    tables = _tables_read(console, explained, sql)
    services, views = _what_it_uses(duck, tables, query)
    config = _config_excerpt(console, services, views, language, notes)
    extras, packages = _extras(config)
    install = _install_lines(extras, packages)
    script = _script(query, language)
    cells = _cells(query, language, install)
    if any(MASK in json.dumps(block) for block in (config.get("services") or {}).values()):
        notes.append(f"Values shown as {MASK} are secrets written in the server's duckduck.json — they never leave "
                     f"it. Put yours in their place, or better, keep them in AWS Secrets Manager / Azure Key Vault "
                     f"(authentication type aws / azure): then the file holds no secret at all.")
    if language == "kql":
        notes.append("KQL needs the kql DuckDB extension on your computer (~/.duckduck/extensions/"
                     "kql.duckdb_extension, or DUCKDUCK_KQL_EXTENSION) — except a query that only reads one Azure "
                     "Data Explorer connection, which runs on the cluster as it is.")
    if _local_paths(config):
        notes.append("Some connectors read folders or files on the server (path / module): change those paths to "
                     "where the files are on your computer.")
    return {"language": language, "query": query, "tables": tables,
            "services": [{"name": s, "connector": (config.get("services", {}).get(s) or {}).get("connector", s)}
                         for s in services],
            "views": views, "extras": extras, "packages": packages, "install": install,
            "config": config, "config_text": json.dumps(config, indent=2, ensure_ascii=False),
            "script": script, "script_name": SCRIPT_NAME, "cells": cells,
            "notebook": _notebook(cells), "notebook_name": NOTEBOOK_NAME, "notes": notes}


# ---- what the query reads ------------------------------------------------------------------------------------

def _tables_read(console: Any, explained: Dict[str, Any], sql: str) -> List[str]:
    """Registered tables the plan calls — and, for a saved query among them, the tables it reads (the dry run
    doesn't open it)."""
    found: List[str] = []
    calls = ((explained.get("pushdown") or {}).get("calls")) or []
    for call in calls:
        if call.get("table") and call["table"] not in found:
            found.append(call["table"])
    if not calls and explained.get("error"):  # doesn't plan: every registered name its text mentions
        words = set(re.findall(r"[A-Za-z_][\w.]*", sql))
        found = [n for n in console.duck.functions if n in words or any(w.replace(".", "_") == n for w in words)]
    return found


def _what_it_uses(duck: Any, tables: List[str], query: str) -> Tuple[List[str], List[str]]:
    """(services, saved tables) behind ``tables`` — a saved query's own tables followed, once each."""
    services: List[str] = []
    views: List[str] = []
    view_key = getattr(duck, "view_key", {}) or {}
    defs = getattr(duck, "views", {}) or {}
    todo, seen = list(tables), set()  # type: List[str], Set[str]
    while todo:
        table = todo.pop(0)
        if table in seen:
            continue
        seen.add(table)
        name = view_key.get(table) or (table if table in defs else None)
        if name is not None:
            if name not in views:
                views.append(name)
            definition = defs.get(name) or {}
            if definition.get("table"):
                todo.append(definition["table"])
            elif definition.get("sql"):
                words = set(re.findall(r"[A-Za-z_][\w.]*", definition["sql"]))
                todo.extend(n for n in duck.functions
                            if n in words or any(w.replace(".", "_").lower() == n for w in words))
                todo.extend(_addressed(duck, definition["sql"]))
            continue
        service = (getattr(duck, "service_of", {}) or {}).get(table)
        if service and service not in ("saved tables", "taken over", "duckduck") and service not in services:
            services.append(service)
    return services, views


def _addressed(duck: Any, sql: str) -> List[str]:
    """The registered functions a saved query reads by address (``svc.x`` / ``svc.db.t``)."""
    try:
        from ..addresses import resolve_addresses

        resolved = resolve_addresses(duck, sql)
    except Exception:  # noqa: BLE001 — a best effort: the plan of the query itself already named the rest
        return []
    resolved = resolved[0] if isinstance(resolved, tuple) else resolved
    words = set(re.findall(r"[A-Za-z_]\w*", str(resolved)))
    return [n for n in duck.functions if n in words]


# ---- duckduck.json for the other computer ---------------------------------------------------------------------

def _config_excerpt(console: Any, services: List[str], views: List[str], language: str,
                    notes: List[str]) -> Dict[str, Any]:
    duck = console.duck
    path = getattr(console, "_config_path", None) or getattr(duck, "_config_path", None)
    data: Dict[str, Any] = {}
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            notes.append(f"couldn't read {path}: {exc}")
    configured = data.get("services") or getattr(duck, "configured_services", {}) or {}
    out: Dict[str, Any] = {"services": {s: configured[s] for s in services if s in configured}}
    missing = [s for s in services if s not in configured]
    if missing:
        notes.append(f"{', '.join(missing)}: registered in code on the server, not in a config file — register "
                     f"it the same way in your script.")
    file_views = data.get("views") or {}
    kept = {v: file_views.get(v) or (getattr(duck, "views", {}) or {}).get(v) for v in views}
    if kept:
        out["views"] = {v: d for v, d in kept.items() if d}
    for key in ("default_database",):
        if data.get(key):
            out[key] = data[key]
    if language == "kql" and data.get("kql"):
        out["kql"] = data["kql"]
    return mask(out)


def _extras(config: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    extras: List[str] = []
    packages: List[str] = []

    def add(bucket: List[str], item: Optional[str]) -> None:
        if item and item not in bucket:
            bucket.append(item)

    for name, block in (config.get("services") or {}).items():
        block = block or {}
        connector = block.get("connector") or name
        add(extras, CONNECTOR_EXTRAS.get(connector))
        add(extras, AUTH_EXTRAS.get(((block.get("authentication") or {}).get("type") or "").lower()))
        if connector == "database":
            scheme = _scheme(block)
            if scheme:
                driver = DRIVERS.get(scheme) or DRIVERS.get(scheme.split("+")[0])
                if driver == "oracledb":
                    add(extras, "oracle")
                else:
                    add(packages, driver)
    return extras, packages


def _scheme(block: Dict[str, Any]) -> Optional[str]:
    """The SQLAlchemy ``dialect+driver`` of a database service: its connection string or its ``drivername``."""
    auth = block.get("authentication") or {}
    for text in (block.get("connection_string"), auth.get("connection_string")):
        if isinstance(text, str) and "://" in text:
            return text.split("://", 1)[0].lower()
    for value in (block.get("drivername"), auth.get("drivername")):
        if isinstance(value, str) and not value.startswith("$secret."):
            return value.lower()
    return None


def _local_paths(config: Dict[str, Any]) -> bool:
    return any((block or {}).get("connector") in ("files", "python") for block in (config.get("services") or {}).values())


def _install_lines(extras: List[str], packages: List[str]) -> List[str]:
    """``pip install`` from a checkout of the repository the server runs from (the package isn't on PyPI)."""
    spec = f'".[{",".join(extras)}]"' if extras else "."
    repo = _repository()
    lines = [f"git clone {repo}" if repo else "git clone <the duckduck repository>", "cd duckduck",
             f"pip install -e {spec}" + (" " + " ".join(packages) if packages else "")]
    return lines


def _repository() -> Optional[str]:
    """The ``origin`` this package was installed from, without credentials — None when it isn't a git checkout
    or the remote isn't a public-looking URL (a local proxy, a file path)."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not os.path.isdir(os.path.join(root, ".git")):
        return None
    try:
        url = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    url = re.sub(r"//[^/@]*@", "//", url)  # never a token
    host = re.match(r"https?://([^/:]+)", url)
    if not host or host.group(1) in ("localhost", "127.0.0.1") or host.group(1).startswith("127."):
        return url if url.startswith("git@") else None
    return url if url.endswith(".git") else url + ".git"


# ---- the code ------------------------------------------------------------------------------------------------

def _literal(text: str) -> str:
    """The query as a Python string literal, as written: a raw triple-quoted string when it can be one."""
    if '"""' not in text and not text.endswith("\\"):
        return 'r"""\n' + text + '\n"""'
    return repr(text)


def _run_line(language: str) -> str:
    return "df = run_kql(duck, QUERY).df()" if language == "kql" else "df = duck.sql(QUERY).df()"


def _imports(language: str) -> str:
    return "from duckduck import DuckAPI" + ("\nfrom duckduck.kql import run_kql" if language == "kql" else "")


def _script(query: str, language: str) -> str:
    first = (query.splitlines() or [""])[0][:70]
    return f'''"""{first.replace(chr(92), "/").replace('"', "'")}{"…" if len(query) > len(first) else ""}

The query from the duckduck SQL tab. Run it with: python {SCRIPT_NAME}
duckduck.json (next to this file, or the path in DUCKDUCK_CONFIG) says which connectors to start.
"""
{_imports(language)}

QUERY = {_literal(query)}

duck = DuckAPI()  # DuckAPI(verbose=True) prints each call, what reached the source, and each page
duck.auto_register(config_path="duckduck.json")

{_run_line(language)}
print(df)
# df.to_csv("result.csv", index=False)   # or df.to_parquet("result.parquet")
'''


def _cells(query: str, language: str, install: List[str]) -> List[Dict[str, str]]:
    pip = install[-1].replace("pip install", "%pip install", 1)
    return [
        {"type": "markdown", "source": "# The query from the duckduck SQL tab\n\nRun the first cell once (from the "
                                       "duckduck folder), keep `duckduck.json` next to this notebook."},
        {"type": "code", "source": f"# once, from the duckduck checkout:\n# {pip}"},
        {"type": "code", "source": f"{_imports(language)}\n\nduck = DuckAPI()\n"
                                   f"duck.auto_register(config_path=\"duckduck.json\")"},
        {"type": "code", "source": f"QUERY = {_literal(query)}"},
        {"type": "markdown", "source": "What each source will be sent (WHERE, LIMIT, ORDER BY) — nothing is read yet:"},
        {"type": "code", "source": ("from duckduck.kql import default_translator\n\nSQL = default_translator()"
                                    ".to_sql(QUERY, duck).sql\nprint(duck.explain(SQL).report())")
         if language == "kql" else "print(duck.explain(QUERY).report())"},
        {"type": "code", "source": f"{_run_line(language)}\ndf"},
        {"type": "markdown", "source": "Keep going in pandas, or query the result again with DuckDB:\n\n"
                                       "```python\nimport duckdb\nduckdb.sql(\"SELECT count(*) FROM df\")\n```"},
    ]


def _notebook(cells: List[Dict[str, str]]) -> Dict[str, Any]:
    """An nbformat 4 notebook of ``cells`` — opens in Jupyter, VS Code, JupyterLab."""
    def cell(c: Dict[str, str]) -> Dict[str, Any]:
        lines = c["source"].splitlines(keepends=True)
        if c["type"] == "markdown":
            return {"cell_type": "markdown", "metadata": {}, "source": lines}
        return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": lines}

    return {"cells": [cell(c) for c in cells], "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                         "language_info": {"name": "python"}}}

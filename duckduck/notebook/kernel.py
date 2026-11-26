"""
duckduck's notebook kernels: a cell switched to SQL in the toolbar (or one starting with ``%%sql``) runs as SQL and
leaves its result as a DataFrame; everything else is ordinary code.

- ``duckduck_local`` — **a plain local kernel, no Spark and no AWS, like the SQL tab**: an IPython kernel with
  ``duck`` (a ``DuckAPI`` with your connectors already registered) ready in it. An SQL cell runs
  ``name = duck.sql(<the SQL>).df()`` — the same push-down as the SQL tab — and the answer is shown as an HTML
  table (``sqlcells.frame_result`` → ``sqlcells.render``). The DataFrame is kept under its name (``_sqldf`` is
  always the last one), and a pandas DataFrame named in the SQL is read as a table. This is the default kernel.
- ``duckduck_glue_pyspark`` — AWS's Glue PySpark kernel (``aws-glue-sessions``) with the same SQL cells, but each
  runs in a Glue interactive session: ``name = spark.sql(…)`` (``reader.sql`` for *SQL · duckduck*). SQL cells are
  turned into the code the session runs (``sqlcells.session_code``, printed back as ``MARK…END``).
  ``DUCKDUCK_GLUE_LOCAL=1`` (the ``duckduck_local_pyspark`` kernel) swaps the Glue API for ``LocalGateway``: the
  statements run on a local Spark in the kernel's own process. For trying notebooks without AWS, and for the tests.

    python -m duckduck.notebook.kernel install [--user] [--local]   # register them with Jupyter outside the web app
"""

import contextlib
import io
import os
import re
import sys
import threading
import traceback
from typing import Any, Dict, List, Optional

from . import sqlcells

KERNEL = "duckduck_glue_pyspark"
LOCAL_KERNEL = "duckduck_local_pyspark"
LOCAL_DUCK_KERNEL = "duckduck_local"
LOCAL_ENV = "DUCKDUCK_GLUE_LOCAL"


def kernel_specs() -> Dict[str, Dict[str, Any]]:
    """The kernel.json of each kernel this module offers (by name), for the ones whose packages are installed."""
    import importlib.util

    argv = [sys.executable, "-m", "duckduck.notebook.kernel", "-f", "{connection_file}"]
    specs: Dict[str, Dict[str, Any]] = {}
    if importlib.util.find_spec("ipykernel"):  # the local DuckDB kernel needs nothing but ipykernel + duckduck
        specs[LOCAL_DUCK_KERNEL] = {
            "argv": [sys.executable, "-m", "duckduck.notebook.kernel", "--duckdb", "-f", "{connection_file}"],
            "display_name": "DuckDB + SQL (duckduck)", "language": "python", "metadata": {"debugger": True}}
    if importlib.util.find_spec("aws_glue_interactive_sessions_kernel"):
        specs[KERNEL] = {"argv": argv, "display_name": "Glue PySpark + SQL (duckduck)", "language": "python",
                         "metadata": {"debugger": False}}
        if importlib.util.find_spec("pyspark"):
            specs[LOCAL_KERNEL] = {"argv": argv, "display_name": "Local PySpark + SQL (duckduck)",
                                   "language": "python", "env": {LOCAL_ENV: "1"}, "metadata": {"debugger": False}}
    return specs


def write_kernel_specs(folder: str) -> Dict[str, str]:
    """Each kernel spec as ``folder/<name>/kernel.json`` (with the Glue kernel's logos) → {name: its folder}."""
    import json
    import shutil

    written = {}
    logos = None
    try:
        import aws_glue_interactive_sessions_kernel.glue_pyspark as glue

        logos = os.path.dirname(glue.__file__)
    except ImportError:
        pass
    for name, spec in kernel_specs().items():
        target = os.path.join(folder, name)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "kernel.json"), "w", encoding="utf-8") as fh:
            json.dump(spec, fh, indent=1)
        for logo in ("logo-32x32.png", "logo-64x64.png"):
            if logos and os.path.isfile(os.path.join(logos, logo)):
                shutil.copy(os.path.join(logos, logo), target)
        written[name] = target
    return written


class LocalGateway:
    """The Glue API the kernel calls (``KernelGateway``), answered by a local Spark: one session, statements run
    in one namespace with ``spark`` / ``sc`` / ``glueContext`` (None) defined, printed output as the result."""

    def __init__(self) -> None:
        self.namespace: Dict[str, Any] = {"__name__": "__glue_session__"}
        self.statements: Dict[int, Dict[str, Any]] = {}
        self.session: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    # the parts of KernelGateway the Glue kernel uses
    def initialize_clients(self, *args: Any, **kwargs: Any) -> None:
        return None

    def is_glue_client_initialized(self) -> bool:
        return True

    def get_iam_role_using_sts(self, *args: Any, **kwargs: Any) -> str:
        return "arn:aws:iam::000000000000:role/local"

    def create_session(self, role: str, default_arguments: Any, new_session_id: str, command: Any,
                       **additional: Any) -> Dict[str, Any]:
        from pyspark.sql import SparkSession

        spark = (SparkSession.builder.master(os.environ.get("DUCKDUCK_LOCAL_SPARK_MASTER", "local[2]"))
                 .appName(new_session_id).config("spark.ui.enabled", "false").getOrCreate())
        self.namespace.update({"spark": spark, "sc": spark.sparkContext, "glueContext": None})
        self.session = {"Id": new_session_id, "Status": "READY", "Role": role}
        return {"Session": self.session}

    def get_session(self, session_id: str) -> Dict[str, Any]:
        return {"Session": self.session or {"Id": session_id, "Status": "STOPPED"}}

    def list_sessions(self) -> Dict[str, Any]:
        return {"Ids": [self.session["Id"]] if self.session else [], "Sessions": [self.session] if self.session else []}

    def stop_session(self, session_id: str) -> Dict[str, Any]:
        if self.session:
            self.session["Status"] = "STOPPED"
        return {}

    def delete_session(self, session_id: str) -> Dict[str, Any]:
        return self.stop_session(session_id)

    def run_statement(self, session_id: str, code: str) -> Dict[str, Any]:
        with self._lock:
            sid = len(self.statements) + 1
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    self._run(code, sid)
                output = {"Status": "ok", "Data": {"TextPlain": out.getvalue()}}
            except Exception as exc:  # noqa: BLE001 — reported like Glue does
                output = {"Status": "error", "ErrorName": type(exc).__name__, "ErrorValue": str(exc),
                          "Traceback": traceback.format_exception(type(exc), exc, exc.__traceback__),
                          "Data": {"TextPlain": out.getvalue()}}
            self.statements[sid] = {"Id": sid, "State": "AVAILABLE", "Output": output, "Code": code}
            return {"Id": sid}

    def _run(self, code: str, sid: int) -> None:
        """Like the session's REPL: the statements run, and the last one's value is printed when it's an expression."""
        import ast

        tree = ast.parse(code, f"<statement {sid}>")
        last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        exec(compile(tree, f"<statement {sid}>", "exec"), self.namespace)  # noqa: S102 — the user's cell
        if last is not None:
            value = eval(compile(ast.Expression(last.value), f"<statement {sid}>", "eval"), self.namespace)  # noqa: S307
            if value is not None:
                print(repr(value))

    def get_statement(self, session_id: str, statement_id: int) -> Dict[str, Any]:
        return {"Statement": self.statements[statement_id]}

    def cancel_statement(self, session_id: str, statement_id: Any) -> None:
        return None

    def list_statements(self, session_id: str) -> Dict[str, Any]:
        return {"Statements": list(self.statements.values())}

    def get_results(self, s3_path: str) -> str:
        return ""

    def get_tags(self, resource_arn: str) -> Dict[str, Any]:
        return {"Tags": {}}

    def tag_resource(self, resource_arn: str, tags: Any) -> None:
        return None

    def get_caller_identity(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"Account": "000000000000", "Arn": "arn:aws:iam::000000000000:user/local"}


def _kernel_class() -> Any:
    from aws_glue_interactive_sessions_kernel.glue_pyspark.GlueKernel import GlueKernel

    class DuckduckGlueKernel(GlueKernel):
        """The Glue PySpark kernel whose ``%%sql`` cells leave a DataFrame (see the module docstring)."""

        implementation = "duckduck Glue PySpark"

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            if os.environ.get(LOCAL_ENV) == "1":
                self.kernel_gateway = LocalGateway()
                self.set_session_id_prefix("local")

        async def do_execute(self, code: str, silent: bool, store_history: bool = True,
                             user_expressions: Any = None, allow_stdin: bool = False, *,
                             cell_meta: Optional[Dict[str, Any]] = None, cell_id: Optional[str] = None) -> Any:
            # an SQL cell holds only its SQL; its metadata (sent with the execution) makes it %%sql
            if cell_meta is None:  # an ipykernel that doesn't hand it over: the request's own
                try:
                    cell_meta = (self.get_parent("shell") or {}).get("metadata")
                except Exception:  # noqa: BLE001
                    cell_meta = None
            return await super().do_execute(sqlcells.from_metadata(code, cell_meta), silent, store_history,
                                            user_expressions, allow_stdin)

        def _handle_sql_code(self, lines: Any) -> Optional[str]:
            try:
                header, sql = sqlcells.split_cell(list(lines))
                return sqlcells.session_code(header, sql)
            except sqlcells.SqlCellError as exc:
                self._send_error_output(str(exc))
                return None

        def _send_output(self, output: Any) -> None:
            text = output if isinstance(output, str) else str(output)
            if sqlcells.MARK not in text:
                return super()._send_output(output)
            for kind, piece in sqlcells.split_output(text):
                if kind == "result":
                    # tag the output with the result's columns, so SQL-cell autocomplete can offer them
                    self.send_response(self.iopub_socket, "display_data",
                                       {"data": sqlcells.render(piece),
                                        "metadata": {sqlcells.METADATA_KEY: {
                                            "name": piece.get("name"),
                                            "columns": [c[0] for c in (piece.get("columns") or [])]}}})
                elif piece.strip():
                    super()._send_output(piece.rstrip("\n"))

    return DuckduckGlueKernel


# ---- the local DuckDB kernel: SQL cells run like the SQL tab (duck.sql), no Spark -------------------------------

def _mask_strings(sql: str) -> str:
    """``sql`` with string literals and comments blanked (same length), so a name inside one isn't matched."""
    for pattern, flags in ((r"'(?:[^']|'')*'", 0), (r'"(?:[^"]|"")*"', 0), (r"--[^\n]*", 0), (r"/\*.*?\*/", re.S)):
        sql = re.sub(pattern, lambda m: " " * len(m.group(0)), sql, flags=flags)
    return sql


def _register_frames(duck: Any, user_ns: Dict[str, Any], query: str) -> List[str]:
    """Each pandas DataFrame in the session named in ``query`` (and not a duckduck table) registered as a DuckDB
    view, so an SQL cell reads on from a previous cell's DataFrame — the local counterpart of Spark temp views."""
    conn = getattr(duck, "conn", None)
    if conn is None:
        return []
    try:
        import pandas as pd
    except Exception:  # noqa: BLE001 — pandas always present with duckduck, but never fail the query over it
        return []
    functions = getattr(duck, "functions", None) or {}
    masked = _mask_strings(query)
    views: List[str] = []
    for var, value in list(user_ns.items()):
        if (isinstance(var, str) and var.isidentifier() and not var.startswith("_") and var not in functions
                and isinstance(value, pd.DataFrame)
                and re.search(r"(?<![\w.`])" + re.escape(var) + r"(?![\w`])", masked)):
            try:
                conn.register(var, value)
                views.append(var)
            except Exception:  # noqa: BLE001 — a name DuckDB won't take: leave it to the query
                pass
    return views


def _sql_literal(value: Any) -> str:
    """A Python value rendered as a SQL literal, for ``{{ … }}`` interpolation in an SQL cell."""
    import datetime as _dt

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return "NULL" if value != value else repr(value)  # NaN → NULL
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (_dt.date, _dt.datetime)):
        return "'" + value.isoformat() + "'"
    if isinstance(value, (list, tuple, set)):
        return "(" + ", ".join(_sql_literal(v) for v in value) + ")"
    if hasattr(value, "columns") and hasattr(value, "iloc"):  # a DataFrame
        raise sqlcells.SqlCellError("a DataFrame goes in the query by its name directly (FROM my_df), not in {{ }}")
    if hasattr(value, "isoformat"):  # a pandas Timestamp and the like
        return "'" + value.isoformat() + "'"
    raise sqlcells.SqlCellError(
        f"a {type(value).__name__} isn't a value {{ }} can put in SQL — use a number, text, True/False, None, "
        "a date, or a list of those (a DataFrame goes in the FROM by its name)")


_INTERPOLATE = re.compile(r"\{\{\s*(.+?)\s*\}\}", re.S)


def _interpolate(sql: str, user_ns: Dict[str, Any]) -> str:
    """Replace each ``{{ expr }}`` in the cell with its Python value (evaluated in the kernel's namespace) as a SQL
    literal — so ``WHERE severity = {{ sev }}`` becomes ``WHERE severity = 'high'`` (write it unquoted: the value
    brings its own quotes). A DuckDB struct literal ``{...}`` (single braces) is left alone."""
    def repl(match: "re.Match[str]") -> str:
        expr = match.group(1)
        try:
            value = eval(expr, {**user_ns})  # noqa: S307 — the user's own kernel namespace, like any cell
        except sqlcells.SqlCellError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise sqlcells.SqlCellError(f"couldn't read {{{{ {expr} }}}}: {exc}")
        return _sql_literal(value)

    return _INTERPOLATE.sub(repl, sql)


def run_local(duck: Any, user_ns: Dict[str, Any], header: str, sql: str) -> Dict[str, Any]:
    """Run one ``%%sql`` cell on a local ``DuckAPI``: ``name = duck.sql(<sql>).df()``, the DataFrame kept in
    ``user_ns`` under its name and ``_sqldf``. Returns the result dict ``sqlcells.render`` shows. Pure (no
    ipykernel), so it is tested on a stand-in ``duck`` the way the Spark session code is."""
    options = sqlcells.parse_header(header)
    query = (sql or "").strip().rstrip(";").strip()
    if not query:
        raise sqlcells.SqlCellError("the cell has no SQL after %%sql")
    if duck is None:
        raise NameError("duck isn't defined: this kernel sets it up automatically — restart the kernel, or run "
                        "duck = DuckAPI().auto_register()")
    query = _interpolate(query, user_ns)  # {{ python_var }} → its value as a SQL literal
    views = _register_frames(duck, user_ns, query)
    frame = duck.sql(query).df()
    user_ns[options["name"]] = frame
    user_ns["_sqldf"] = frame
    options["views"] = views
    return sqlcells.frame_result(frame, options)


def _bootstrap_duck(shell: Any) -> None:
    """Put a ready ``duck`` (a ``DuckAPI`` with the config's connectors, ``DUCKDUCK_CONFIG``) in the kernel's
    namespace — so an SQL cell works at once, like the SQL tab. A config that doesn't load leaves an empty
    ``duck`` the user can register by hand; it never stops the kernel from starting."""
    try:
        from .. import DuckAPI
    except Exception as exc:  # noqa: BLE001
        print(f"duckduck couldn't be imported ({exc}); `duck` isn't set up", file=sys.stderr)
        return
    shell.user_ns.setdefault("DuckAPI", DuckAPI)
    try:  # pandas is a duckduck dependency, so it's here — put `pd` ready in the namespace, like `duck`,
        import pandas as pd  # so a `name = pd.DataFrame(...)` cell (the grid's, or the user's own) needs no import
        shell.user_ns.setdefault("pandas", pd)
        shell.user_ns.setdefault("pd", pd)
    except Exception:  # noqa: BLE001 — never stop the kernel over this
        pass
    duck = DuckAPI()
    try:
        duck.auto_register(config_path=os.environ.get("DUCKDUCK_CONFIG"), on_error="warn")
    except Exception as exc:  # noqa: BLE001 — no config found, a bad file: duck is still usable
        print(f"duckduck: no services registered yet ({exc}). Set DUCKDUCK_CONFIG, or run "
              "duck.auto_register(services=...).", file=sys.stderr)
    shell.user_ns["duck"] = duck


def _install_sql_magic(shell: Any, duck_getter: Any) -> None:
    """Register the ``%%sql`` cell magic on an IPython shell: run the cell with ``run_local`` and show the table."""
    def duckduck_sql(line: str, cell: str) -> None:
        from IPython.display import publish_display_data

        header = (sqlcells.HEADER + (" " + line if line.strip() else "")).strip()
        try:
            result = run_local(duck_getter(), shell.user_ns, header, cell)
        except sqlcells.SqlCellError as exc:
            print(str(exc), file=sys.stderr)
            return
        # tag the output with the result's columns (the name + each column), so SQL-cell autocomplete can offer them
        publish_display_data(
            sqlcells.render(result),
            metadata={sqlcells.METADATA_KEY: {"name": result.get("name"),
                                              "columns": [c[0] for c in (result.get("columns") or [])]}})

    shell.register_magic_function(duckduck_sql, "cell", "sql")


def _install_df_display(shell: Any) -> None:
    """Show every pandas DataFrame in this kernel with the duckduck toolbar (⤢ Expand / ⬇ CSV / ⬇ Excel / ▦ Chart /
    ✎ Edit) — the same one SQL-cell results get — instead of pandas' plain HTML. A DataFrame shown in a Python cell
    (the last expression, ``display(df)``, a bare ``df``) then carries the toolbar too. Falls back to pandas' own
    repr on anything odd, and never raises (a failing display formatter would break every cell's output)."""
    try:
        import pandas as pd

        formatter = shell.display_formatter.formatters.get("text/html")
    except Exception:  # noqa: BLE001 — pandas missing, or no HTML formatter: leave the kernel as it is
        return
    if formatter is None:
        return

    def to_html(frame: Any) -> Optional[str]:
        try:
            shown = sqlcells.dataframe_display(frame)
            if shown:
                return shown["text/html"]
        except Exception:  # noqa: BLE001 — never break display; fall through to pandas' own repr
            pass
        try:
            return frame._repr_html_()
        except Exception:  # noqa: BLE001
            return None

    try:
        formatter.for_type(pd.DataFrame, to_html)
    except Exception:  # noqa: BLE001
        pass


def _local_kernel_class() -> Any:
    from ipykernel.ipkernel import IPythonKernel

    class DuckduckLocalKernel(IPythonKernel):
        """A local IPython kernel with duckduck: SQL cells run ``duck.sql(…).df()`` and show the table."""

        implementation = "duckduck local DuckDB"
        banner = ("duckduck — local DuckDB. `duck` is ready (a DuckAPI with your connectors); switch a cell to SQL "
                  "in the toolbar to query it with duck.sql, exactly like the SQL tab.")

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            _bootstrap_duck(self.shell)
            _install_sql_magic(self.shell, lambda: self.shell.user_ns.get("duck"))
            _install_df_display(self.shell)

        async def do_execute(self, code: str, silent: bool, store_history: bool = True,
                             user_expressions: Any = None, allow_stdin: bool = False, *,
                             cell_meta: Optional[Dict[str, Any]] = None, cell_id: Optional[str] = None) -> Any:
            # an SQL cell holds only its SQL; its metadata (sent with the execution) turns it into %%sql code
            if cell_meta is None:
                try:
                    cell_meta = (self.get_parent("shell") or {}).get("metadata")
                except Exception:  # noqa: BLE001
                    cell_meta = None
            return await super().do_execute(sqlcells.from_metadata(code, cell_meta), silent, store_history,
                                            user_expressions, allow_stdin)

    return DuckduckLocalKernel


def local_notebook_template(kernel: str = LOCAL_DUCK_KERNEL) -> Dict[str, Any]:
    """A notebook for the local DuckDB kernel (nbformat 4): an intro, an SQL cell listing the tables, an empty cell."""
    def cell(kind: str, src: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {"cell_type": kind, "metadata": meta or {}, "source": src.splitlines(keepends=True) or [""]}
        if kind == "code":
            out.update({"execution_count": None, "outputs": []})
        return out

    intro = ("# duckduck — local DuckDB\n\n"
             "This notebook runs **locally**, with no Spark and no AWS. `duck` is a duckduck `DuckAPI` with your "
             "connectors already registered — query it just like the SQL tab.\n\n"
             "**SQL cells**: the toolbar's language switch turns a cell into SQL. It runs as "
             "`df = duck.sql(...).df()` (the same push-down as the SQL tab) and keeps the result as a pandas "
             "DataFrame — `%%sql name` names it, `_sqldf` is always the last one — so the next Python cell goes on "
             "from it. A DataFrame from an earlier cell, named in the SQL, is read as a table.\n\n"
             "**Python values in SQL**: write `{{ my_var }}` (double braces) anywhere in an SQL cell and the "
             "kernel puts that Python variable in as a SQL literal — `WHERE severity = {{ sev }}` becomes "
             "`WHERE severity = 'high'` (unquoted: text brings its own quotes; a list becomes `(a, b, c)` for "
             "`IN`). A whole DataFrame goes in by its name directly, not in `{{ }}`.\n\n"
             "Click a table in the list on the left to add a cell that reads it.")
    cells = [cell("markdown", intro),
             cell("code", "SHOW TABLES",
                  {"duckduck": {"language": "sql", "engine": "duckduck", "name": "tables", "auto": False}}),
             cell("code", "")]
    return {"nbformat": 4, "nbformat_minor": 5, "cells": cells,
            "metadata": {"kernelspec": {"name": kernel, "display_name": "DuckDB + SQL (duckduck)",
                                        "language": "python"},
                         "language_info": {"name": "python"}, "duckduck": {"cell_language": "python"}}}


def install(user: bool = True, prefix: Optional[str] = None, local: bool = False) -> Dict[str, str]:
    """Registers the kernel(s) with Jupyter (``jupyter kernelspec``) → {name: where}."""
    import tempfile

    from jupyter_client.kernelspec import KernelSpecManager

    manager = KernelSpecManager()
    done = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, folder in write_kernel_specs(tmp).items():
            if name == LOCAL_KERNEL and not local:
                continue
            done[name] = manager.install_kernel_spec(folder, name, user=user and prefix is None, prefix=prefix)
    return done


def main(argv: Optional[list] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["install"]:
        import argparse

        parser = argparse.ArgumentParser(prog="python -m duckduck.notebook.kernel install")
        parser.add_argument("--user", action="store_true", help="in your user's Jupyter folder (the default)")
        parser.add_argument("--prefix", help="under this Python prefix instead (e.g. sys.prefix)")
        parser.add_argument("--local", action="store_true", help="also the local-Spark kernel")
        args = parser.parse_args(argv[1:])
        for name, where in install(user=True, prefix=args.prefix, local=args.local).items():
            print(f"{name}: {where}")
        return
    from ipykernel.kernelapp import IPKernelApp

    local_duckdb = "--duckdb" in argv  # the local DuckDB kernel's spec passes it; ipykernel never should see it
    argv = [a for a in argv if a != "--duckdb"]
    IPKernelApp.launch_instance(argv=argv, kernel_class=_local_kernel_class() if local_duckdb else _kernel_class())


if __name__ == "__main__":
    main()


__all__ = ["KERNEL", "LOCAL_DUCK_KERNEL", "LOCAL_KERNEL", "LocalGateway", "install", "kernel_specs",
           "local_notebook_template", "run_local", "write_kernel_specs"]

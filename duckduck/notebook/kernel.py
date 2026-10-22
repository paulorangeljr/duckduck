"""
The duckduck Glue PySpark kernel: AWS's Glue PySpark kernel (``aws-glue-sessions``) with SQL cells that leave a
DataFrame behind.

Everything is the Glue kernel's own (the magics, the session, the statements) but SQL cells — a cell whose metadata
says SQL (the notebook's PySpark / SQL switch; ``cell_meta`` → ``sqlcells.from_metadata``) or that starts with
``%%sql``: where the Glue kernel runs ``spark.sql(…).show()``, this one sends ``sqlcells.session_code`` — ``name = spark.sql(…)``, DataFrames named
in the SQL as temporary views, ``_sqldf`` — and shows the answer as an HTML table (``sqlcells.render``).

``DUCKDUCK_GLUE_LOCAL=1`` (the ``duckduck_local_pyspark`` kernel) swaps the Glue API for ``LocalGateway``: the
statements run on a local Spark in the kernel's own process. For trying notebooks without AWS, and for the tests.

    python -m duckduck.notebook.kernel install [--user] [--local]   # register it with Jupyter outside the web app
"""

import contextlib
import io
import os
import sys
import threading
import traceback
from typing import Any, Dict, Optional

from . import sqlcells

KERNEL = "duckduck_glue_pyspark"
LOCAL_KERNEL = "duckduck_local_pyspark"
LOCAL_ENV = "DUCKDUCK_GLUE_LOCAL"


def kernel_specs() -> Dict[str, Dict[str, Any]]:
    """The kernel.json of each kernel this module offers (by name), for the ones whose packages are installed."""
    import importlib.util

    argv = [sys.executable, "-m", "duckduck.notebook.kernel", "-f", "{connection_file}"]
    specs: Dict[str, Dict[str, Any]] = {}
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
                    self.send_response(self.iopub_socket, "display_data",
                                       {"data": sqlcells.render(piece), "metadata": {}})
                elif piece.strip():
                    super()._send_output(piece.rstrip("\n"))

    return DuckduckGlueKernel


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

    IPKernelApp.launch_instance(kernel_class=_kernel_class())


if __name__ == "__main__":
    main()


__all__ = ["KERNEL", "LOCAL_KERNEL", "LocalGateway", "install", "kernel_specs", "write_kernel_specs"]

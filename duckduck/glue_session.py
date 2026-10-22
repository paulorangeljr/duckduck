"""
duckduck inside an AWS Glue interactive session.

The pieces:

- ``publish(target)`` — the duckduck wheel (built from this source tree) and ``duckduck.json`` uploaded to an S3
  prefix, so a Glue session installs the one and reads the other. A config holding literal secrets is refused:
  the session reads them from Secrets Manager with its own IAM role (``"authentication": {"type": "aws", …}``).
- ``connect(config)`` — run in the session (or anywhere with Spark): the config read (from ``s3://…`` or a path),
  every service registered, and a ``SparkReader`` over them → ``(duck, reader)``. ``reader.sql(…)`` answers with a
  Spark DataFrame; plain PySpark (``spark.table(…)``) works next to it.
- ``pyspark_code(duck, sql)`` — the cell a click on a table adds: ``spark.table("db.t")`` for a table of the Glue
  Data Catalog, else ``reader.sql(…)``.
- ``notebook_template(settings)`` — the ``.ipynb`` the web app's Notebook tab starts from: the Glue kernel's magics
  (role, version, workers, idle timeout, connections, the wheel) and the ``connect`` cell, from the config's
  ``notebook.glue`` block.

    python -m duckduck.glue_session publish s3://minhaempresa-artifacts/duckduck/ --config duckduck.json
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: the Glue session settings a notebook starts with (``notebook.glue`` in duckduck.json), with their defaults
GLUE_DEFAULTS: Dict[str, Any] = {
    "iam_role": None,            # the session's role: reads the wheel/config in S3, the secrets, the lake
    "region": None,
    "glue_version": "5.0",
    "worker_type": "G.1X",
    "number_of_workers": 2,
    "idle_timeout": 30,          # minutes — the session (and its bill) stops after this long idle
    "connections": [],           # Glue connections: a VPC path to the APIs and databases the connectors call
    "artifacts": None,           # s3://bucket/prefix/ where publish put the wheel and duckduck.json
    "extra_python_modules": [],  # more pip specs or s3:// wheels for the session
    "datalake_formats": "delta,iceberg",
    "profile": None,             # the AWS profile the kernel (on the server) uses to call Glue
}
GLUE_KEYS = frozenset(GLUE_DEFAULTS)
_S3 = re.compile(r"^s3://([^/]+)/?(.*)$")


class GlueError(RuntimeError):
    """Something about the Glue setup — the message says what to change."""


def _split_s3(uri: str) -> Tuple[str, str]:
    match = _S3.match(uri or "")
    if not match:
        raise GlueError(f"{uri!r} isn't an s3://bucket/key address")
    return match.group(1), match.group(2)


def _s3_client(client: Any = None, region: Optional[str] = None) -> Any:
    if client is not None:
        return client
    try:
        import boto3
    except ImportError:
        raise GlueError('S3 needs boto3: pip install "duckduck[aws]"') from None
    return boto3.client("s3", region_name=region)


def glue_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """``notebook.glue`` of a config, over the defaults (unknown keys refused, naming the known ones)."""
    block = ((config or {}).get("notebook") or {}).get("glue") or {}
    unknown = sorted(set(block) - GLUE_KEYS)
    if unknown:
        raise GlueError(f"notebook.glue: unknown key(s) {unknown} — it takes {sorted(GLUE_KEYS)}")
    settings = {**GLUE_DEFAULTS, **block}
    if isinstance(settings["connections"], str):
        settings["connections"] = [c.strip() for c in settings["connections"].split(",") if c.strip()]
    return settings


def wheel_name(version: Optional[str] = None) -> str:
    if version is None:
        from importlib.metadata import PackageNotFoundError, version as dist_version

        try:
            version = dist_version("duckduck")
        except PackageNotFoundError:
            version = "0.1.0"
    return f"duckduck-{version}-py3-none-any.whl"


def artifact_uris(artifacts: str, version: Optional[str] = None) -> Dict[str, str]:
    """Where ``publish`` puts the wheel and the config under an ``s3://…/`` prefix."""
    bucket, prefix = _split_s3(artifacts)
    prefix = prefix.rstrip("/") + "/" if prefix else ""
    return {"wheel": f"s3://{bucket}/{prefix}{wheel_name(version)}",
            "config": f"s3://{bucket}/{prefix}duckduck.json"}


# ---- connect: in the session ---------------------------------------------------------------------------------

def _local_config(config: str, s3_client: Any = None) -> str:
    """A path to the config — an ``s3://`` one downloaded to a temporary folder first."""
    if not str(config).startswith("s3://"):
        if not os.path.isfile(config):
            raise GlueError(f"no config file at {config}")
        return config
    bucket, key = _split_s3(config)
    folder = tempfile.mkdtemp(prefix="duckduck-")
    path = os.path.join(folder, os.path.basename(key) or "duckduck.json")
    try:
        _s3_client(s3_client).download_file(bucket, key, path)
    except Exception as exc:  # noqa: BLE001 — said with the fix
        raise GlueError(f"couldn't read {config}: {exc} — does the session's IAM role have s3:GetObject on it?") \
            from exc
    return path


def connect(config: str, spark: Any = None, on_error: str = "warn", s3_client: Any = None,
            quiet: bool = False) -> Tuple[Any, Any]:
    """The framework in a Spark session: ``duckduck.json`` (``s3://…`` or a path) registered, and a
    ``SparkReader`` over it. Services that don't start are warned about (``on_error``), not fatal — the others
    still work. Returns ``(duck, reader)``."""
    from . import DuckAPI
    from .spark import SparkReader

    path = _local_config(config, s3_client)
    duck = DuckAPI()
    duck.auto_register(config_path=path, on_error=on_error)
    if spark is None:
        try:
            from pyspark.sql import SparkSession
        except ImportError:
            raise GlueError("connect needs pyspark — in a Glue session it's there; locally: "
                            'pip install "duckduck[spark]"') from None
        spark = SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
    reader = SparkReader(spark, duck)
    if not quiet:
        started = sorted({s for s in (duck.service_of or {}).values() if s not in ("duckduck",)})
        failed = getattr(duck, "failed_services", {}) or {}
        broken = [f"{n} ({e.get('error', e) if isinstance(e, dict) else e})" for n, e in failed.items()]
        print(f"duckduck: {len(started)} service(s) ready ({', '.join(started) or 'none'})"
              + (f"; didn't start: {', '.join(broken)}" if broken else ""))
    return duck, reader


# ---- publish: from the server or a laptop ---------------------------------------------------------------------

@dataclass
class Published:
    wheel: str
    config: Optional[str]
    files: List[str] = field(default_factory=list)

    def report(self) -> str:
        lines = [f"wheel   {self.wheel}"] + ([f"config  {self.config}"] if self.config else [])
        lines.append(f"\nIn the notebook: %additional_python_modules {self.wheel}")
        if self.config:
            lines.append(f'                 duck, reader = connect("{self.config}")')
        return "\n".join(lines)


def _project_root() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.isfile(os.path.join(root, "pyproject.toml")):
        raise GlueError("publish builds the wheel from duckduck's source tree (a git checkout with pyproject.toml); "
                        "this one is installed without it — pass wheel= with a wheel you built")
    return root


def build_wheel(out_dir: str) -> str:
    """The duckduck wheel, built from this source tree (no dependencies: Glue installs those with pip)."""
    result = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q",
                             "-w", out_dir, _project_root()], capture_output=True, text=True)
    if result.returncode != 0:
        raise GlueError(f"building the wheel failed: {(result.stderr or result.stdout).strip()[-800:]}")
    wheels = [f for f in os.listdir(out_dir) if f.startswith("duckduck-") and f.endswith(".whl")]
    if not wheels:
        raise GlueError(f"pip built no duckduck wheel in {out_dir}")
    return os.path.join(out_dir, sorted(wheels)[-1])


def _literal_secrets(config: Dict[str, Any]) -> List[str]:
    """Settings whose value is a secret written in the file (what ``system.mask`` would hide)."""
    from .system import mask

    found: List[str] = []

    def walk(a: Any, b: Any, where: str) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for k in a:
                walk(a[k], b.get(k), f"{where}.{k}" if where else k)
        elif isinstance(a, list) and isinstance(b, list):
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{where}[{i}]")
        elif a != b:
            found.append(where)

    walk(config, mask(config), "")
    return found


def publish(target: str, config_path: Optional[str] = None, wheel: Optional[str] = None, s3_client: Any = None,
            include_config: bool = True, allow_secrets: bool = False) -> Published:
    """The wheel (``wheel`` or built now) and ``config_path`` uploaded under ``target`` (``s3://bucket/prefix/``).
    The config is refused when it holds literal secrets (``allow_secrets`` to upload anyway): the session reads
    them from Secrets Manager with its IAM role."""
    bucket, prefix = _split_s3(target)
    prefix = prefix.rstrip("/") + "/" if prefix else ""
    s3 = _s3_client(s3_client)
    uploaded: List[str] = []
    config_uri = None
    if include_config:
        config_path = config_path or os.environ.get("DUCKDUCK_CONFIG") or "duckduck.json"
        if not os.path.isfile(config_path):
            raise GlueError(f"no config at {config_path} (pass config_path=, or include_config=False)")
        with open(config_path, encoding="utf-8") as fh:
            data = json.load(fh)
        secrets = _literal_secrets(data)
        if secrets and not allow_secrets:
            raise GlueError(f"{config_path} has secrets written in it ({', '.join(secrets[:5])}"
                            f"{' …' if len(secrets) > 5 else ''}) — keep them in Secrets Manager "
                            '("authentication": {"type": "aws", "secret_id": …}) so the Glue role reads them, '
                            "or pass allow_secrets=True")
    with tempfile.TemporaryDirectory() as tmp:
        local_wheel = wheel or build_wheel(tmp)
        wheel_key = prefix + os.path.basename(local_wheel)
        s3.upload_file(local_wheel, bucket, wheel_key)
        uploaded.append(f"s3://{bucket}/{wheel_key}")
    if include_config:
        key = prefix + "duckduck.json"
        s3.upload_file(config_path, bucket, key)
        config_uri = f"s3://{bucket}/{key}"
        uploaded.append(config_uri)
    return Published(wheel=uploaded[0], config=config_uri, files=uploaded)


# ---- the notebook a session starts from ------------------------------------------------------------------------

def _cell(kind: str, source: str) -> Dict[str, Any]:
    cell: Dict[str, Any] = {"cell_type": kind, "metadata": {}, "source": source.splitlines(keepends=True)}
    if kind == "code":
        cell.update({"execution_count": None, "outputs": []})
    return cell


def magics(settings: Dict[str, Any], version: Optional[str] = None) -> str:
    """The Glue kernel's line magics for these settings (one per line, the ones that are set)."""
    lines = []
    for key, magic in (("profile", "%profile"), ("region", "%region"), ("iam_role", "%iam_role"),
                       ("glue_version", "%glue_version"), ("worker_type", "%worker_type"),
                       ("number_of_workers", "%number_of_workers"), ("idle_timeout", "%idle_timeout")):
        if settings.get(key) not in (None, ""):
            lines.append(f"{magic} {settings[key]}")
    if settings.get("connections"):
        lines.append(f"%connections {','.join(settings['connections'])}")
    modules = list(settings.get("extra_python_modules") or [])
    if settings.get("artifacts"):
        modules.insert(0, artifact_uris(settings["artifacts"], version)["wheel"])
    if modules:
        lines.append(f"%additional_python_modules {','.join(modules)}")
    return "\n".join(lines)


def notebook_template(settings: Dict[str, Any], kernel: str = "duckduck_glue_pyspark", version: Optional[str] = None,
                      config_uri: Optional[str] = None) -> Dict[str, Any]:
    """A notebook ready to start a Glue session with duckduck in it (nbformat 4)."""
    if config_uri is None and settings.get("artifacts"):
        config_uri = artifact_uris(settings["artifacts"], version)["config"]
    configure = {"--enable-glue-datacatalog": "true"}
    if settings.get("datalake_formats"):
        configure["--datalake-formats"] = settings["datalake_formats"]
    missing = [k for k in ("iam_role", "artifacts") if not settings.get(k)]
    intro = ("# duckduck on AWS Glue\n\n"
             "The first cell sets up the Glue interactive session; the session starts when the first code cell "
             "runs, and stops after the idle timeout. Then `reader.sql(...)` reads any duckduck table as a Spark "
             "DataFrame, and plain PySpark (`spark.table(...)`) works next to it.\n\n"
             "Click a table in the list on the left to add a cell that reads it.\n\n"
             "**SQL cells**: the toolbar's *PySpark / SQL* switch turns a cell into SQL (`%%sql` on its first line). "
             "It runs as `df = spark.sql(...)` — `%%sql name` keeps it under another name, `_sqldf` is always the "
             "last one — so the next PySpark cell goes on from the DataFrame. A DataFrame variable named in the SQL "
             "is read as a table. *SQL · duckduck* reads through duckduck instead (`reader.sql`).")
    if missing:
        intro += (f"\n\n**Set {' and '.join('`notebook.glue.' + k + '`' for k in missing)} in duckduck.json** "
                  "(or edit the cell below).")
    connect_src = ("from duckduck.glue_session import connect\n\n"
                   f'duck, reader = connect("{config_uri or "s3://<bucket>/<prefix>/duckduck.json"}")')
    cells = [
        _cell("markdown", intro),
        _cell("code", magics(settings, version) or "%glue_version 5.0"),
        _cell("code", "%%configure\n" + json.dumps(configure, indent=2)),
        _cell("code", connect_src),
        _cell("code", ""),
    ]
    display = {"glue_pyspark": "Glue PySpark", "glue_spark": "Glue Spark",
               "duckduck_glue_pyspark": "Glue PySpark + SQL (duckduck)",
               "duckduck_local_pyspark": "Local PySpark + SQL (duckduck)"}.get(kernel, kernel)
    language = {"glue_spark": "scala"}.get(kernel, "python")
    return {"nbformat": 4, "nbformat_minor": 5, "cells": cells,
            "metadata": {"kernelspec": {"name": kernel, "display_name": display, "language": language},
                         "language_info": {"name": language}}}


_LIMIT = re.compile(r"\s+limit\s+(\d+)\s*;?\s*$", re.I)
_ONE_TABLE = re.compile(r"^\s*select\s+\*\s+from\s+([A-Za-z_]\w*)\s*(?:\((.*)\))?\s*"
                        r"(?:(?:as\s+)?[A-Za-z_]\w*\s*)?(?:where\s+(.+?))?\s*;?\s*$", re.I | re.S)
_GLUE_CATALOG = {("duckduck.connectors.lake.glue", "GlueTable", "table"),
                 ("duckduck.connectors.databases.athena", "Athena", "table")}


def _variable(name: str) -> str:
    import keyword

    name = re.sub(r"\W+", "_", name.split(".")[-1].strip('"')).strip("_").lower() or "df"
    if name[0].isdigit():
        name = "t_" + name
    return name + "_df" if keyword.iskeyword(name) or name in ("spark", "reader", "duck") else name


_ARG = re.compile(r"^\s*args?\.(\w+)\s*=\s*'([^']*)'\s*$", re.I)


def _split_where(where: Optional[str]) -> Tuple[Dict[str, str], Optional[str]]:
    """``arg.x = 'v'`` parts of a WHERE (ANDed) → ({x: v}, what's left); an OR anywhere → nothing taken."""
    if not where:
        return {}, None
    if re.search(r"\bor\b", where, re.I):
        return {}, where.strip()
    args, rest = {}, []
    for part in re.split(r"\s+and\s+", where.strip(), flags=re.I):
        match = _ARG.match(part)
        if match:
            args[match.group(1).lower()] = match.group(2)
        else:
            rest.append(part.strip())
    return args, " AND ".join(rest) or None


def _glue_table(duck: Any, name: str, args_text: Optional[str], args: Dict[str, str]) -> Optional[str]:
    """``database.table`` when the call reads a table of the Glue Data Catalog (Glue, Athena), else None."""
    kwargs: Dict[str, Any] = {**(duck._parse_kwargs(args_text) if args_text else {}), **args}
    saved = (getattr(duck, "view_key", None) or {}).get(name.lower())
    if saved is not None:
        definition = (getattr(duck, "views", None) or {}).get(saved) or {}
        if "table" not in definition:
            return None
        name, kwargs = definition["table"], {**(definition.get("args") or {}), **kwargs}
    fn = (getattr(duck, "functions", None) or {}).get(name)
    owner = getattr(fn, "__self__", None)
    if owner is None or (type(owner).__module__, type(owner).__name__, getattr(fn, "__name__", "")) not in _GLUE_CATALOG:
        return None
    database, table = kwargs.get("database"), kwargs.get("table_name")
    if not all(isinstance(v, str) and re.match(r"^[\w-]+$", v) for v in (database, table)):
        return None
    return f"{database}.{table}"


def pyspark_code(duck: Any, sql: str) -> Dict[str, str]:
    """A notebook cell that reads what ``sql`` reads, in a Glue session → ``{code, how}``.

    A plain ``SELECT * FROM <table> [WHERE …]`` over a table of the Glue Data Catalog (a ``glue`` or ``athena``
    table, or a saved table over one) becomes ``spark.table("db.table")`` — Spark reads the files itself, no
    duckduck in between; anything else ``reader.sql(…)``, which reads through duckduck as the SQL tab does (the
    same push-down, and the connectors' credentials in the session).
    """
    from .addresses import resolve_addresses

    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise GlueError("no query to turn into a cell")
    limit = _LIMIT.search(sql)
    body = sql[:limit.start()] if limit else sql
    try:
        resolved = resolve_addresses(duck, body)
    except ValueError:
        resolved = body
    match = _ONE_TABLE.match(resolved)
    table, where = None, None
    if match:
        args, where = _split_where(match.group(3))
        try:
            table = _glue_table(duck, match.group(1), match.group(2), args)
        except (SyntaxError, ValueError):
            table = None
    written = re.search(r"\bfrom\s+([^\s(]+)", body, re.I)
    var = _variable(table or (written.group(1) if written else "df"))
    if table:
        code = f'{var} = spark.table("{table}")'
        if where:
            code += f'.where("""{where.strip()}""")'
        code += f"  # read by Spark from the Glue Data Catalog\n{var}.show(20)"
        sql_cell = f"%%sql {var}\nSELECT * FROM {table}" + (f"\nWHERE {where.strip()}" if where else "")
        return {"code": code, "how": "spark.table", "sql": sql_cell}
    text = sql.replace('"""', '\\"\\"\\"')
    note = f"  # LIMIT {limit.group(1)}: a first look — take it out to read every row" if limit else ""
    fill = "# fill in the '<…>' values first\n" if re.search(r"'<[^'>]+>'", sql) else ""
    code = (fill + f'{var} = reader.sql("""\n{text}\n""")' + note
            + f"\n{var}.show(20)  # read through duckduck: the push-down of the SQL tab")
    sql_cell = (f"%%sql {var} --duckduck\n" + ("-- fill in the '<…>' values first\n" if fill else "") + sql)
    return {"code": code, "how": "reader.sql", "sql": sql_cell}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m duckduck.glue_session", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pub = sub.add_parser("publish", help="upload the duckduck wheel and duckduck.json to S3")
    pub.add_argument("target", help="s3://bucket/prefix/")
    pub.add_argument("--config", default=None, help="duckduck.json (default: DUCKDUCK_CONFIG or ./duckduck.json)")
    pub.add_argument("--wheel", default=None, help="a wheel already built (default: build one from this tree)")
    pub.add_argument("--no-config", action="store_true", help="upload only the wheel")
    pub.add_argument("--allow-secrets", action="store_true", help="upload a config with secrets written in it")
    args = parser.parse_args(argv)
    try:
        done = publish(args.target, args.config, wheel=args.wheel, include_config=not args.no_config,
                       allow_secrets=args.allow_secrets)
    except GlueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(done.report())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

__all__ = ["GLUE_DEFAULTS", "GlueError", "Published", "artifact_uris", "build_wheel", "connect", "glue_settings",
           "magics", "notebook_template", "publish", "pyspark_code"]

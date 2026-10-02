"""
``python -m duckduck.pipeline plan|run|sip`` — the same functions a script
calls (``plan_pipeline``, ``run_pipeline``, ``read_sip``), the same reports.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict, List, Optional

from ..common.logs import set_verbose
from .domains import Pipelines
from .sip import read_sip
from .spec import PipelineError


def _params(pairs: Optional[List[str]]) -> Dict[str, str]:
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise PipelineError(f"--param takes name=value (got {pair!r})")
        k, v = pair.split("=", 1)
        out[k.strip()] = v
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m duckduck.pipeline", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_ in (("plan", "show what a run would do, without running"), ("run", "run a pipeline file")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("pipeline", help="the pipeline's JSON file (a pipeline, or a domain of tables)")
        p.add_argument("--table", action="append", metavar="NAME", help="only this table of a domain file "
                                                                         "(repeatable; default: every table)")
        p.add_argument("--param", action="append", metavar="NAME=VALUE", help="a value for {{ NAME }}")
        p.add_argument("--config", help="duckduck.json with the connectors and the lake settings (default: "
                                        "DUCKDUCK_CONFIG / duckduck.json)")
        if name == "run":
            p.add_argument("--run-id", help="this run's id (default: the time + a random suffix)")
            p.add_argument("--dry-run", action="store_true", help="run the views and the sip, write nothing")
            p.add_argument("-v", "--verbose", action="count", default=0, help="-v progress, -vv debug")
    s = sub.add_parser("sip", help="show the sip kept in a store")
    s.add_argument("store", help="the sip.store folder (s3://lake/_sip/ or a local folder)")
    s.add_argument("--pipeline", help="only this pipeline")
    s.add_argument("--key", help="only this key's way")
    s.add_argument("--run-id", help="only this run")
    s.add_argument("--config", help="duckduck.json whose lake.aws reads the store (default: DUCKDUCK_CONFIG / "
                                    "duckduck.json)")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            domain = Pipelines(config=args.config).domain(args.pipeline)
            print("\n".join(domain.plan(t, params=_params(args.param)).report()
                            for t in args.table or domain.tables))
        elif args.command == "run":
            if args.verbose:
                set_verbose("debug" if args.verbose > 1 else "info")
            domain = Pipelines(config=args.config).domain(args.pipeline)
            runs = domain.run(*(args.table or ()), params=_params(args.param), run_id=args.run_id,
                              dry_run=args.dry_run)
            print(runs.report())
        else:
            from .settings import lake_settings

            df = read_sip(args.store, pipeline=args.pipeline, key=args.key, run_id=args.run_id,
                          aws=lake_settings(config_path=args.config).get("aws"))
            cols = ["run_id", "stage", "position", "key", "stage_key", "event", "n", "changed", "note"]
            print(df[cols].to_string(index=False) if len(df) else "no sip events there")
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

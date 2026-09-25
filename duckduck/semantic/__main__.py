"""
Command line for ``duckduck.semantic`` — a thin wrapper over the Python
functions in ``duckduck.semantic.commands`` (``ask``, ``generate_catalog``,
``jev_check``), which do exactly the same from a script or notebook. All
commands read ``duckduck.json`` (or ``--config``): connectors from
``services``, everything else from its ``semantic`` section.

    python -m duckduck.semantic generate-catalog [--force [NAME ...]] [--only NAME ...] [--out FILE]
    python -m duckduck.semantic ask "Which users accessed github in the last 24hrs?"
    python -m duckduck.semantic jev-check
    python -m duckduck.semantic calibrate examples/semantic/evaluation.json
"""

import argparse
import json
import sys

from .commands import ask, calibrate, generate_catalog, jev_check


def cmd_calibrate(args) -> int:
    report = calibrate(args.dataset, config_path=args.config, verbose=args.verbose,
                       cost_wrong=args.cost_wrong, cost_ask=args.cost_ask)
    print(report.summary())
    return 0


def cmd_generate(args) -> int:
    force = False if args.force is None else (args.force or True)  # bare --force → everything
    result = generate_catalog(config_path=args.config, out=args.out, verbose=args.verbose, force=force,
                              only=args.only or None)
    print(result.summary())
    return 0


def cmd_ask(args) -> int:
    result = ask(args.question, config_path=args.config, verbose=args.verbose, execute=not args.plan_only)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(result.report())
    return 0 if result.status in ("ok", "planned") else 1


def cmd_jev_check(args) -> int:
    try:
        result = jev_check(config_path=args.config, verbose=args.verbose)
    except ValueError as exc:
        print(exc)
        return 1
    print("Jev OK:", result.ranked())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m duckduck.semantic")
    parser.add_argument("--config", help="config file (default: duckduck.json lookup)")
    parser.add_argument("-v", "--verbose", nargs="?", const="info", default=None, choices=["info", "debug"],
                        help="log API calls, push-down decisions and pagination progress (default level: info)")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-catalog", help="bring the semantic catalog up to date with the LLM")
    gen.add_argument("--out", help="catalog file to update (default: output_path, else catalog_path)")
    gen.add_argument(
        "--force", nargs="*", metavar="NAME",
        help="redraft even if not expired: every generated source (no NAME), or exactly the tables "
             "matching each NAME — a table, a service (glue), or service:table (glue:security.proxy_logs); "
             "fnmatch patterns; hand-written ones included",
    )
    gen.add_argument(
        "--only", nargs="+", metavar="NAME",
        help="only look at these tables this run (same selectors as --force): new/expired among them",
    )
    gen.set_defaults(fn=cmd_generate, python=generate_catalog)

    cal = sub.add_parser("calibrate", help="suggest decision thresholds from labeled questions")
    cal.add_argument("dataset", help="JSON list of {question, expected_entity/activity/sources} (like evaluation.json)")
    cal.add_argument("--cost-wrong", type=float, default=5.0, help="cost of accepting a wrong decision (default 5)")
    cal.add_argument("--cost-ask", type=float, default=1.0, help="cost of sending a right one back to the user (default 1)")
    cal.set_defaults(fn=cmd_calibrate, python=calibrate)

    q = sub.add_parser("ask", help="answer a question")
    q.add_argument("question")
    q.add_argument("--plan-only", action="store_true", help="plan without executing")
    q.add_argument("--json", action="store_true", help="print the full result as JSON")
    q.set_defaults(fn=cmd_ask, python=ask)

    check = sub.add_parser("jev-check", help="make one Jev call to verify the key/network")
    check.set_defaults(fn=cmd_jev_check, python=jev_check)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

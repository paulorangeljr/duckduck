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
    python -m duckduck.semantic serve [--host H] [--port P]
    python -m duckduck.semantic feedback-report
    python -m duckduck.semantic feedback-to-eval [--out FILE]
    python -m duckduck.semantic feedback-suggest [--accept ID ...] [--dismiss ID ...]
    python -m duckduck.semantic feedback-export [--out FILE] [--since 7d] [--redact]
"""

import argparse
import json
import sys

from .commands import (ask, calibrate, feedback_export, feedback_report, feedback_suggest, feedback_to_eval,
                       generate_catalog, jev_check, serve)


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
    if args.interactive:
        return _converse(args)
    result = ask(args.question, config_path=args.config, verbose=args.verbose, execute=not args.plan_only)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(result.report())
    return 0 if result.status in ("ok", "planned") else 1


def _converse(args, read=None) -> int:
    """``ask --interactive``: answer the clarifications at the prompt until it's answered (empty line quits)."""
    from .commands import connect

    read = read or input
    from .engine import SemanticSearch

    duck = connect(args.config, args.verbose)
    conversation = SemanticSearch.from_config(duck, args.config).conversation(args.question, execute=not args.plan_only)
    while not conversation.done:
        print(conversation.result.report())
        reply = read("> ").strip()
        if not reply:
            break
        conversation.answer(reply)
        if conversation.history[-1]["understood"] is None:
            print("(didn't get that — answer with a number or an option's name)")
    print(conversation.result.report())
    return 0 if conversation.result.status in ("ok", "planned") else 1


def cmd_jev_check(args) -> int:
    try:
        result = jev_check(config_path=args.config, verbose=args.verbose)
    except ValueError as exc:
        print(exc)
        return 1
    print("Jev OK:", result.ranked())
    return 0


def cmd_serve(args) -> int:
    serve(config_path=args.config, host=args.host, port=args.port, verbose=args.verbose)
    return 0


def cmd_feedback_report(args) -> int:
    print(feedback_report(config_path=args.config, verbose=args.verbose).summary())
    return 0


def cmd_feedback_to_eval(args) -> int:
    print(feedback_to_eval(config_path=args.config, out=args.out, verbose=args.verbose).summary())
    return 0


def cmd_feedback_suggest(args) -> int:
    print(feedback_suggest(config_path=args.config, accept=args.accept, dismiss=args.dismiss,
                           verbose=args.verbose).summary())
    return 0


def cmd_feedback_export(args) -> int:
    print(feedback_export(config_path=args.config, out=args.out, since=args.since, limit=args.limit,
                          include_partial=not args.not_answered_only, redact=args.redact,
                          verbose=args.verbose).summary())
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
    q.add_argument("-i", "--interactive", action="store_true",
                   help="when it needs clarification, ask you at the prompt and continue with your answer")
    q.set_defaults(fn=cmd_ask, python=ask)

    check = sub.add_parser("jev-check", help="make one Jev call to verify the key/network")
    check.set_defaults(fn=cmd_jev_check, python=jev_check)

    srv = sub.add_parser("serve", help="the web app: ask, answer clarifications, rate answers, review suggestions")
    srv.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: this machine only)")
    srv.add_argument("--port", type=int, default=8765)
    srv.set_defaults(fn=cmd_serve, python=serve)

    rep = sub.add_parser("feedback-report", help="answer rates and what went wrong, from the feedback")
    rep.set_defaults(fn=cmd_feedback_report, python=feedback_report)

    fev = sub.add_parser("feedback-to-eval", help="rated questions → evaluation set, metrics, suggested thresholds")
    fev.add_argument("--out", help="where to write the evaluation set (default: feedback_evaluation.json)")
    fev.set_defaults(fn=cmd_feedback_to_eval, python=feedback_to_eval)

    sug = sub.add_parser("feedback-suggest", help="catalog suggestions from the feedback; accept or dismiss them")
    sug.add_argument("--accept", nargs="+", metavar="ID", help="apply these suggestions")
    sug.add_argument("--dismiss", nargs="+", metavar="ID", help="set these aside")
    sug.set_defaults(fn=cmd_feedback_suggest, python=feedback_suggest)

    exp = sub.add_parser("feedback-export", help="the questions that still fail, as a brief for a developer (Markdown)")
    exp.add_argument("--out", help="where to write it (default: feedback_export.md)")
    exp.add_argument("--since", help="only feedback since a date (2026-09-01) or an age (7d, 2w)")
    exp.add_argument("--limit", type=int, default=50, help="at most this many question patterns (default 50)")
    exp.add_argument("--not-answered-only", action="store_true", help="leave out 'partly answered'")
    exp.add_argument("--redact", action="store_true", help="question templates instead of questions; no SQL")
    exp.set_defaults(fn=cmd_feedback_export, python=feedback_export)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

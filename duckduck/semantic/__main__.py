"""
Command line for ``duckduck.semantic``. All commands read ``duckduck.json``
(or ``--config``): connectors from ``services``, everything else from its
``semantic`` section.

    python -m duckduck.semantic generate-catalog [--out semantic_catalog.yaml]
    python -m duckduck.semantic ask "Which users accessed github in the last 24hrs?"
    python -m duckduck.semantic jev-check
"""

import argparse
import json
import sys


def _duck(args):
    from duckduck import DuckAPI

    duck = DuckAPI(verbose=args.verbose)
    duck.auto_register(config_path=args.config, on_error="warn")
    return duck


def cmd_generate(args) -> int:
    from .config import SemanticConfig

    duck = _duck(args)
    cfg = SemanticConfig.load(duck, args.config)
    result = cfg.build_generator(duck).generate(cfg.catalog_generation.tables)
    out = args.out or cfg.path(cfg.catalog_generation.output_path)
    result.write(out)
    print(f"wrote {out}: {len(result.catalog.sources)} sources, "
          f"{len(result.catalog.relationships)} relationships")
    for w in result.warnings:
        print("  note:", w)
    return 0


def cmd_ask(args) -> int:
    from .engine import SemanticSearch

    search = SemanticSearch.from_config(_duck(args), args.config)
    result = search.search(args.question, execute=not args.plan_only)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0 if result.status in ("ok", "planned") else 1
    print(f"[{result.status}] ({result.elapsed_ms:.0f} ms)")
    for d in result.decisions:
        print(f"  {d.kind:<24} {str(d.subject)[:40]:<40} {str(d.answer):<20} {d.probability:.2f}")
    if result.clarification:
        print("\n" + result.clarification)
        return 1
    print("\n" + result.sql)
    if result.results is not None:
        print("\n" + result.results.to_string(index=False))
    return 0


def cmd_jev_check(args) -> int:
    """One real Jev call — checks the key, network access and response parsing."""
    from duckduck import DuckAPI

    from .config import SemanticConfig
    from .decisions import DecisionState

    duck = DuckAPI()
    cfg = SemanticConfig.load(duck, args.config)
    if cfg.decision_engine.type != "jev":
        print("semantic.decision_engine.type isn't 'jev'")
        return 1
    engine = cfg.build_engine(duck)
    result = engine.classify(
        DecisionState(query="Which users accessed github in the last 24hrs?"),
        "What entity is the user asking for?",
        {"user": "A person or account", "host": "A machine", "domain": "A website"},
    )
    print("Jev OK:", result.ranked())
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m duckduck.semantic")
    parser.add_argument("--config", help="config file (default: duckduck.json lookup)")
    parser.add_argument("-v", "--verbose", nargs="?", const="info", default=None, choices=["info", "debug"],
                        help="log API calls, push-down decisions and pagination progress (default level: info)")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-catalog", help="draft the semantic catalog with the LLM")
    gen.add_argument("--out", help="output YAML (default: catalog_generation.output_path)")
    gen.set_defaults(fn=cmd_generate)

    ask = sub.add_parser("ask", help="answer a question")
    ask.add_argument("question")
    ask.add_argument("--plan-only", action="store_true", help="plan without executing")
    ask.add_argument("--json", action="store_true", help="print the full result as JSON")
    ask.set_defaults(fn=cmd_ask)

    check = sub.add_parser("jev-check", help="make one Jev call to verify the key/network")
    check.set_defaults(fn=cmd_jev_check)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

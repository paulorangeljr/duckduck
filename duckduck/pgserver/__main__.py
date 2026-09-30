"""``python -m duckduck.pgserver [--config duckduck.json] [--host 127.0.0.1] [--port 5433]``"""

import argparse
import logging
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m duckduck.pgserver",
                                     description="duckduck's tables over the PostgreSQL protocol, for any SQL client.")
    parser.add_argument("--config", help="duckduck.json (default: DUCKDUCK_CONFIG, else ./duckduck.json)")
    parser.add_argument("--host", help="address to listen on (default 127.0.0.1; pg_server.host)")
    parser.add_argument("--port", type=int, help="port (default 5433; pg_server.port)")
    parser.add_argument("--database", help="the database name clients connect to (default duckduck)")
    parser.add_argument("--allow-saved-tables", action="store_true", default=None,
                        help="CREATE / DROP / ALTER / COMMENT ON VIEW from SQL clients save tables into duckduck.json "
                             "(pg_server.allow_saved_tables)")
    parser.add_argument("-v", "--verbose", nargs="?", const="info", help="log what each query does (info / debug)")
    args = parser.parse_args(argv)

    from duckduck import DuckAPI
    from duckduck.pgserver import PGServer

    duck = DuckAPI(verbose=args.verbose)
    if args.verbose is None:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logging.getLogger("duckduck").setLevel(logging.WARNING)
        logging.getLogger("duckduck.pgserver").setLevel(logging.INFO)
    duck.auto_register(config_path=args.config, on_error="warn")
    try:
        server = PGServer.from_config(duck, args.config or getattr(duck, "_config_path", None),
                                      host=args.host, port=args.port, database=args.database,
                                      allow_saved_tables=args.allow_saved_tables)
    except ValueError as exc:
        print(f"duckduck: {exc}", file=sys.stderr)
        return 2
    print(f"duckduck: PostgreSQL clients connect to {server.host}:{server.port}, database {server.database}"
          f"{' (password required)' if server.password_check else ' (no password: this machine only)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

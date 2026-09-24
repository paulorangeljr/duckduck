"""
Virtualization only, fully offline: a synthetic "API" (synthetic.py) and
local files (data/) queried together with SQL.

    python examples/local/run.py
"""

import os

from duckduck import DuckAPI

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    duck = DuckAPI(verbose=True)  # shows what reaches each source and what DuckDB filters
    duck.auto_register(config_path=os.path.join(HERE, "duckduck.local.json"))

    print(duck.sql("SHOW TABLES"))
    print(duck.sql("""
        SELECT a.hostname, o.owner, o.department, count(*) AS alerts
        FROM assets a
        JOIN owners o ON a.ip = o.ip
        JOIN alerts x ON x.ip = a.ip
        WHERE a.os = 'linux' AND a.hostname LIKE 'web%' AND x.severity = 'critical'
        GROUP BY ALL
        ORDER BY alerts DESC
        LIMIT 10
    """).df())


if __name__ == "__main__":
    main()

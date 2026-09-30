"""The source cache: what a source returned is reused for the same call within its ttl (``duckduck.cache``)."""

import json

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.cache import SourceCache, collecting, refreshing, seconds


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _duck(cache=True, clock=None):
    calls = []

    def assets(hostname=None, limit=None):
        calls.append((hostname, limit))
        return pd.DataFrame({"hostname": ["web", "db", "web2"], "cpu": [1, 2, 3]})

    duck = DuckAPI(cache=SourceCache(clock=clock) if clock else cache)
    duck.register_api_function("assets", assets)
    return duck, calls


def test_off_by_default_in_the_library():
    duck, calls = _duck(cache=None)
    duck.sql("SELECT * FROM assets").df()
    duck.sql("SELECT * FROM assets").df()
    assert duck.cache is None and len(calls) == 2


def test_the_same_call_is_read_once_and_the_rest_of_the_query_still_runs():
    duck, calls = _duck()
    assert len(duck.sql("SELECT * FROM assets").df()) == 3
    out = duck.sql("SELECT hostname FROM assets WHERE cpu > 1 ORDER BY hostname").df()  # cpu stays with DuckDB
    assert calls == [(None, None)] and out["hostname"].tolist() == ["db", "web2"]


def test_a_different_push_down_reads_again():
    duck, calls = _duck()
    duck.sql("SELECT * FROM assets WHERE hostname = 'web'").df()
    duck.sql("SELECT * FROM assets WHERE hostname = 'db'").df()
    duck.sql("SELECT * FROM assets WHERE hostname = 'web'").df()
    assert calls == [("web", None), ("db", None)]


def test_expires_after_its_ttl():
    clock = Clock()
    duck, calls = _duck(clock=clock)
    duck.sql("SELECT * FROM assets").df()
    clock.now += 599
    with collecting() as used:
        duck.sql("SELECT * FROM assets").df()
    assert len(calls) == 1 and used == [{"table": "assets", "cached": True, "rows": 3, "age_s": 599.0}]
    clock.now += 2
    duck.sql("SELECT * FROM assets").df()
    assert len(calls) == 2


def test_refreshing_reads_again_and_keeps_the_new_read():
    duck, calls = _duck()
    duck.sql("SELECT * FROM assets").df()
    with refreshing(), collecting() as used:
        duck.sql("SELECT * FROM assets").df()
    duck.sql("SELECT * FROM assets").df()
    assert len(calls) == 2 and used[0]["cached"] is False


def test_bounded_by_rows():
    cache = SourceCache(max_rows=5)
    cache.put("a", "A", 3)
    cache.put("b", "B", 3)  # 6 > 5: the oldest goes
    assert cache.get("a") is None and cache.get("b")[0] == "B"
    cache.put("big", "X", 6)  # never kept
    assert cache.get("big") is None


def test_durations_and_config():
    assert seconds("10m") == 600 and seconds("1h") == 3600 and seconds(30) == 30 and seconds("45s") == 45
    with pytest.raises(ValueError):
        seconds("soon")
    assert SourceCache.from_config(False) is None and SourceCache.from_config({"enabled": False}) is None
    assert SourceCache.from_config(None).ttl == 600 and SourceCache.from_config({"ttl": "2h"}).ttl == 7200
    with pytest.raises(ValueError):
        SourceCache.from_config({"tll": "2h"})


def test_page_by_page_reads_are_kept_per_conditions_and_columns():
    calls = []

    def iter_assets():
        calls.append(1)
        yield pd.DataFrame({"hostname": ["web", "db"], "cpu": [1, 2]})
        yield pd.DataFrame({"hostname": ["web2"], "cpu": [3]})

    duck = DuckAPI(cache=True)
    duck.register_api_function("assets", lambda limit=None: pd.DataFrame())
    duck.register_streaming_function("assets", iter_assets)
    assert duck.sql("SELECT * FROM assets WHERE cpu > 1").df()["cpu"].tolist() == [2, 3]
    assert duck.sql("SELECT * FROM assets WHERE cpu > 1").df()["cpu"].tolist() == [2, 3]
    assert len(calls) == 1
    duck.sql("SELECT * FROM assets WHERE cpu > 2").df()  # other rows kept per page: read again
    assert len(calls) == 2


def test_the_console_uses_it_by_default_and_says_so(tmp_path):
    from duckduck.semantic.admin import SQLConsole

    duck, calls = _duck(cache=None)
    console = SQLConsole(duck)
    first = console.run("SELECT * FROM assets")
    again = console.run("SELECT count(*) AS n FROM assets")
    assert len(calls) == 1 and first["cache"]["used"][0]["cached"] is False
    assert again["cache"]["enabled"] and again["cache"]["used"][0]["cached"] is True
    fresh = console.run("SELECT * FROM assets", cache=False)
    assert len(calls) == 2 and fresh["cache"]["skipped"] is True
    assert console.clear_cache() == 1 and console.cache_status()["entries"] == 0


def test_the_config_file_turns_it_off(tmp_path):
    from duckduck.semantic.admin import SQLConsole, validate_config

    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps({"services": {}, "sql_cache": False}))
    duck, calls = _duck(cache=None)
    duck._config_path = str(path)
    console = SQLConsole(duck)
    console.run("SELECT * FROM assets")
    console.run("SELECT * FROM assets")
    assert len(calls) == 2 and console.cache_status() == {"enabled": False}
    assert any("sql_cache" in e for e in validate_config({"services": {}, "sql_cache": {"ttl": "never"}})["errors"])


def test_the_server_skips_and_clears_it():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.server import create_app

    duck, calls = _duck(cache=None)
    app = create_app(lambda: None, store=None, console=SQLConsole(duck))
    client = TestClient(app)
    client.post("/api/sql", json={"sql": "SELECT * FROM assets"})
    r = client.post("/api/sql", json={"sql": "SELECT * FROM assets", "cache": False}).json()
    assert len(calls) == 2 and r["cache"]["skipped"]
    assert client.get("/api/sql/cache").json()["entries"] == 1
    assert client.delete("/api/sql/cache").json()["cleared"] == 1


def test_the_page_can_run_without_the_cache():
    from duckduck.semantic.webpage import PAGE

    assert 'id="sqlfresh"' in PAGE and "cache: !fresh" in PAGE and "function cacheNote(" in PAGE
    assert "runSql(e.shiftKey)" in PAGE and 'id="sqlnocache"' in PAGE

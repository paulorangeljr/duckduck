"""Local autoloading: the `files` and `python` connectors under auto_register()."""

import json
import os
import textwrap

import duckdb
import pytest

from duckduck import DuckAPI
from duckduck.local_files import LocalFiles, table_name_for
from duckduck.python_source import PythonSource

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "Owners List.csv").write_text("ip,owner\n10.0.0.1,alice\n10.0.0.2,bob\n")
    (d / "alerts.jsonl").write_text('{"ip": "10.0.0.1", "sev": "high"}\n{"ip": "10.0.0.2", "sev": "low"}\n')
    (d / "notes.txt").write_text("not data")
    (d / "_SUCCESS").write_text("")
    (d / ".hidden.csv").write_text("a\n1\n")
    # Hive-partitioned parquet directory → one table, partition becomes a column
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * FROM (VALUES ('web', '2026-09-23'), ('db', '2026-09-24')) t(host, dt)) "
        f"TO '{d / 'proxy'}' (FORMAT parquet, PARTITION_BY (dt))"
    )
    return d


# ---------------------------------------------------------------------------
# files connector
# ---------------------------------------------------------------------------


def test_table_name_for():
    assert table_name_for("Owners List") == "owners_list"
    assert table_name_for("2026-report") == "t_2026_report"


def test_discovers_files_and_partitioned_directories(data_dir):
    lf = LocalFiles(str(data_dir))
    assert sorted(lf.table_functions()) == ["alerts", "owners_list", "proxy"]
    listing = lf.tables().set_index("table_name")
    assert listing.loc["proxy", "format"] == "parquet" and listing.loc["proxy", "files"] == 2
    assert listing.loc["owners_list", "format"] == "csv"


def test_partition_values_become_columns_and_where_runs_in_the_scan(data_dir):
    duck = DuckAPI()
    duck.auto_register({"local": {"connector": "files", "path": str(data_dir), "table_prefix": ""}})
    df = duck.sql("SELECT host FROM proxy WHERE dt = '2026-09-24'").df()
    assert df["host"].tolist() == ["db"]
    joined = duck.sql(
        "SELECT o.owner FROM owners_list o JOIN alerts a ON a.ip = o.ip WHERE a.sev = 'high'"
    ).df()
    assert joined["owner"].tolist() == ["alice"]


def test_columns(data_dir):
    cols = LocalFiles(str(data_dir)).columns("alerts")
    assert cols["column_name"].tolist() == ["ip", "sev"]


def test_name_collision_is_an_error(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n")
    (tmp_path / "a.jsonl").write_text('{"x": 1}\n')
    with pytest.raises(ValueError, match="map to the table name 'a'"):
        LocalFiles(str(tmp_path))


def test_not_a_directory(tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        LocalFiles(str(tmp_path / "nope"))


# ---------------------------------------------------------------------------
# python connector
# ---------------------------------------------------------------------------


def _module(tmp_path, body: str, name: str = "synthetic.py") -> str:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return str(path)


def test_python_factory_with_kwargs_and_pushdown(tmp_path):
    path = _module(tmp_path, """
        from typing import Optional
        CALLS = []
        def tables(rows=3):
            data = [{"id": i, "kind": "a" if i % 2 else "b"} for i in range(rows)]
            def items(kind: Optional[str] = None, limit: Optional[int] = None):
                CALLS.append((kind, limit))
                out = [r for r in data if kind is None or r["kind"] == kind]
                return out[:limit] if limit else out
            return {"items": items}
    """)
    duck = DuckAPI()
    duck.auto_register({"syn": {"connector": "python", "module": path, "kwargs": {"rows": 10}}})
    assert duck.sql("SELECT count(*) AS n FROM syn_items WHERE kind = 'a'").df()["n"].tolist() == [5]
    listed = duck.list_tables().set_index("table_name").loc["syn_items"]
    assert listed["source"] == "Python module" and listed["endpoint"] == path


def test_python_named_factory_and_tables_dict(tmp_path):
    path = _module(tmp_path, "def build():\n    return {'t': lambda limit=None: [{'a': 1}]}\n")
    assert list(PythonSource(path, factory="build").table_functions()) == ["t"]
    path = _module(tmp_path, "TABLES = {'u': lambda limit=None: [{'a': 1}]}\n", "dict_mod.py")
    assert list(PythonSource(path).table_functions()) == ["u"]
    with pytest.raises(ValueError, match="takes no kwargs"):
        PythonSource(path, kwargs={"x": 1})


@pytest.mark.parametrize("body, message", [
    ("X = 1\n", "must define"),
    ("def tables():\n    return [1]\n", "must return a dict"),
    ("def tables():\n    return {'bad name': lambda: []}\n", "valid SQL identifier"),
    ("def tables():\n    return {'t': 42}\n", "isn't callable"),
])
def test_python_module_errors(tmp_path, body, message):
    with pytest.raises(ValueError, match=message):
        PythonSource(_module(tmp_path, body))


def test_same_stem_in_two_folders_does_not_clash(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    p1 = _module(tmp_path / "a", "TABLES = {'t': lambda limit=None: [{'v': 'a'}]}\n", "gen.py")
    p2 = _module(tmp_path / "b", "TABLES = {'t': lambda limit=None: [{'v': 'b'}]}\n", "gen.py")
    assert PythonSource(p1).table_functions()["t"]()[0]["v"] == "a"
    assert PythonSource(p2).table_functions()["t"]()[0]["v"] == "b"


# ---------------------------------------------------------------------------
# auto_register integration
# ---------------------------------------------------------------------------


def test_relative_paths_resolve_against_the_config_file(tmp_path, data_dir, monkeypatch):
    _module(tmp_path, "TABLES = {'hosts': lambda limit=None: [{'h': 'x'}]}\n")
    cfg = tmp_path / "duckduck.local.json"
    cfg.write_text(json.dumps({"services": {
        "syn": {"connector": "python", "module": "synthetic.py", "table_prefix": ""},
        "local": {"connector": "files", "path": "data"},
    }}))
    monkeypatch.chdir("/")  # somewhere unrelated
    duck = DuckAPI()
    duck.auto_register(config_path=str(cfg))
    assert {"hosts", "local_alerts", "local_proxy", "local_tables", "local_columns"} <= set(duck.functions)


def test_prefix_collisions_are_reported(tmp_path, data_dir):
    path = _module(tmp_path, "TABLES = {'alerts': lambda limit=None: [{'a': 1}]}\n")
    duck = DuckAPI()
    with pytest.raises(ValueError, match="already registered.*alerts.*table_prefix"):
        duck.auto_register({
            "local": {"connector": "files", "path": str(data_dir), "table_prefix": ""},
            "syn": {"connector": "python", "module": path, "table_prefix": ""},
        })


def test_data_table_clashing_with_builtin_listing_table(tmp_path):
    (tmp_path / "tables.csv").write_text("x\n1\n")
    with pytest.raises(ValueError, match="clashes with the built-in 'tables'"):
        DuckAPI().auto_register({"local": {"connector": "files", "path": str(tmp_path)}})


def test_bad_local_source_can_be_skipped_with_warn(tmp_path):
    duck = DuckAPI()
    with pytest.warns(RuntimeWarning, match="'broken'"):
        duck.auto_register({
            "broken": {"connector": "files", "path": str(tmp_path / "missing")},
            "ok": {"connector": "python", "module": _module(tmp_path, "TABLES = {'t': lambda limit=None: [{'a': 1}]}\n")},
        }, on_error="warn")
    assert "ok_t" in duck.functions


def test_real_connectors_still_require_authentication():
    with pytest.raises(ValueError, match="missing 'authentication'"):
        DuckAPI().auto_register({"sn": {"connector": "servicenow", "instance": "x"}})


# ---------------------------------------------------------------------------
# the shipped examples keep working
# ---------------------------------------------------------------------------


def test_examples_local_config_runs():
    duck = DuckAPI()
    duck.auto_register(config_path=os.path.join(REPO, "examples", "local", "duckduck.local.json"))
    df = duck.sql(
        "SELECT count(*) AS n FROM assets a JOIN owners o ON a.ip = o.ip WHERE a.os = 'linux'"
    ).df()
    assert df["n"].iloc[0] > 0


def test_examples_semantic_local_config_answers_from_the_cli(capsys):
    pytest.importorskip("pydantic")
    from duckduck.semantic.__main__ import main

    code = main(["--config", os.path.join(REPO, "examples", "semantic", "duckduck.local.json"),
                 "ask", "Which machines communicated with 203.0.113.9?"])
    out = capsys.readouterr().out
    assert code == 0 and "srv-build" in out and "ws-dave" in out

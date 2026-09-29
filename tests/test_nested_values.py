"""Nested values (ADX dynamic columns, JSON fields): same shape → STRUCT / LIST, mixed shapes → JSON text, never Python's repr;
and the page shows them as JSON, never "[object Object]"."""

import json

import pandas as pd
import pytest

from duckduck import DuckAPI
from duckduck.core import json_for_mixed_objects


def _duck(frame):
    duck = DuckAPI()
    duck.register_api_function("events", lambda limit=None: frame)
    return duck


def test_rows_of_one_shape_stay_struct_and_list():
    df = pd.DataFrame({"props": [{"k": 1, "v": "x"}, {"k": 2, "v": None}], "tags": [["a", "b"], []]})
    assert json_for_mixed_objects(df) is df
    got = _duck(df).sql("SELECT props.k AS k, tags[1] AS t FROM events").fetchall()
    assert got == [(1, "a"), (2, None)]


@pytest.mark.parametrize("values", [
    [{"a": 1}, {"b": [1, 2]}],        # different keys, different value types
    [{"k": 1}, {"k": "one"}],          # a number here, a string there
    [{"a": 1}, "plain text"],          # a dict next to plain text
    [[1, "a"], [2]],                   # a list of mixed types
])
def test_mixed_shapes_become_json_text(values):
    df = json_for_mixed_objects(pd.DataFrame({"dyn": values}))
    assert all(isinstance(v, str) for v in df["dyn"])
    for v in df["dyn"]:
        if v != "plain text":
            json.loads(v)  # JSON, not Python's repr
    assert "'" not in "".join(df["dyn"])


def test_json_text_still_answers_arrow_queries():
    duck = _duck(pd.DataFrame({"dyn": [{"a": 1}, {"b": [1, 2]}], "n": [1, 2]}))
    assert duck.sql("SELECT dyn->>'a' AS a, dyn->'b'->>0 AS b0 FROM events ORDER BY n").fetchall() == [("1", None), (None, "1")]


def test_the_sql_console_returns_json_for_the_page():
    pytest.importorskip("pydantic")
    from duckduck.semantic.admin import SQLConsole
    from duckduck.semantic.engine import _json_default

    duck = _duck(pd.DataFrame({"props": [{"k": 1}], "tags": [["x", "y"]], "dyn": [{"a": {"b": 1}}]}))
    out = SQLConsole(duck).run("SELECT * FROM events")
    assert json.loads(json.dumps(out["rows"], default=_json_default)) == [[{"k": 1}, ["x", "y"], {"a": {"b": 1}}]]


def test_the_page_renders_objects_as_json():
    from duckduck.semantic.webpage import PAGE

    assert "function cellHtml(v)" in PAGE and "${cellHtml(r[c])}" in PAGE

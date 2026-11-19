"""The SQL rewrite that turns `FROM fn` / `fn(args)` into DuckDB temp tables, over the query shapes that
used to break it: a CTE or a column that shares a function's name, a function name inside a string or a
comment, an implicit (comma) join, a quoted table name. The rewrite is driven by the parse (which names
are really tables) and searches a string-and-comment-masked copy, so these are all handled.

A tracking fake records whether each registered function was actually called, so a test can assert both the
answer and that a name which only *looks* like a function was left alone (no wasted API call, no wrong data).
"""

import pandas as pd

from duckduck import DuckAPI

USERS = [{"id": 1, "name": "alice", "dept": "eng"}, {"id": 2, "name": "bob", "dept": "sales"}]
EVENTS = [{"user_id": 1, "action": "login"}, {"user_id": 2, "action": "logout"}]
DEPTS = [{"dept": "eng", "floor": 3}, {"dept": "sales", "floor": 1}]


def _duck():
    called = {}

    def table(name, rows):
        def fn(where=None, limit=None, **kwargs):
            called[name] = called.get(name, 0) + 1
            return pd.DataFrame(rows)
        fn.__name__ = name
        return fn

    duck = DuckAPI()
    duck.register_api_function("users", table("users", USERS))
    duck.register_api_function("events", table("events", EVENTS))
    duck.register_api_function("depts", table("depts", DEPTS))
    return duck, called


def test_a_cte_named_like_a_function_is_the_cte_not_the_function():
    duck, called = _duck()
    df = duck.sql("WITH users AS (SELECT 1 AS id, 'x' AS name) SELECT * FROM users").df()
    assert list(df["name"]) == ["x"] and len(df) == 1  # the CTE's one row, not the function's two
    assert "users" not in called  # the function was never called


def test_a_cte_shadows_the_function_while_another_function_still_reads():
    duck, called = _duck()
    df = duck.sql("WITH events AS (SELECT 1 AS user_id) "
                  "SELECT u.name FROM users u JOIN events ON u.id = events.user_id").df()
    assert list(df["name"]) == ["alice"]  # the events CTE (user_id=1) joined to the real users table
    assert called.get("users") == 1 and "events" not in called


def test_an_implicit_comma_join_reads_both_sources():
    duck, called = _duck()
    df = duck.sql("SELECT u.name, e.action FROM users u, events e WHERE u.id = e.user_id").df()
    assert len(df) == 2 and set(df["name"]) == {"alice", "bob"}
    assert called.get("users") == 1 and called.get("events") == 1


def test_three_way_comma_join():
    duck, called = _duck()
    df = duck.sql("SELECT u.name, e.action, d.floor FROM users u, events e, depts d "
                  "WHERE u.id = e.user_id AND u.dept = d.dept").df()
    assert len(df) == 2 and set(df["floor"]) == {3, 1}
    assert called.get("users") == 1 and called.get("events") == 1 and called.get("depts") == 1


def test_a_function_name_inside_a_string_literal_is_not_a_call():
    duck, called = _duck()
    # 'events(1)' inside the string must not be parsed as a call to the events function
    df = duck.sql("SELECT * FROM users WHERE name = 'events(1)'").df()
    assert len(df) == 0  # no user is named that
    assert "events" not in called and called.get("users") == 1


def test_a_function_name_inside_a_string_with_no_parens_is_not_a_table():
    duck, called = _duck()
    duck.sql("SELECT * FROM events WHERE action = 'see users logs'").df()
    assert called.get("events") == 1 and "users" not in called


def test_a_function_name_in_a_comment_is_not_called():
    duck, called = _duck()
    duck.sql("SELECT * FROM users u -- join events here\n WHERE u.dept = 'eng'").df()
    assert called.get("users") == 1 and "events" not in called


def test_a_column_named_like_a_function_is_not_a_table():
    duck, called = _duck()

    def rows(where=None, limit=None, **kwargs):
        return pd.DataFrame([{"events": 7, "floor": 3}])
    rows.__name__ = "office"
    duck.register_api_function("office", rows)
    df = duck.sql("SELECT events FROM office").df()
    assert list(df["events"]) == [7] and "events" not in called


def test_a_quoted_table_name_resolves():
    duck, called = _duck()
    df = duck.sql('SELECT "users".name FROM "users"').df()
    assert len(df) == 2 and called.get("users") == 1


def test_an_inline_argument_string_may_contain_a_closing_paren_and_comma():
    called = {}

    def fn(where=None, limit=None, q=None, **kwargs):
        called["q"] = q
        return pd.DataFrame([{"a": 1}])
    fn.__name__ = "search"
    duck = DuckAPI()
    duck.register_api_function("search", fn)
    duck.sql("SELECT * FROM search(q='a (b), c') s").df()
    assert called["q"] == "a (b), c"  # the ) and , inside the string didn't cut the argument

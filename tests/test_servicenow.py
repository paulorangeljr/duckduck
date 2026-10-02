"""Unit tests for the ServiceNow Table API wrapper."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from duckduck import DuckAPI, ServiceNow


def _make_sn():
    return ServiceNow("dev12345", "admin", "secret")


def _response(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.ok = status < 400
    r.json.return_value = payload
    r.text = str(payload)
    r.raise_for_status = MagicMock()
    return r


# ---------------------------------------------------------------------------
# Construction / from_secret
# ---------------------------------------------------------------------------


def test_init_sets_base_url_and_basic_auth():
    sn = _make_sn()
    assert sn.base_url == "https://dev12345.service-now.com/api/now"
    assert sn.session.auth == ("admin", "secret")


def test_from_secret_builds_instance():
    sn = ServiceNow.from_secret({"instance": "dev12345", "username": "admin", "password": "secret"})
    assert sn.base_url == "https://dev12345.service-now.com/api/now"


def test_from_secret_overrides_win():
    sn = ServiceNow.from_secret(
        {"instance": "dev12345", "username": "admin", "password": "secret"},
        instance="prod99999",
    )
    assert sn.base_url == "https://prod99999.service-now.com/api/now"


# ---------------------------------------------------------------------------
# host= — custom domain / on-prem, as an alternative to instance=
# ---------------------------------------------------------------------------


def test_host_used_as_is():
    sn = ServiceNow(username="admin", password="secret", host="servicenow.mycompany.com")
    assert sn.base_url == "https://servicenow.mycompany.com/api/now"


def test_host_takes_precedence_over_instance():
    sn = ServiceNow(
        instance="dev12345", username="admin", password="secret", host="servicenow.mycompany.com"
    )
    assert sn.base_url == "https://servicenow.mycompany.com/api/now"


def test_neither_instance_nor_host_raises():
    with pytest.raises(ValueError, match="instance' or 'host'"):
        ServiceNow(username="admin", password="secret")


def test_missing_credentials_raises():
    with pytest.raises(ValueError, match="username' and 'password'"):
        ServiceNow(instance="dev12345", username="admin", password="")


def test_from_secret_with_host():
    sn = ServiceNow.from_secret({
        "host": "servicenow.mycompany.com", "username": "admin", "password": "secret",
    })
    assert sn.base_url == "https://servicenow.mycompany.com/api/now"


# ---------------------------------------------------------------------------
# OAuth2 client-credentials (from_oauth2)
# ---------------------------------------------------------------------------


def test_from_oauth2_builds_base_url_from_instance():
    sn = ServiceNow.from_oauth2(
        token_url="https://login.microsoftonline.com/tenant/oauth2/token",
        client_id="cid", client_secret="csecret", instance="dev12345",
    )
    assert sn.base_url == "https://dev12345.service-now.com/api/now"
    assert sn._auth_mode == "oauth2"


def test_from_oauth2_api_base_used_as_is():
    sn = ServiceNow.from_oauth2(
        token_url="https://login.microsoftonline.com/tenant/oauth2/token",
        client_id="cid", client_secret="csecret",
        api_base="https://internal-gateway.mycompany.com/v1/now",
    )
    assert sn.base_url == "https://internal-gateway.mycompany.com/v1/now"


def test_from_oauth2_missing_instance_host_and_api_base_raises():
    with pytest.raises(ValueError, match="instance.*host.*api_base"):
        ServiceNow.from_oauth2(
            token_url="https://login.microsoftonline.com/tenant/oauth2/token",
            client_id="cid", client_secret="csecret",
        )


def test_ensure_token_fetches_and_caches():
    sn = ServiceNow.from_oauth2(
        token_url="https://login.microsoftonline.com/tenant/oauth2/token",
        client_id="cid", client_secret="csecret", instance="dev12345",
        resource="api://resource-id",
    )
    token_response = _response({"access_token": "tok123", "expires_in": 3600})

    with patch("duckduck.servicenow.requests.post", return_value=token_response) as mock_post:
        sn._ensure_token()
        sn._ensure_token()  # second call should use the cached token

    mock_post.assert_called_once()
    body = mock_post.call_args[1]["data"]
    assert body["client_id"] == "cid"
    assert body["client_secret"] == "csecret"
    assert body["grant_type"] == "client_credentials"
    assert body["resource"] == "api://resource-id"
    assert sn.session.headers["Authorization"] == "Bearer tok123"


def test_ensure_token_failure_surfaces_response_body():
    """
    A 400 from the token endpoint must surface the actual response body
    (Azure AD puts the real reason in "error"/"error_description" there,
    e.g. AADSTS7000215 for a bad client_secret) -- raise_for_status()
    alone drops it, leaving a debugging dead end.
    """
    sn = ServiceNow.from_oauth2(
        token_url="https://login.microsoftonline.com/tenant/oauth2/token",
        client_id="cid", client_secret="wrong-secret", instance="dev12345",
    )
    error_body = {
        "error": "invalid_client",
        "error_description": "AADSTS7000215: Invalid client secret provided.",
    }
    with patch("duckduck.servicenow.requests.post", return_value=_response(error_body, status=400)):
        with pytest.raises(ValueError, match="AADSTS7000215"):
            sn._ensure_token()


def test_ensure_token_refreshes_after_expiry():
    sn = ServiceNow.from_oauth2(
        token_url="https://login.microsoftonline.com/tenant/oauth2/token",
        client_id="cid", client_secret="csecret", instance="dev12345",
    )
    with patch(
        "duckduck.servicenow.requests.post",
        return_value=_response({"access_token": "tok1", "expires_in": 3600}),
    ):
        sn._ensure_token()

    # Force the cached token to look expired
    from datetime import datetime, timedelta
    sn._token_expires_at = datetime.now() - timedelta(seconds=1)

    with patch(
        "duckduck.servicenow.requests.post",
        return_value=_response({"access_token": "tok2", "expires_in": 3600}),
    ) as mock_post:
        sn._ensure_token()

    mock_post.assert_called_once()
    assert sn.session.headers["Authorization"] == "Bearer tok2"


def test_get_calls_ensure_token_in_oauth2_mode():
    sn = ServiceNow.from_oauth2(
        token_url="https://login.microsoftonline.com/tenant/oauth2/token",
        client_id="cid", client_secret="csecret", instance="dev12345",
    )
    with patch.object(sn, "_ensure_token") as mock_ensure, \
         patch.object(sn.session, "get", return_value=_response({"result": []})):
        sn._get("incident", {})

    mock_ensure.assert_called_once()


def test_basic_auth_mode_never_calls_ensure_token():
    sn = _make_sn()
    with patch.object(sn, "_ensure_token") as mock_ensure, \
         patch.object(sn.session, "get", return_value=_response({"result": []})):
        sn._get("incident", {})

    mock_ensure.assert_not_called()


def test_from_secret_detects_oauth2_mode():
    sn = ServiceNow.from_secret({
        "token_url": "https://login.microsoftonline.com/tenant/oauth2/token",
        "client_id": "cid",
        "client_secret": "csecret",
        "instance": "dev12345",
    })
    assert sn._auth_mode == "oauth2"
    assert sn.base_url == "https://dev12345.service-now.com/api/now"


def test_from_secret_detects_basic_auth_mode():
    sn = ServiceNow.from_secret({
        "instance": "dev12345", "username": "admin", "password": "secret",
    })
    assert sn._auth_mode == "basic"


# ---------------------------------------------------------------------------
# _build_query
# ---------------------------------------------------------------------------


def test_build_query_joins_with_caret():
    query = ServiceNow._build_query(state="2", priority="1", assigned_to=None)
    assert query == "state=2^priority=1"


def test_build_query_all_none_returns_none():
    assert ServiceNow._build_query(state=None, priority=None) is None


# ---------------------------------------------------------------------------
# _fetch — limit vs full pagination
# ---------------------------------------------------------------------------


def test_fetch_with_limit_single_request():
    sn = _make_sn()
    with patch.object(sn.session, "get", return_value=_response({"result": [{"sys_id": "1"}]})) as mock_get:
        results = sn._fetch("incident", limit=10)

    mock_get.assert_called_once()
    params = mock_get.call_args[1]["params"]
    assert params["sysparm_limit"] == 10
    assert params["sysparm_offset"] == 0
    assert results == [{"sys_id": "1"}]


def test_fetch_without_limit_paginates_until_short_page():
    sn = ServiceNow("dev12345", "admin", "secret", default_page_size=2)
    pages = [
        _response({"result": [{"id": "1"}, {"id": "2"}]}),
        _response({"result": [{"id": "3"}]}),  # shorter than page size -> stop
    ]
    with patch.object(sn.session, "get", side_effect=pages) as mock_get:
        results = sn._fetch("incident")

    assert len(results) == 3
    assert mock_get.call_count == 2
    offsets = [call.kwargs["params"]["sysparm_offset"] for call in mock_get.call_args_list]
    assert offsets == [0, 2]


def test_fetch_passes_query_and_fields():
    sn = _make_sn()
    with patch.object(sn.session, "get", return_value=_response({"result": []})) as mock_get:
        sn._fetch("incident", query="active=true", fields=["number", "state"], limit=5)

    params = mock_get.call_args[1]["params"]
    assert params["sysparm_query"] == "active=true"
    assert params["sysparm_fields"] == "number,state"


# ---------------------------------------------------------------------------
# Dedicated methods with push-down
# ---------------------------------------------------------------------------


def test_incidents_pushes_down_filters():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"number": "INC001"}]) as mock_fetch:
        df = sn.incidents(state="2", priority="1")

    mock_fetch.assert_called_once_with("incident", query="state=2^priority=1", limit=None, where=None)
    assert list(df["number"]) == ["INC001"]


def test_problems_pushes_down_state():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"number": "PRB001"}]) as mock_fetch:
        sn.problems(state="open")

    mock_fetch.assert_called_once_with("problem", query="state=open", limit=None, where=None)


def test_change_requests_pushes_down_type():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"number": "CHG001"}]) as mock_fetch:
        sn.change_requests(type="normal")

    mock_fetch.assert_called_once_with("change_request", query="type=normal", limit=None, where=None)


def test_users_pushes_down_active():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"user_name": "jdoe"}]) as mock_fetch:
        sn.users(active="true")

    mock_fetch.assert_called_once_with("sys_user", query="active=true", limit=None, where=None)


def test_cmdb_ci_pushes_down_class_name():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"name": "web01"}]) as mock_fetch:
        sn.cmdb_ci(sys_class_name="cmdb_ci_server")

    mock_fetch.assert_called_once_with(
        "cmdb_ci", query="sys_class_name=cmdb_ci_server", limit=None, where=None
    )


# ---------------------------------------------------------------------------
# Generic table() — any table, structural table_name
# ---------------------------------------------------------------------------


def test_table_uses_structural_table_name():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"x": 1}]) as mock_fetch:
        df = sn.table(table_name="sys_user_group", query="active=true", limit=20)

    mock_fetch.assert_called_once_with("sys_user_group", query="active=true", limit=20, where=None)
    assert list(df["x"]) == [1]


# ---------------------------------------------------------------------------
# Streaming (iter_*)
# ---------------------------------------------------------------------------


def test_iter_incidents_yields_pages():
    sn = ServiceNow("dev12345", "admin", "secret", default_page_size=1)
    pages = [
        _response({"result": [{"number": "INC001"}]}),
        _response({"result": [{"number": "INC002"}]}),
        _response({"result": []}),
    ]
    with patch.object(sn.session, "get", side_effect=pages):
        chunks = list(sn.iter_incidents(state="2"))

    assert len(chunks) == 2
    assert all(isinstance(c, pd.DataFrame) for c in chunks)


# ---------------------------------------------------------------------------
# Integration with DuckAPI
# ---------------------------------------------------------------------------


def test_duckapi_sql_pushdown_integration():
    sn = _make_sn()
    duck = DuckAPI()
    duck.register_api_function("incidents", sn.incidents)

    with patch.object(sn, "_get", return_value={"result": [{"number": "INC001", "priority": "1"}]}) as mock_get:
        df = duck.sql("SELECT * FROM incidents WHERE priority = '1'").df()

    # the condition arrives through `where` and becomes the same encoded query
    assert mock_get.call_args.args[1]["sysparm_query"] == "priority=1"
    assert list(df["number"]) == ["INC001"]
    duck.close()


# ---------------------------------------------------------------------------
# Generic `where` push-down (any field, any table)
# ---------------------------------------------------------------------------

from duckduck.pushdown import Condition  # noqa: E402


@pytest.mark.parametrize("cond, clause", [
    (Condition("dns_domain", "like", "%auql%"), "dns_domainLIKEauql"),
    (Condition("name", "ilike", "web%"), "nameSTARTSWITHweb"),
    (Condition("fqdn", "like", "%.corp"), "fqdnENDSWITH.corp"),
    (Condition("name", "like", "srv01"), "name=srv01"),
    (Condition("install_status", "eq", "1"), "install_status=1"),
    (Condition("active", "eq", True), "active=true"),
    (Condition("cpu_count", "gte", 4), "cpu_count>=4"),
])
def test_condition_clause_translates(cond, clause):
    assert ServiceNow._condition_clause(cond) == (clause, "")


@pytest.mark.parametrize("cond, reason", [
    (Condition("assigned_to_value", "eq", "abc"), "reference sub-column"),
    (Condition("assigned_to_link", "eq", "x"), "reference sub-column"),
    (Condition("sys_created_on", "gte", "2026-01-01"), "set the connector's \"timezone\""),
    (Condition("short_description", "gt", "abc"), "non-numeric comparison"),
    (Condition("name", "like", "srv_1%"), "not translatable"),
    (Condition("name", "eq", "x^ORactive=false"), "'^'"),
    (Condition("Weird Name", "eq", "x"), "not a ServiceNow field name"),
])
def test_condition_clause_refuses_what_could_lose_rows(cond, reason):
    clause, why = ServiceNow._condition_clause(cond)
    assert clause is None and reason in why


def test_table_where_goes_into_sysparm_query_with_limit():
    sn = _make_sn()
    duck = DuckAPI()
    duck.register_api_function("sn_table", sn.table)
    with patch.object(sn, "_get", return_value={"result": [{"name": "a", "dns_domain": "x.auql.net"}]}) as mock_get:
        df = duck.sql(
            "SELECT name FROM sn_table(table_name='cmdb_ci_server') WHERE dns_domain LIKE '%auql%' LIMIT 10"
        ).df()
    params = mock_get.call_args.args[1]
    assert mock_get.call_args.args[0] == "cmdb_ci_server"
    assert params["sysparm_query"] == "dns_domainLIKEauql" and params["sysparm_limit"] == 10
    assert df["name"].tolist() == ["a"]
    duck.close()


def test_blocked_condition_stays_with_duckdb_and_limit_is_not_pushed():
    sn = _make_sn()
    duck = DuckAPI()
    duck.register_api_function("sn_table", sn.table)
    rows = [{"name": f"s{i}", "assigned_to": {"link": "l", "value": "abc" if i == 3 else "zzz"}} for i in range(5)]
    with patch.object(sn, "_get", return_value={"result": rows}) as mock_get:
        df = duck.sql(
            "SELECT name FROM sn_table(table_name='cmdb_ci') WHERE name LIKE 's%' AND assigned_to_value = 'abc' LIMIT 1"
        ).df()
    params = mock_get.call_args.args[1]
    assert params["sysparm_query"] == "nameSTARTSWITHs"
    assert params["sysparm_limit"] == sn.default_page_size  # paginating: no LIMIT 1 at the source
    assert df["name"].tolist() == ["s3"]  # with LIMIT 1 pushed, s3 would have been lost
    duck.close()


def test_where_is_combined_with_raw_query_and_dedicated_filters():
    sn = _make_sn()
    with patch.object(sn, "_get", return_value={"result": []}) as mock_get:
        sn.incidents(state="2", where=[Condition("category", "eq", "network")], limit=5)
    assert mock_get.call_args.args[1]["sysparm_query"] == "state=2^category=network"
    with patch.object(sn, "_get", return_value={"result": []}) as mock_get:
        sn.table("incident", query="active=true", where=[Condition("priority", "lte", 2)], limit=5)
    assert mock_get.call_args.args[1]["sysparm_query"] == "active=true^priority<=2"


def test_raw_query_with_nq_keeps_where_in_duckdb_and_drops_limit():
    sn = _make_sn()
    with patch.object(sn, "_get", return_value={"result": []}) as mock_get:
        sn.table("incident", query="priority=1^NQpriority=2", where=[Condition("category", "eq", "x")], limit=5)
    params = mock_get.call_args.args[1]
    assert params["sysparm_query"] == "priority=1^NQpriority=2"
    assert params["sysparm_limit"] == sn.default_page_size


# ---------------------------------------------------------------------------
# The instance's tables (sys_db_object) — a catalog of table()
# ---------------------------------------------------------------------------


def test_tables_lists_sys_db_object_as_calls_of_table():
    from duckduck.kinds import CATALOG, drafts_of, kind_of, lists_of

    sn = _make_sn()
    rows = [
        {"name": "incident", "label": "Incident", "super_class.name": "task", "is_extendable": "true",
         "sys_scope.scope": "global", "sys_updated_on": "2026-01-01 00:00:00"},
        {"name": "u_custom", "label": "Custom", "super_class.name": "", "is_extendable": "false",
         "sys_scope.scope": "x_app", "sys_updated_on": ""},
    ]
    with patch.object(sn, "_get", return_value={"result": rows}) as mock_get:
        df = sn.tables(table_name_ilike="%in%", extends="task", limit=10)
    table, params = mock_get.call_args.args
    assert table == "sys_db_object"
    assert params["sysparm_query"] == "nameLIKEin^super_class.name=task^ORDERBYname"
    assert params["sysparm_fields"] == "name,label,super_class.name,is_extendable,sys_scope.scope,sys_updated_on"
    assert df["table_name"].tolist() == ["incident", "u_custom"]
    assert df["extends"].tolist() == ["task", ""]
    assert df["extendable"].tolist() == [True, False]
    assert kind_of(sn.tables) == CATALOG and lists_of(sn.tables) == "table"
    assert drafts_of(sn.tables) is False  # thousands of platform tables: never drafted wholesale


def test_tables_behind_the_servicenow_catalog_are_its_table_calls():
    duck = DuckAPI()
    duck.auto_register({"sn": {"connector": "servicenow", "instance": "dev1",
                               "authentication": {"type": "local", "username": "a", "password": "b"}}})
    sn = duck.functions["sn_tables"].__self__
    with patch.object(sn, "_get", return_value={"result": [{"name": "incident"}, {"name": "sys_user"}]}):
        nested, notes = duck.nested_tables(service="sn")
    assert notes == []
    assert [(n["table"], n["args"], n["address"], n["drafts"]) for n in nested] == [
        ("sn_table", {"table_name": "incident"}, "sn.incident", False),
        ("sn_table", {"table_name": "sys_user"}, "sn.sys_user", False),
    ]
    duck.close()


# -- dates go to ServiceNow converted to the API user's timezone (the connector's "timezone") ---------------------------


@pytest.mark.parametrize("value, op, clause", [
    ("2026-10-01 12:00:00", "gt", "sys_updated_on>javascript:gs.dateGenerate('2026-10-01','09:00:00')"),
    ("2026-10-01T12:00:00Z", "gt", "sys_updated_on>javascript:gs.dateGenerate('2026-10-01','09:00:00')"),
    ("2026-10-01 12:00:00-03:00", "gte", "sys_updated_on>=javascript:gs.dateGenerate('2026-10-01','12:00:00')"),
    ("2026-10-01", "gte", "sys_updated_on>=javascript:gs.dateGenerate('2026-09-30','21:00:00')"),
    ("2026-10-01 12:00:00.250", "lt", "sys_updated_on<javascript:gs.dateGenerate('2026-10-01','09:00:01')"),
    ("2026-10-01 12:00:00.250", "gt", "sys_updated_on>javascript:gs.dateGenerate('2026-10-01','09:00:00')"),
])
def test_dates_are_converted_to_the_api_users_timezone(value, op, clause):
    """Values without an offset are UTC — what the Table API returns — and rounding only ever widens."""
    assert ServiceNow._condition_clause(Condition("sys_updated_on", op, value), "America/Sao_Paulo") == (clause, "")


def test_the_timezone_is_a_connector_option(monkeypatch):
    sn = ServiceNow.from_secret({"instance": "dev1", "username": "u", "password": "p"}, timezone="UTC")
    assert sn.timezone == "UTC" and sn.pushdown_blocker(Condition("sys_updated_on", "gt", "2026-10-01")) is None
    oauth = ServiceNow.from_secret({"instance": "dev1", "client_id": "c", "client_secret": "s",
                                    "token_url": "https://t"}, timezone="Europe/Lisbon")
    assert oauth.timezone == "Europe/Lisbon"
    assert "timezone" in _make_sn().pushdown_blocker(Condition("sys_updated_on", "gt", "2026-10-01"))
    with pytest.raises(ValueError, match="isn't a timezone name"):
        ServiceNow("dev1", "u", "p", timezone="Brasilia")


def test_an_incremental_pipeline_sends_its_watermark_to_servicenow(tmp_path):
    from duckduck.pipeline import run_pipeline

    sn = ServiceNow("dev12345", "admin", "secret", timezone="America/Sao_Paulo")
    duck = DuckAPI()
    duck.register_api_function("incidents", sn.incidents)
    rows = [{"sys_id": "a", "number": "INC1", "sys_updated_on": "2026-10-01 12:00:00"}]
    spec = {"pipeline": "inc", "primary_key": "sys_id", "sql": "SELECT * FROM incidents",
            "load": {"type": "incremental", "columns": ["sys_updated_on"]}, "state": str(tmp_path / "s"),
            "target": str(tmp_path / "bronze")}
    with patch.object(sn, "_get", return_value={"result": rows}) as mock_get:
        run_pipeline(spec, duck=duck)
        assert "sysparm_query" not in mock_get.call_args.args[1] or \
            "sys_updated_on" not in (mock_get.call_args.args[1].get("sysparm_query") or "")
        run_pipeline(spec, duck=duck)
    sent = mock_get.call_args.args[1]["sysparm_query"]
    assert "sys_updated_on>javascript:gs.dateGenerate('2026-10-01','09:00:00')" in sent
    duck.close()


class _Instance:
    """A ServiceNow answering by offset, slowly, counting requests in flight; ``counted`` = sends X-Total-Count."""

    def __init__(self, rows=9, counted=True, delay=0.05):
        import threading

        self.rows, self.counted, self.delay = rows, counted, delay
        self.lock, self.in_flight, self.most, self.offsets = threading.Lock(), 0, 0, []

    def get(self, url, params=None, timeout=None):
        import time

        with self.lock:
            self.in_flight += 1
            self.most = max(self.most, self.in_flight)
            self.offsets.append(params["sysparm_offset"])
        time.sleep(self.delay)
        with self.lock:
            self.in_flight -= 1
        start, size = params["sysparm_offset"], params["sysparm_limit"]
        r = _response({"result": [{"number": f"INC{i}"} for i in range(start, min(start + size, self.rows))]})
        r.headers = {"X-Total-Count": str(self.rows)} if self.counted else {}
        return r


@pytest.mark.parametrize("counted", [True, False])
def test_pages_are_read_several_at_once_with_or_without_the_total_header(counted):
    sn = ServiceNow("dev12345", "admin", "secret", default_page_size=2)
    fake = _Instance(counted=counted)
    sn.session.get = fake.get
    duck = DuckAPI()
    duck.register_api_function("incidents", sn.table)
    df = duck.sql("SELECT number FROM incidents(table_name='incident')").df()
    assert df["number"].tolist() == [f"INC{i}" for i in range(9)]  # every row, in order
    assert fake.most > 1  # more than one request in flight at a time
    if counted:
        assert sorted(fake.offsets) == [0, 2, 4, 6, 8]
    else:  # no total: read ahead until a short page, a few empty ones at most at the end
        assert sorted(fake.offsets)[:5] == [0, 2, 4, 6, 8] and len(fake.offsets) <= 5 + 3

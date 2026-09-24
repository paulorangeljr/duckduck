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
    r.json.return_value = payload
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

    mock_fetch.assert_called_once_with("incident", query="state=2^priority=1", limit=None)
    assert list(df["number"]) == ["INC001"]


def test_problems_pushes_down_state():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"number": "PRB001"}]) as mock_fetch:
        sn.problems(state="open")

    mock_fetch.assert_called_once_with("problem", query="state=open", limit=None)


def test_change_requests_pushes_down_type():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"number": "CHG001"}]) as mock_fetch:
        sn.change_requests(type="normal")

    mock_fetch.assert_called_once_with("change_request", query="type=normal", limit=None)


def test_users_pushes_down_active():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"user_name": "jdoe"}]) as mock_fetch:
        sn.users(active="true")

    mock_fetch.assert_called_once_with("sys_user", query="active=true", limit=None)


def test_cmdb_ci_pushes_down_class_name():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"name": "web01"}]) as mock_fetch:
        sn.cmdb_ci(sys_class_name="cmdb_ci_server")

    mock_fetch.assert_called_once_with(
        "cmdb_ci", query="sys_class_name=cmdb_ci_server", limit=None
    )


# ---------------------------------------------------------------------------
# Generic table() — any table, structural table_name
# ---------------------------------------------------------------------------


def test_table_uses_structural_table_name():
    sn = _make_sn()
    with patch.object(sn, "_fetch", return_value=[{"x": 1}]) as mock_fetch:
        df = sn.table(table_name="sys_user_group", query="active=true", limit=20)

    mock_fetch.assert_called_once_with("sys_user_group", query="active=true", limit=20)
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

    with patch.object(sn, "_fetch", return_value=[{"number": "INC001", "priority": "1"}]) as mock_fetch:
        df = duck.sql("SELECT * FROM incidents WHERE priority = '1'").df()

    mock_fetch.assert_called_once_with("incident", query="priority=1", limit=None)
    assert list(df["number"]) == ["INC001"]
    duck.close()

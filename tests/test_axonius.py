"""Unit tests for the Axonius REST API v2 wrapper."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from duckduck import Axonius, DuckAPI


def _make_ax():
    return Axonius("axonius.example.com", "key123", "secret456")


def _response(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status = MagicMock()
    return r


# ---------------------------------------------------------------------------
# Construction / from_secret
# ---------------------------------------------------------------------------


def test_init_sets_base_url_and_headers():
    ax = _make_ax()
    assert ax.base_url == "https://axonius.example.com/api"
    assert ax.session.headers["api-key"] == "key123"
    assert ax.session.headers["api-secret"] == "secret456"


def test_from_secret_builds_instance():
    ax = Axonius.from_secret({
        "instance": "axonius.example.com", "api_key": "key123", "api_secret": "secret456",
    })
    assert ax.base_url == "https://axonius.example.com/api"


def test_from_secret_overrides_win():
    ax = Axonius.from_secret(
        {"instance": "axonius.example.com", "api_key": "key123", "api_secret": "secret456"},
        instance="axonius2.example.com",
    )
    assert ax.base_url == "https://axonius2.example.com/api"


# ---------------------------------------------------------------------------
# _build_body / _build_aql
# ---------------------------------------------------------------------------


def test_build_body_shape_without_filter():
    body = Axonius._build_body(0, 100, None)
    assert body == {
        "meta": None,
        "data": {"type": "entity_request_schema", "attributes": {"page": {"offset": 0, "limit": 100}}},
    }


def test_build_body_shape_with_filter():
    body = Axonius._build_body(50, 25, 'hostname == "web01"')
    assert body["data"]["attributes"]["page"] == {"offset": 50, "limit": 25}
    assert body["data"]["attributes"]["filter"] == 'hostname == "web01"'


def test_build_aql_joins_with_and():
    aql = Axonius._build_aql(a='x == "1"', b='y == "2"', c=None)
    assert aql == 'x == "1" and y == "2"'


def test_build_aql_all_none_returns_none():
    assert Axonius._build_aql(a=None, b=None) is None


# ---------------------------------------------------------------------------
# _fetch — limit vs full pagination
# ---------------------------------------------------------------------------


def test_fetch_with_limit_single_request():
    ax = _make_ax()
    with patch.object(
        ax.session, "post", return_value=_response({"data": [{"id": "1"}]})
    ) as mock_post:
        results = ax._fetch("/devices", limit=10)

    mock_post.assert_called_once()
    body = mock_post.call_args[1]["json"]
    assert body["data"]["attributes"]["page"] == {"offset": 0, "limit": 10}
    assert results == [{"id": "1"}]


def test_fetch_without_limit_paginates_until_short_page():
    ax = Axonius("axonius.example.com", "k", "s", default_page_size=2)
    pages = [
        _response({"data": [{"id": "1"}, {"id": "2"}]}),
        _response({"data": [{"id": "3"}]}),
    ]
    with patch.object(ax.session, "post", side_effect=pages) as mock_post:
        results = ax._fetch("/devices")

    assert len(results) == 3
    assert mock_post.call_count == 2
    offsets = [call.kwargs["json"]["data"]["attributes"]["page"]["offset"] for call in mock_post.call_args_list]
    assert offsets == [0, 2]


# ---------------------------------------------------------------------------
# devices() / users() — push-down via AQL
# ---------------------------------------------------------------------------


def test_devices_pushes_down_hostname():
    ax = _make_ax()
    with patch.object(ax, "_fetch", return_value=[{"id": "1", "type": "devices", "attributes": {"hostname": "web01"}}]) as mock_fetch:
        df = ax.devices(hostname="web01")

    mock_fetch.assert_called_once_with(
        "/devices", filter_aql='specific_data.data.hostname == "web01"', limit=None
    )
    assert list(df["hostname"]) == ["web01"]
    assert list(df["_id"]) == ["1"]


def test_devices_combines_raw_filter_and_hostname():
    ax = _make_ax()
    with patch.object(ax, "_fetch", return_value=[]) as mock_fetch:
        ax.devices(filter='os.type == "Windows"', hostname="web01")

    mock_fetch.assert_called_once_with(
        "/devices",
        filter_aql='os.type == "Windows" and specific_data.data.hostname == "web01"',
        limit=None,
    )


def test_devices_no_filters_passes_none():
    ax = _make_ax()
    with patch.object(ax, "_fetch", return_value=[]) as mock_fetch:
        ax.devices()

    mock_fetch.assert_called_once_with("/devices", filter_aql=None, limit=None)


def test_users_pushes_down_username():
    ax = _make_ax()
    with patch.object(ax, "_fetch", return_value=[{"id": "u1", "type": "users", "attributes": {"username": "jdoe"}}]) as mock_fetch:
        df = ax.users(username="jdoe")

    mock_fetch.assert_called_once_with(
        "/users", filter_aql='specific_data.data.username == "jdoe"', limit=None
    )
    assert list(df["username"]) == ["jdoe"]


# ---------------------------------------------------------------------------
# _normalize_assets
# ---------------------------------------------------------------------------


def test_normalize_assets_elevates_attributes():
    items = [
        {"id": "1", "type": "devices", "attributes": {"hostname": "a", "os_type": "Linux"}},
        {"id": "2", "type": "devices", "attributes": {"hostname": "b", "os_type": "Windows"}},
    ]
    df = Axonius._normalize_assets(items)

    assert list(df["_id"]) == ["1", "2"]
    assert list(df["hostname"]) == ["a", "b"]
    assert list(df["os_type"]) == ["Linux", "Windows"]


# ---------------------------------------------------------------------------
# Streaming (iter_*)
# ---------------------------------------------------------------------------


def test_iter_devices_yields_pages():
    ax = Axonius("axonius.example.com", "k", "s", default_page_size=1)
    pages = [
        _response({"data": [{"id": "1", "type": "devices", "attributes": {"hostname": "a"}}]}),
        _response({"data": [{"id": "2", "type": "devices", "attributes": {"hostname": "b"}}]}),
        _response({"data": []}),
    ]
    with patch.object(ax.session, "post", side_effect=pages):
        chunks = list(ax.iter_devices())

    assert len(chunks) == 2
    assert all(isinstance(c, pd.DataFrame) for c in chunks)


# ---------------------------------------------------------------------------
# Integration with DuckAPI
# ---------------------------------------------------------------------------


def test_duckapi_sql_pushdown_integration():
    ax = _make_ax()
    duck = DuckAPI()
    duck.register_api_function("devices", ax.devices)

    with patch.object(
        ax, "_fetch",
        return_value=[{"id": "1", "type": "devices", "attributes": {"hostname": "web-prod-01"}}],
    ) as mock_fetch:
        df = duck.sql("SELECT * FROM devices WHERE hostname = 'web-prod-01'").df()

    mock_fetch.assert_called_once_with(
        "/devices", filter_aql='specific_data.data.hostname == "web-prod-01"', limit=None
    )
    assert list(df["hostname"]) == ["web-prod-01"]
    duck.close()

"""Unit tests for the SharePoint wrapper."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from duckduck import DuckAPI, SharePoint
from duckduck.sharepoint import _normalize_pem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT = "tenant-id"
CLIENT = "client-id"
SECRET = "secret"

SITE_ID = "site-abc"
LIST_ID = "list-xyz"
DRIVE_ID = "drive-111"


def _make_sp(msal_cls):
    """Creates a SharePoint instance with MSAL mocked."""
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok123"}
    msal_cls.return_value = mock_app
    return SharePoint(TENANT, CLIENT, SECRET)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_token_from_acquire(msal_cls):
    sp = _make_sp(msal_cls)
    token = sp._get_token()
    assert token == "tok123"


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_token_uses_cache(msal_cls):
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = {"access_token": "cached"}
    msal_cls.return_value = mock_app
    sp = SharePoint(TENANT, CLIENT, SECRET)

    token = sp._get_token()
    assert token == "cached"
    mock_app.acquire_token_for_client.assert_not_called()


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_auth_error_raises(msal_cls):
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {
        "error": "invalid_client",
        "error_description": "bad secret",
    }
    msal_cls.return_value = mock_app
    sp = SharePoint(TENANT, CLIENT, SECRET)

    with pytest.raises(ValueError, match="authentication failed"):
        sp._get_token()


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_from_thumbprint(msal_cls):
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
    msal_cls.return_value = mock_app

    sp = SharePoint.from_thumbprint(TENANT, CLIENT, "AA:BB:CC", "-----BEGIN PRIVATE KEY-----\n...")
    assert sp._get_token() == "tok"
    # Verifies the thumbprint was normalized (no ':', uppercase)
    call_credential = msal_cls.call_args[1]["client_credential"]
    assert call_credential["thumbprint"] == "AABBCC"


# ---------------------------------------------------------------------------
# _normalize_pem — fixes a literal \n (common with double-escaped secrets)
# ---------------------------------------------------------------------------


def test_normalize_pem_converts_literal_backslash_n():
    raw = "-----BEGIN PRIVATE KEY-----\\nMIIE...\\n-----END PRIVATE KEY-----\\n"
    normalized = _normalize_pem(raw)

    assert "\\n" not in normalized
    assert normalized == "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----\n"


def test_normalize_pem_keeps_real_newlines_untouched():
    raw = "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----\n"
    assert _normalize_pem(raw) == raw


def test_normalize_pem_noop_without_backslash_n():
    raw = "-----BEGIN PRIVATE KEY-----MIIE...-----END PRIVATE KEY-----"
    assert _normalize_pem(raw) == raw


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_from_thumbprint_fixes_escaped_newlines(msal_cls):
    """
    A PEM coming from a double-escaped AWS Secrets Manager secret arrives
    as '...\\n...' (2 characters) instead of a real line break.
    from_thumbprint must fix this automatically.
    """
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
    msal_cls.return_value = mock_app

    escaped_pem = "-----BEGIN PRIVATE KEY-----\\nMIIE...\\n-----END PRIVATE KEY-----\\n"
    SharePoint.from_thumbprint(TENANT, CLIENT, "AABBCC", escaped_pem)

    call_credential = msal_cls.call_args[1]["client_credential"]
    assert "\\n" not in call_credential["private_key"]
    assert call_credential["private_key"].count("\n") == 3


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_from_secret_thumbprint_fixes_escaped_newlines(msal_cls):
    """Same fix via from_secret, as it arrives from SecretsManager.get_secret."""
    msal_cls.return_value = MagicMock()

    sp = SharePoint.from_secret({
        "tenant_id": TENANT, "client_id": CLIENT,
        "thumbprint": "AABBCC",
        "private_key_pem": "-----BEGIN PRIVATE KEY-----\\nMIIE...\\n-----END PRIVATE KEY-----\\n",
    })

    assert isinstance(sp, SharePoint)
    call_credential = msal_cls.call_args[1]["client_credential"]
    assert "\\n" not in call_credential["private_key"]


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_from_thumbprint_accepts_file_path(msal_cls, tmp_path):
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
    msal_cls.return_value = mock_app

    key_file = tmp_path / "key.pem"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----\n")

    SharePoint.from_thumbprint(TENANT, CLIENT, "AABBCC", str(key_file))

    call_credential = msal_cls.call_args[1]["client_credential"]
    assert call_credential["private_key"].startswith("-----BEGIN PRIVATE KEY-----")


# ---------------------------------------------------------------------------
# _with_top
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_with_top_no_existing(msal_cls):
    sp = _make_sp(msal_cls)
    url = sp._with_top("https://graph.microsoft.com/v1.0/sites?search=*", 50)
    assert "$top=50" in url


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_with_top_replaces_existing(msal_cls):
    sp = _make_sp(msal_cls)
    url = sp._with_top("https://example.com/items?$top=200", 25)
    assert "$top=25" in url
    assert "$top=200" not in url


# ---------------------------------------------------------------------------
# _iter_pages — nextLink pagination
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_iter_pages_follows_next_link(msal_cls):
    sp = _make_sp(msal_cls)

    page1 = {"value": [{"id": "1"}], "@odata.nextLink": "https://next"}
    page2 = {"value": [{"id": "2"}]}

    with patch.object(sp, "_get", side_effect=[page1, page2]):
        pages = list(sp._iter_pages("https://start"))

    assert len(pages) == 2
    assert pages[0][0]["id"] == "1"
    assert pages[1][0]["id"] == "2"


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_iter_pages_single_page(msal_cls):
    sp = _make_sp(msal_cls)

    with patch.object(sp, "_get", return_value={"value": [{"id": "A"}, {"id": "B"}]}):
        pages = list(sp._iter_pages("https://url"))

    assert len(pages) == 1
    assert len(pages[0]) == 2


# ---------------------------------------------------------------------------
# _fetch — limit vs full pagination
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_fetch_with_limit_single_request(msal_cls):
    sp = _make_sp(msal_cls)

    with patch.object(sp, "_get", return_value={"value": [{"id": "1"}, {"id": "2"}]}) as mock_get:
        items = sp._fetch("https://url", limit=5)

    mock_get.assert_called_once()
    assert "$top=5" in mock_get.call_args[0][0]
    assert len(items) == 2


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_fetch_without_limit_paginates(msal_cls):
    sp = _make_sp(msal_cls)

    pages = [
        {"value": [{"id": "1"}], "@odata.nextLink": "https://p2"},
        {"value": [{"id": "2"}]},
    ]
    with patch.object(sp, "_get", side_effect=pages):
        items = sp._fetch("https://url")

    assert len(items) == 2


# ---------------------------------------------------------------------------
# _normalize_list_items
# ---------------------------------------------------------------------------


def test_normalize_list_items_elevates_fields():
    items = [
        {
            "id": "1",
            "createdDateTime": "2024-01-01",
            "lastModifiedDateTime": "2024-06-01",
            "webUrl": "https://example.com/1",
            "fields": {"Title": "Foo", "Status": "Active"},
        },
        {
            "id": "2",
            "createdDateTime": "2024-02-01",
            "lastModifiedDateTime": "2024-07-01",
            "webUrl": "https://example.com/2",
            "fields": {"Title": "Bar", "Status": "Done"},
        },
    ]
    df = SharePoint._normalize_list_items(items)

    assert "_item_id" in df.columns
    assert "Title" in df.columns
    assert "Status" in df.columns
    assert list(df["_item_id"]) == ["1", "2"]
    assert list(df["Title"]) == ["Foo", "Bar"]


def test_normalize_list_items_renames_via_column_map():
    """Fields with an internal name (field_1) become the map's displayName."""
    items = [
        {"id": "1", "fields": {"field_1": "Foo", "Status": "Active"}},
    ]
    column_map = {"field_1": "Customer Name", "Status": "Situation"}

    df = SharePoint._normalize_list_items(items, column_map=column_map)

    assert "Customer Name" in df.columns
    assert "Situation" in df.columns
    assert "field_1" not in df.columns
    assert list(df["Customer Name"]) == ["Foo"]


def test_normalize_list_items_keeps_unmapped_fields():
    """Fields with no entry in the map keep their original name."""
    items = [{"id": "1", "fields": {"field_1": "Foo", "Extra": "bar"}}]
    column_map = {"field_1": "Name"}

    df = SharePoint._normalize_list_items(items, column_map=column_map)

    assert "Name" in df.columns
    assert "Extra" in df.columns  # not in the map, keeps its original name


# ---------------------------------------------------------------------------
# _get_column_display_map — internal name → displayName resolution
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_get_column_display_map_builds_dict(msal_cls):
    sp = _make_sp(msal_cls)

    columns_data = [
        {"name": "field_1", "displayName": "Customer Name"},
        {"name": "Status", "displayName": "Situation"},
    ]
    with patch.object(sp, "_fetch", return_value=columns_data):
        col_map = sp._get_column_display_map(SITE_ID, LIST_ID)

    assert col_map == {"field_1": "Customer Name", "Status": "Situation"}


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_get_column_display_map_is_cached(msal_cls):
    sp = _make_sp(msal_cls)

    columns_data = [{"name": "field_1", "displayName": "Name"}]
    with patch.object(sp, "_fetch", return_value=columns_data) as mock_fetch:
        sp._get_column_display_map(SITE_ID, LIST_ID)
        sp._get_column_display_map(SITE_ID, LIST_ID)

    mock_fetch.assert_called_once()  # second call uses the cache


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_list_items_display_names_by_default(msal_cls):
    """By default, list_items() resolves internal names to displayName."""
    sp = _make_sp(msal_cls)

    columns_data = [{"name": "field_1", "displayName": "Customer Name"}]
    items_data = [{"id": "i1", "fields": {"field_1": "ACME"}}]

    with patch.object(sp, "_fetch", side_effect=[columns_data, items_data]):
        df = sp.list_items(site_id=SITE_ID, list_id=LIST_ID)

    assert "Customer Name" in df.columns
    assert "field_1" not in df.columns
    assert list(df["Customer Name"]) == ["ACME"]


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_list_items_internal_names_skip_columns_fetch(msal_cls):
    """column_names='internal' doesn't make an extra request to /columns."""
    sp = _make_sp(msal_cls)

    items_data = [{"id": "i1", "fields": {"field_1": "ACME"}}]

    with patch.object(sp, "_fetch", return_value=items_data) as mock_fetch:
        df = sp.list_items(site_id=SITE_ID, list_id=LIST_ID, column_names="internal")

    mock_fetch.assert_called_once()  # only the items call, no /columns
    assert "field_1" in df.columns


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_list_items_invalid_column_names_raises(msal_cls):
    sp = _make_sp(msal_cls)

    with pytest.raises(ValueError, match="column_names"):
        sp.list_items(site_id=SITE_ID, list_id=LIST_ID, column_names="bogus")


# ---------------------------------------------------------------------------
# sites()
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_sites_returns_dataframe(msal_cls):
    sp = _make_sp(msal_cls)

    with patch.object(sp, "_fetch", return_value=[{"id": "s1", "displayName": "Home"}]):
        df = sp.sites()

    assert list(df["id"]) == ["s1"]


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_sites_pushes_limit(msal_cls):
    sp = _make_sp(msal_cls)

    with patch.object(sp, "_fetch", return_value=[]) as mock_fetch:
        try:
            sp.sites(limit=10)
        except Exception:
            pass
        mock_fetch.assert_called_once()
        assert mock_fetch.call_args[1]["limit"] == 10


# ---------------------------------------------------------------------------
# lists() and list_items()
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_lists_uses_site_id(msal_cls):
    sp = _make_sp(msal_cls)

    with patch.object(sp, "_fetch", return_value=[{"id": "l1"}]) as mock_fetch:
        sp.lists(site_id=SITE_ID)

    url = mock_fetch.call_args[0][0]
    assert SITE_ID in url


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_list_items_expands_fields(msal_cls):
    sp = _make_sp(msal_cls)

    raw = [{"id": "i1", "fields": {"Title": "T"}}]
    with patch.object(sp, "_fetch", return_value=raw) as mock_fetch:
        df = sp.list_items(site_id=SITE_ID, list_id=LIST_ID, column_names="internal")

    url = mock_fetch.call_args[0][0]
    assert "expand=fields" in url
    assert "Title" in df.columns


# ---------------------------------------------------------------------------
# drive_items() — derived is_file column
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_drive_items_is_file_column(msal_cls):
    sp = _make_sp(msal_cls)

    items = [
        {"id": "f1", "name": "doc.pdf", "file": {"mimeType": "application/pdf"}},
        {"id": "d1", "name": "folder", "folder": {"childCount": 3}},
    ]
    with patch.object(sp, "_fetch", return_value=items):
        df = sp.drive_items(site_id=SITE_ID, drive_id=DRIVE_ID)

    assert "is_file" in df.columns


# ---------------------------------------------------------------------------
# Streaming — iter_list_items
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_iter_list_items_yields_dataframes(msal_cls):
    sp = _make_sp(msal_cls)

    pages = [
        [{"id": "i1", "fields": {"Title": "A"}}],
        [{"id": "i2", "fields": {"Title": "B"}}, {"id": "i3", "fields": {"Title": "C"}}],
    ]
    with patch.object(sp, "_iter_pages", return_value=iter(pages)):
        chunks = list(
            sp.iter_list_items(site_id=SITE_ID, list_id=LIST_ID, column_names="internal")
        )

    assert len(chunks) == 2
    assert all(isinstance(c, pd.DataFrame) for c in chunks)
    assert len(chunks[1]) == 2


# ---------------------------------------------------------------------------
# Integration with DuckAPI.stream()
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_duckapi_stream_list_items(msal_cls):
    sp = _make_sp(msal_cls)

    pages = [
        [{"id": "i1", "fields": {"Title": "Alpha", "Status": "Active"}}],
        [{"id": "i2", "fields": {"Title": "Beta",  "Status": "Done"}}],
    ]

    with patch.object(sp, "_iter_pages", return_value=iter(pages)):
        duck = DuckAPI()

        def _stub(site_id=None, list_id=None, site_name=None, list_name=None,
                  column_names="display", limit=None):
            return []

        duck.register_api_function("list_items", _stub)
        duck.register_streaming_function("list_items", sp.iter_list_items)

        chunks = list(
            duck.stream(
                f"SELECT * FROM list_items(site_id='{SITE_ID}', list_id='{LIST_ID}',"
                f" column_names='internal')"
            )
        )

    assert len(chunks) == 2
    duck.close()


# ---------------------------------------------------------------------------
# Resolution by name (_resolve_site / _resolve_list)
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_resolve_site_by_name(msal_cls):
    sp = _make_sp(msal_cls)

    sites_data = [
        {"id": "s1", "displayName": "Intranet"},
        {"id": "s2", "displayName": "Marketing"},
    ]
    with patch.object(sp, "_fetch", return_value=sites_data):
        resolved = sp._resolve_site(None, "Marketing")

    assert resolved == "s2"


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_resolve_site_id_wins_over_name(msal_cls):
    sp = _make_sp(msal_cls)

    with patch.object(sp, "_fetch") as mock_fetch:
        resolved = sp._resolve_site("explicit-id", "Marketing")

    mock_fetch.assert_not_called()
    assert resolved == "explicit-id"


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_resolve_site_not_found_raises(msal_cls):
    sp = _make_sp(msal_cls)

    with patch.object(sp, "_fetch", return_value=[{"id": "s1", "displayName": "Intranet"}]):
        with pytest.raises(ValueError, match="not found"):
            sp._resolve_site(None, "Unknown")


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_resolve_list_by_name(msal_cls):
    sp = _make_sp(msal_cls)

    lists_data = [
        {"id": "l1", "displayName": "Tasks"},
        {"id": "l2", "displayName": "Documents"},
    ]
    with patch.object(sp, "_fetch", return_value=lists_data):
        resolved = sp._resolve_list(SITE_ID, None, "Tasks")

    assert resolved == "l1"


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_list_items_by_name_end_to_end(msal_cls):
    sp = _make_sp(msal_cls)

    sites_data = [{"id": SITE_ID, "displayName": "Intranet"}]
    lists_data = [{"id": LIST_ID, "displayName": "Tasks"}]
    columns_data = [{"name": "Title", "displayName": "Title"}]
    items_data = [{"id": "i1", "fields": {"Title": "Fix bug"}}]

    fetch_calls = iter([sites_data, lists_data, columns_data, items_data])
    with patch.object(sp, "_fetch", side_effect=fetch_calls):
        df = sp.list_items(site_name="Intranet", list_name="Tasks")

    assert list(df["Title"]) == ["Fix bug"]


# ---------------------------------------------------------------------------
# Default hostname + site_path on the constructor
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_default_site_used_when_no_site_given(msal_cls):
    """Without site_id/site_name, uses the constructor's default site."""
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
    msal_cls.return_value = mock_app

    sp = SharePoint(
        TENANT, CLIENT, "secret",
        hostname="company.sharepoint.com",
        site_path="/teams/myteam",
    )

    lists_data = [{"id": LIST_ID, "displayName": "Tasks"}]
    columns_data = [{"name": "Title", "displayName": "Title"}]
    items_data = [{"id": "i1", "fields": {"Title": "Item"}}]

    with patch.object(sp, "_get", return_value={"id": SITE_ID}) as mock_get, \
         patch.object(sp, "_fetch", side_effect=iter([lists_data, columns_data, items_data])):
        df = sp.list_items(list_name="Tasks")

    # Verifies the site was resolved via site_by_path with URL-encoding
    called_url = mock_get.call_args[0][0]
    assert "company.sharepoint.com" in called_url
    assert "%2Fteams%2Fmyteam" in called_url or "/teams/myteam" in called_url
    assert list(df["Title"]) == ["Item"]


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_default_site_cached(msal_cls):
    """_get to resolve the default site is only called once."""
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
    msal_cls.return_value = mock_app

    sp = SharePoint(TENANT, CLIENT, "secret",
                    hostname="company.sharepoint.com", site_path="/teams/myteam")

    with patch.object(sp, "_get", return_value={"id": SITE_ID}) as mock_get, \
         patch.object(sp, "_fetch", return_value=[]):
        try:
            sp.lists()
        except Exception:
            pass
        try:
            sp.lists()
        except Exception:
            pass

    assert mock_get.call_count == 1  # resolved only on the first call


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_site_name_as_path_uses_hostname(msal_cls):
    """site_name starting with / combines with the constructor's hostname."""
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
    msal_cls.return_value = mock_app

    sp = SharePoint(TENANT, CLIENT, "secret", hostname="company.sharepoint.com")

    with patch.object(sp, "_get", return_value={"id": SITE_ID}) as mock_get, \
         patch.object(sp, "_fetch", return_value=[{"id": "l1"}]):
        sp.lists(site_name="/sites/marketing")

    called_url = mock_get.call_args[0][0]
    assert "company.sharepoint.com" in called_url


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_site_name_as_path_without_hostname_raises(msal_cls):
    """site_name with / but no hostname on the constructor must raise ValueError."""
    sp = _make_sp(msal_cls)

    with pytest.raises(ValueError, match="hostname"):
        sp._resolve_site(None, "/sites/marketing")


# ---------------------------------------------------------------------------
# from_secret — construction from a credentials dict
# ---------------------------------------------------------------------------


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_from_secret_client_secret(msal_cls):
    msal_cls.return_value = MagicMock()

    sp = SharePoint.from_secret({
        "tenant_id": TENANT, "client_id": CLIENT, "client_secret": SECRET,
    })

    assert isinstance(sp, SharePoint)
    call_credential = msal_cls.call_args[1]["client_credential"]
    assert call_credential == SECRET


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_from_secret_thumbprint(msal_cls):
    msal_cls.return_value = MagicMock()

    sp = SharePoint.from_secret({
        "tenant_id": TENANT, "client_id": CLIENT,
        "thumbprint": "AA:BB", "private_key_pem": "-----BEGIN PRIVATE KEY-----\n...",
    })

    assert isinstance(sp, SharePoint)
    call_credential = msal_cls.call_args[1]["client_credential"]
    assert call_credential["thumbprint"] == "AABB"


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_from_secret_passes_overrides(msal_cls):
    msal_cls.return_value = MagicMock()

    sp = SharePoint.from_secret(
        {"tenant_id": TENANT, "client_id": CLIENT, "client_secret": SECRET},
        hostname="company.sharepoint.com",
        site_path="/teams/myteam",
    )

    assert sp._default_hostname == "company.sharepoint.com"
    assert sp._default_site_path == "/teams/myteam"


def test_from_secret_unrecognized_credentials_raises():
    with pytest.raises(ValueError, match="does not contain recognized credentials"):
        SharePoint.from_secret({"tenant_id": TENANT, "client_id": CLIENT})

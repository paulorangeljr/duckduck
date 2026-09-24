"""Testes unitários para o wrapper SharePoint."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from duckduck import DuckAPI, SharePoint


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
    """Cria SharePoint com MSAL mockado."""
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok123"}
    msal_cls.return_value = mock_app
    return SharePoint(TENANT, CLIENT, SECRET)


# ---------------------------------------------------------------------------
# Autenticação
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

    with pytest.raises(ValueError, match="Falha na autenticação"):
        sp._get_token()


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_from_thumbprint(msal_cls):
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok"}
    msal_cls.return_value = mock_app

    sp = SharePoint.from_thumbprint(TENANT, CLIENT, "AA:BB:CC", "-----BEGIN PRIVATE KEY-----\n...")
    assert sp._get_token() == "tok"
    # Verifica que thumbprint foi normalizado (sem ':', maiúsculo)
    call_credential = msal_cls.call_args[1]["client_credential"]
    assert call_credential["thumbprint"] == "AABBCC"


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
# _iter_pages — paginação nextLink
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
# lists() e list_items()
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
        df = sp.list_items(site_id=SITE_ID, list_id=LIST_ID)

    url = mock_fetch.call_args[0][0]
    assert "expand=fields" in url
    assert "Title" in df.columns


# ---------------------------------------------------------------------------
# drive_items() — is_file derivado
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
        chunks = list(sp.iter_list_items(site_id=SITE_ID, list_id=LIST_ID))

    assert len(chunks) == 2
    assert all(isinstance(c, pd.DataFrame) for c in chunks)
    assert len(chunks[1]) == 2


# ---------------------------------------------------------------------------
# Integração com DuckAPI.stream()
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

        def _stub(site_id=None, list_id=None, site_name=None, list_name=None, limit=None):
            return []

        duck.register_api_function("list_items", _stub)
        duck.register_streaming_function("list_items", sp.iter_list_items)

        chunks = list(
            duck.stream(
                f"SELECT * FROM list_items(site_id='{SITE_ID}', list_id='{LIST_ID}')"
            )
        )

    assert len(chunks) == 2
    duck.close()


# ---------------------------------------------------------------------------
# Resolução por nome (_resolve_site / _resolve_list)
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
        with pytest.raises(ValueError, match="não encontrado"):
            sp._resolve_site(None, "Unknown")


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_resolve_list_by_name(msal_cls):
    sp = _make_sp(msal_cls)

    lists_data = [
        {"id": "l1", "displayName": "Tarefas"},
        {"id": "l2", "displayName": "Documentos"},
    ]
    with patch.object(sp, "_fetch", return_value=lists_data):
        resolved = sp._resolve_list(SITE_ID, None, "Tarefas")

    assert resolved == "l1"


@patch("duckduck.sharepoint.msal.ConfidentialClientApplication")
def test_list_items_by_name_end_to_end(msal_cls):
    sp = _make_sp(msal_cls)

    sites_data = [{"id": SITE_ID, "displayName": "Intranet"}]
    lists_data = [{"id": LIST_ID, "displayName": "Tarefas"}]
    items_data = [{"id": "i1", "fields": {"Title": "Fix bug"}}]

    fetch_calls = iter([sites_data, lists_data, items_data])
    with patch.object(sp, "_fetch", side_effect=fetch_calls):
        df = sp.list_items(site_name="Intranet", list_name="Tarefas")

    assert list(df["Title"]) == ["Fix bug"]

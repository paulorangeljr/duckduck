"""
SharePoint wrapper for use with DuckAPI.

Authenticates via the Microsoft Identity Platform (Azure AD) and queries
SharePoint Lists and files through the Microsoft Graph API v1.0.

Authentication modes
---------------------
- Client secret:        ``SharePoint(tenant_id, client_id, client_secret)``
- Thumbprint + PEM key: ``SharePoint.from_thumbprint(...)``
- PFX/P12 file:         ``SharePoint.from_pfx(...)``  [pip install cryptography]
- PEM cert + PEM key:   ``SharePoint.from_pem_cert(...)``  [pip install cryptography]

Query examples
---------------
::

    sp   = SharePoint(tenant_id, client_id, client_secret)
    duck = DuckAPI()

    duck.register_api_function("sites",       sp.sites)
    duck.register_api_function("lists",       sp.lists)
    duck.register_api_function("list_items",  sp.list_items)
    duck.register_api_function("drives",      sp.drives)
    duck.register_api_function("drive_items", sp.drive_items)

    # All sites — the returned id is the site_id used by the other methods
    duck.sql("SELECT id, displayName, webUrl FROM sites").df()

    # Lists from a site (site_id is structural → inline)
    duck.sql("SELECT * FROM lists(site_id='abc123')").df()

    # Items with a column filter via WHERE push-down would be client-side
    # because Graph doesn't filter custom fields server-side
    duck.sql(
        "SELECT * FROM list_items(site_id='abc123', list_id='def456')"
        " WHERE Status = 'Active' LIMIT 50"
    ).df()

    # Files from a drive
    duck.sql(
        "SELECT name, size, webUrl"
        " FROM drive_items(site_id='abc123', drive_id='ghi789')"
        " WHERE is_file = true"
    ).df()

    # Streaming a large list
    duck.register_streaming_function("list_items", sp.iter_list_items)
    for chunk in duck.stream(
        "SELECT * FROM list_items(site_id='abc123', list_id='def456')"
    ):
        display(chunk)
"""

import re
import threading
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote

import pandas as pd
import requests

try:
    import msal
except ImportError as exc:
    raise ImportError(
        "msal is required for SharePoint authentication.\n"
        "Install with:  pip install msal"
    ) from exc


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
DEFAULT_PAGE_SIZE = 200


def _normalize_pem(pem_text: str) -> str:
    """
    Fixes a PEM whose line breaks came through as the literal two-character
    sequence ``\\n`` instead of a real newline (``\\x0a``).

    This happens often when the key/certificate passes through an AWS
    Secrets Manager secret (or another environment variable) with double
    escaping: the JSON has already been decoded, but a literal ``\\n``
    was left inside the string instead of the line break it was meant to
    represent.

    A PEM's base64 body never contains ``\\`` (the alphabet is A-Z a-z
    0-9 + / =), so the heuristic — only rewrite when there's no real
    newline — is safe.
    """
    if "\\n" in pem_text and "\n" not in pem_text:
        return pem_text.replace("\\n", "\n")
    return pem_text


class SharePoint:
    """
    Client for SharePoint via the Microsoft Graph API.

    See the module docstring for full examples.
    """

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        default_page_size: int = DEFAULT_PAGE_SIZE,
        hostname: Optional[str] = None,
        site_path: Optional[str] = None,
    ):
        """
        Authentication via client secret.

        Parameters
        ----------
        hostname : str, optional
            Hostname of the SharePoint tenant. E.g. ``mycompany.sharepoint.com``.
        site_path : str, optional
            Path of the default site. E.g. ``/teams/myteam``.
            When both are provided, every query uses this site by default
            without needing ``site_id`` or ``site_name``.
        """
        self._setup(tenant_id, client_id, client_secret, default_page_size,
                    hostname, site_path)

    @classmethod
    def from_thumbprint(
        cls,
        tenant_id: str,
        client_id: str,
        thumbprint: str,
        private_key_pem: str,
        passphrase: Optional[bytes] = None,
        default_page_size: int = DEFAULT_PAGE_SIZE,
        hostname: Optional[str] = None,
        site_path: Optional[str] = None,
    ) -> "SharePoint":
        """
        Authentication via SHA-1 thumbprint + PEM private key.

        Direct equivalent of the Python MSAL credential::

            {"thumbprint": "ABC123...", "private_key": "-----BEGIN PRIVATE KEY-----\\n..."}

        Parameters
        ----------
        thumbprint : str
            SHA-1 fingerprint in hex (with or without ``:``, upper or
            lower case).
        private_key_pem : str
            PEM content of the private key **or** a path to a ``.pem``/``.key``
            file. If the string doesn't start with ``-----``, it's
            treated as a file path.
            Line breaks that arrive as a literal ``\\n`` (common with AWS
            Secrets Manager secrets) are fixed automatically.
        passphrase : bytes, optional
            Password for the private key, if encrypted.
        hostname / site_path : str, optional
            Default site — see ``__init__``.
        """
        import pathlib

        if not private_key_pem.strip().startswith("-----"):
            private_key_pem = pathlib.Path(private_key_pem).read_text()
        private_key_pem = _normalize_pem(private_key_pem)

        credential: Dict[str, Any] = {
            "thumbprint": thumbprint.replace(":", "").upper(),
            "private_key": private_key_pem,
        }
        if passphrase is not None:
            credential["passphrase"] = passphrase
        obj = cls.__new__(cls)
        obj._setup(tenant_id, client_id, credential, default_page_size, hostname, site_path)
        return obj

    @classmethod
    def from_pfx(
        cls,
        tenant_id: str,
        client_id: str,
        pfx_path: str,
        pfx_password: Optional[str] = None,
        default_page_size: int = DEFAULT_PAGE_SIZE,
        hostname: Optional[str] = None,
        site_path: Optional[str] = None,
    ) -> "SharePoint":
        """
        Authentication via a PFX/P12 file.

        Requires: ``pip install cryptography``

        Parameters
        ----------
        hostname / site_path : str, optional
            Default site — see ``__init__``.
        """
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                NoEncryption,
                PrivateFormat,
            )
            from cryptography.hazmat.primitives.serialization.pkcs12 import load_pkcs12
        except ImportError as exc:
            raise ImportError(
                "cryptography is required for PFX auth.\n"
                "Install with:  pip install cryptography"
            ) from exc

        import pathlib

        pfx_bytes = pathlib.Path(pfx_path).read_bytes()
        pwd = pfx_password.encode() if pfx_password else None
        private_key, certificate, _ = load_pkcs12(pfx_bytes, pwd)

        pem_key = private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        ).decode()
        thumbprint = certificate.fingerprint(hashes.SHA1()).hex()

        return cls.from_thumbprint(
            tenant_id, client_id, thumbprint, pem_key,
            default_page_size=default_page_size,
            hostname=hostname,
            site_path=site_path,
        )

    @classmethod
    def from_pem_cert(
        cls,
        tenant_id: str,
        client_id: str,
        private_key_pem: str,
        cert_pem: str,
        default_page_size: int = DEFAULT_PAGE_SIZE,
        hostname: Optional[str] = None,
        site_path: Optional[str] = None,
    ) -> "SharePoint":
        """
        Authentication via PEM (RSA private key + X.509 certificate).

        Requires: ``pip install cryptography``

        Parameters
        ----------
        hostname / site_path : str, optional
            Default site — see ``__init__``.

        Notes
        -----
        Line breaks that arrive as a literal ``\\n`` in ``private_key_pem``
        or ``cert_pem`` (common with AWS Secrets Manager secrets) are
        fixed automatically.
        """
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
        except ImportError as exc:
            raise ImportError(
                "cryptography is required for PEM cert auth.\n"
                "Install with:  pip install cryptography"
            ) from exc

        cert_pem = _normalize_pem(cert_pem)
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        thumbprint = cert.fingerprint(hashes.SHA1()).hex()
        return cls.from_thumbprint(
            tenant_id, client_id, thumbprint, private_key_pem,
            default_page_size=default_page_size,
            hostname=hostname,
            site_path=site_path,
        )

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "SharePoint":
        """
        Builds a SharePoint client from a credentials dict (e.g. an AWS
        Secrets Manager secret via ``SecretsManager.get_secret``).

        Detects the authentication mode from the keys present in
        ``secret``:

        - ``client_secret``                                   → client secret
        - ``thumbprint`` + ``private_key_pem``                 → ``from_thumbprint``
        - ``pfx_path``                                         → ``from_pfx``
        - ``private_key_pem`` + ``cert_pem`` (no thumbprint)   → ``from_pem_cert``

        ``tenant_id`` and ``client_id`` come from the secret by default,
        but can be overridden via ``overrides`` (as can ``hostname``,
        ``site_path``, etc.).
        """
        tenant_id = overrides.pop("tenant_id", None) or secret["tenant_id"]
        client_id = overrides.pop("client_id", None) or secret["client_id"]

        if "client_secret" in secret:
            return cls(tenant_id, client_id, secret["client_secret"], **overrides)

        if "thumbprint" in secret and "private_key_pem" in secret:
            return cls.from_thumbprint(
                tenant_id, client_id,
                secret["thumbprint"], secret["private_key_pem"],
                passphrase=secret.get("passphrase"),
                **overrides,
            )

        if "pfx_path" in secret:
            return cls.from_pfx(
                tenant_id, client_id, secret["pfx_path"],
                pfx_password=secret.get("pfx_password"),
                **overrides,
            )

        if "private_key_pem" in secret and "cert_pem" in secret:
            return cls.from_pem_cert(
                tenant_id, client_id,
                secret["private_key_pem"], secret["cert_pem"],
                **overrides,
            )

        raise ValueError(
            "SharePoint secret does not contain recognized credentials. "
            "Use client_secret, thumbprint+private_key_pem, pfx_path or "
            "private_key_pem+cert_pem."
        )

    # ------------------------------------------------------------------
    # Internal setup
    # ------------------------------------------------------------------

    def _setup(
        self,
        tenant_id: str,
        client_id: str,
        credential: Any,
        default_page_size: int,
        hostname: Optional[str] = None,
        site_path: Optional[str] = None,
    ) -> None:
        self.default_page_size = default_page_size
        self._lock = threading.Lock()
        self._default_hostname = hostname
        self._default_site_path = site_path
        self._default_site_id: Optional[str] = None  # resolved lazily
        self._column_map_cache: Dict[str, Dict[str, str]] = {}  # "{site_id}:{list_id}" -> {internal: displayName}
        # ConfidentialClientApplication keeps a token cache in memory
        self._msal_app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=credential,
        )

    # ------------------------------------------------------------------
    # Token
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        with self._lock:
            # MSAL returns the cached token if it's still valid
            result = self._msal_app.acquire_token_silent(GRAPH_SCOPE, account=None)
            if result and "access_token" in result:
                return result["access_token"]

            result = self._msal_app.acquire_token_for_client(scopes=GRAPH_SCOPE)
            if "error" in result:
                raise ValueError(
                    f"Azure AD authentication failed: "
                    f"{result.get('error')} — {result.get('error_description')}"
                )
            return result["access_token"]

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, url: str) -> Dict:
        token = self._get_token()
        r = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def _with_top(self, url: str, top: int) -> str:
        """Adds or replaces $top in the URL."""
        if "$top=" in url:
            return re.sub(r"\$top=\d+", f"$top={top}", url)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}$top={top}"

    def _iter_pages(self, url: str) -> Iterator[List[Dict]]:
        """
        Generator that follows @odata.nextLink until there's nothing left.

        Unlike InsightVM (which uses page/totalPages), the Graph API
        uses @odata.nextLink for pagination. Yields a list of resources
        per page.
        """
        if "$top=" not in url:
            url = self._with_top(url, self.default_page_size)

        next_url: Optional[str] = url
        while next_url:
            payload = self._get(next_url)
            items = payload.get("value", [])
            if items:
                yield items
            next_url = payload.get("@odata.nextLink")

    def _fetch(self, url: str, limit: Optional[int] = None) -> List[Dict]:
        """
        A single request if limit is set; full pagination otherwise.
        """
        if limit is not None:
            payload = self._get(self._with_top(url, limit))
            return payload.get("value", [])

        all_items: List[Dict] = []
        for page in self._iter_pages(url):
            all_items.extend(page)
        return all_items

    # ------------------------------------------------------------------
    # DataFrame helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_list_items(
        items: List[Dict],
        column_map: Optional[Dict[str, str]] = None,
    ) -> pd.DataFrame:
        """
        Elevates the fields of the ``fields`` sub-object into top-level
        columns.

        The Graph API returns::

            {"id": "1", "fields": {"field_1": "foo", "Status": "Active"}}

        This becomes::

            _item_id | _created_at | Title | Status
            1        | 2024-01-01  | foo   | Active

        Internal Graph metadata is prefixed with ``_``.
        The list's fields end up at the top level — allowing
        ``WHERE Status = 'foo'`` without qualifying with ``fields_``.

        Parameters
        ----------
        column_map : dict, optional
            ``{internal_name: displayName}``. When provided, fields are
            renamed before becoming columns — see ``_get_column_display_map``.
            Fields with no entry in the map keep their original name.
        """
        records = []
        for item in items:
            record: Dict[str, Any] = {
                "_item_id": item.get("id"),
                "_created_at": item.get("createdDateTime"),
                "_modified_at": item.get("lastModifiedDateTime"),
                "_web_url": item.get("webUrl"),
            }
            fields = item.get("fields", {})
            if column_map:
                fields = {column_map.get(k, k): v for k, v in fields.items()}
            record.update(fields)
            records.append(record)
        return pd.json_normalize(records, sep="_")

    # ------------------------------------------------------------------
    # Name → ID resolution
    # ------------------------------------------------------------------

    def _resolve_site(
        self,
        site_id: Optional[str],
        site_name: Optional[str],
    ) -> str:
        """
        Returns a site_id.

        Priority:
        1. Explicit ``site_id``
        2. ``site_name`` starting with ``/`` → combined with the constructor's
           ``hostname``.
           E.g. site_name='/teams/myteam'  +  hostname='company.sharepoint.com'
        3. ``site_name`` without ``/`` → looked up by ``displayName`` across
           all sites
        4. ``hostname`` + ``site_path`` set on the constructor (default site)
        5. Error
        """
        if site_id:
            return site_id

        if site_name:
            if site_name.startswith("/"):
                # Relative path — combined with the constructor's hostname
                if not self._default_hostname:
                    raise ValueError(
                        f"site_name='{site_name}' is a path and requires "
                        "hostname to be set on the SharePoint constructor."
                    )
                url = (
                    f"{GRAPH_BASE}/sites/{self._default_hostname}:"
                    f"{quote(site_name)}"
                )
                return self._get(url)["id"]

            # Display name — search across all sites
            all_sites = self._fetch(f"{GRAPH_BASE}/sites?search=*")
            name_lower = site_name.lower()
            for s in all_sites:
                if s.get("displayName", "").lower() == name_lower:
                    return s["id"]
            raise ValueError(
                f"Site '{site_name}' not found. "
                "Use SELECT * FROM sites to see the available names."
            )

        if self._default_hostname and self._default_site_path:
            if self._default_site_id is None:
                url = (
                    f"{GRAPH_BASE}/sites/{self._default_hostname}:"
                    f"{quote(self._default_site_path)}"
                )
                self._default_site_id = self._get(url)["id"]
            return self._default_site_id

        raise ValueError(
            "Provide site_id, site_name, or initialize SharePoint "
            "with hostname and site_path."
        )

    def _resolve_list(
        self,
        site_id: str,
        list_id: Optional[str],
        list_name: Optional[str],
    ) -> str:
        """
        Returns a list_id (always a GUID, safe for URLs).

        Accepts:
        - ``list_id``: GUID or already URL-encoded name → used directly
        - ``list_name``: display name (with spaces, accents, etc.) → looked
          up by ``displayName``; the returned GUID is then used in the URL.
        """
        if list_id:
            return list_id
        if not list_name:
            raise ValueError("Provide list_id or list_name.")
        all_lists = self._fetch(f"{GRAPH_BASE}/sites/{site_id}/lists")
        name_lower = list_name.lower()
        for lst in all_lists:
            if lst.get("displayName", "").lower() == name_lower:
                return lst["id"]
        raise ValueError(
            f"List '{list_name}' not found in site '{site_id}'. "
            "Use SELECT * FROM lists(site_id=...) to see the available lists."
        )

    def _get_column_display_map(self, site_id: str, list_id: str) -> Dict[str, str]:
        """
        Returns ``{internal_name: displayName}`` for a list's columns,
        cached per (site_id, list_id).

        The Graph API returns an item's fields keyed by the column's
        **internal** name (``field_1``, ``OData__ColorTag``, etc.), which
        rarely matches the name shown in the SharePoint UI. This map lets
        the data be presented with the same names the user sees in the
        browser.
        """
        cache_key = f"{site_id}:{list_id}"
        if cache_key not in self._column_map_cache:
            columns = self._fetch(f"{GRAPH_BASE}/sites/{site_id}/lists/{list_id}/columns")
            self._column_map_cache[cache_key] = {
                c["name"]: (c.get("displayName") or c["name"])
                for c in columns
                if c.get("name")
            }
        return self._column_map_cache[cache_key]

    # ------------------------------------------------------------------
    # Sites
    # ------------------------------------------------------------------

    def sites(
        self,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists all accessible SharePoint sites (requires Sites.Read.All).

        Use the returned ``id`` as ``site_id``, or the ``displayName``
        as ``site_name``, in the other methods.
        """
        url = f"{GRAPH_BASE}/sites?search=*"
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    def site_by_path(
        self,
        hostname: str,
        site_path: str,
    ) -> pd.DataFrame:
        """
        Looks up a site by hostname and path.

        Parameters
        ----------
        hostname : str
            E.g. ``contoso.sharepoint.com``
        site_path : str
            E.g. ``/sites/marketing``
        """
        url = f"{GRAPH_BASE}/sites/{hostname}:{quote(site_path)}"
        return pd.json_normalize([self._get(url)], sep="_")

    # ------------------------------------------------------------------
    # SharePoint Lists
    # ------------------------------------------------------------------

    def lists(
        self,
        site_id: Optional[str] = None,
        site_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        All SharePoint Lists in a site.

        Parameters
        ----------
        site_id : str, optional
            Site ID. Use ``site_id`` **or** ``site_name``.
        site_name : str, optional
            Site display name (``displayName``). Automatically looks up
            the ID.

        Examples
        --------
        ::

            # by ID (inline)
            duck.sql("SELECT * FROM lists(site_id='abc123')")

            # by name (WHERE push-down)
            duck.sql("SELECT * FROM lists WHERE site_name = 'Intranet'")
        """
        sid = self._resolve_site(site_id, site_name)
        url = f"{GRAPH_BASE}/sites/{sid}/lists"
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    def list_columns(
        self,
        site_id: Optional[str] = None,
        list_id: Optional[str] = None,
        site_name: Optional[str] = None,
        list_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Columns (schema) of a SharePoint List.

        Accepts direct IDs or display names (automatic lookup).

        Parameters
        ----------
        site_id / site_name : str
            Site identification — provide one of the two.
        list_id / list_name : str
            List identification — provide one of the two.
        """
        sid = self._resolve_site(site_id, site_name)
        lid = self._resolve_list(sid, list_id, list_name)
        url = f"{GRAPH_BASE}/sites/{sid}/lists/{lid}/columns"
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    def list_items(
        self,
        site_id: Optional[str] = None,
        list_id: Optional[str] = None,
        site_name: Optional[str] = None,
        list_name: Optional[str] = None,
        column_names: str = "display",
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Items of a SharePoint List with expanded fields.

        The list's custom fields show up as top-level columns. Internal
        Graph metadata is prefixed with ``_`` (``_item_id``,
        ``_created_at``, etc.).

        Accepts direct IDs or display names (automatic lookup).

        Parameters
        ----------
        site_id / site_name : str
            Site identification — provide one of the two.
        list_id / list_name : str
            List identification — provide one of the two.
        column_names : {"display", "internal"}
            ``"display"`` (default): uses the same column name shown in
            the SharePoint UI (e.g. ``Status``), resolved via
            ``/lists/{id}/columns``. ``"internal"``: uses the Graph API's
            raw name (e.g. ``field_1``), with no extra request cost.
            Either way, the query's ``WHERE``/``SELECT`` operate on the
            already-resolved names — you don't need to know the other one.

        Examples
        --------
        ::

            # by IDs (inline)
            duck.sql("SELECT * FROM list_items(site_id='abc', list_id='def')")

            # by names (WHERE push-down — automatic lookup)
            duck.sql(
                "SELECT * FROM list_items"
                " WHERE site_name = 'Intranet' AND list_name = 'Tasks'"
                " AND Status = 'Active'"
            )

            # Graph internal names, without a columns lookup
            duck.sql(
                "SELECT * FROM list_items(column_names='internal')"
                " WHERE site_name = 'Intranet' AND list_name = 'Tasks'"
            )
        """
        if column_names not in ("display", "internal"):
            raise ValueError("column_names must be 'display' or 'internal'.")
        sid = self._resolve_site(site_id, site_name)
        lid = self._resolve_list(sid, list_id, list_name)
        column_map = (
            self._get_column_display_map(sid, lid) if column_names == "display" else None
        )
        url = f"{GRAPH_BASE}/sites/{sid}/lists/{lid}/items?expand=fields"
        return self._normalize_list_items(self._fetch(url, limit=limit), column_map=column_map)

    # ------------------------------------------------------------------
    # Drives / Files
    # ------------------------------------------------------------------

    def drives(
        self,
        site_id: Optional[str] = None,
        site_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Document libraries (drives) of a site.

        Parameters
        ----------
        site_id / site_name : str
            Site identification — provide one of the two.
        """
        sid = self._resolve_site(site_id, site_name)
        url = f"{GRAPH_BASE}/sites/{sid}/drives"
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    def drive_items(
        self,
        site_id: Optional[str] = None,
        drive_id: Optional[str] = None,
        site_name: Optional[str] = None,
        folder_path: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Files and folders inside a drive (document library).

        Useful columns: ``name``, ``size``, ``webUrl``, ``is_file``,
        ``file_mimeType``, ``folder_childCount``, ``lastModifiedDateTime``.

        Parameters
        ----------
        site_id / site_name : str
            Site identification — provide one of the two.
        drive_id : str
            Drive ID (required). Obtain it via ``SELECT * FROM drives(...)``.
        folder_path : str, optional
            Path relative to the root. E.g. ``/Documents/Reports``.
            Without this parameter, lists the drive's root.
        """
        if not drive_id:
            raise ValueError("drive_id is required.")
        sid = self._resolve_site(site_id, site_name)
        if folder_path:
            path = f"/sites/{sid}/drives/{drive_id}/root:{folder_path}:/children"
        else:
            path = f"/sites/{sid}/drives/{drive_id}/root/children"

        items = self._fetch(f"{GRAPH_BASE}{path}", limit=limit)
        df = pd.json_normalize(items, sep="_")

        if not df.empty:
            df["is_file"] = (
                df["file_mimeType"].notna()
                if "file_mimeType" in df.columns
                else pd.Series(True, index=df.index)
            )

        return df

    def search_files(
        self,
        site_id: Optional[str] = None,
        query: Optional[str] = None,
        site_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Searches files in a site via the Graph API.

        Parameters
        ----------
        site_id / site_name : str
            Site identification — provide one of the two.
        query : str
            Search term (required).
        """
        if not query:
            raise ValueError("query is required.")
        sid = self._resolve_site(site_id, site_name)
        url = f"{GRAPH_BASE}/sites/{sid}/drive/search(q='{query}')"
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    def file_versions(
        self,
        site_id: Optional[str] = None,
        drive_id: Optional[str] = None,
        item_id: Optional[str] = None,
        site_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Versions of a specific file.

        Parameters
        ----------
        site_id / site_name : str
            Site identification — provide one of the two.
        drive_id, item_id : str
            Required. ``item_id`` comes from the ``id`` column of
            ``drive_items()``.
        """
        if not drive_id or not item_id:
            raise ValueError("drive_id and item_id are required.")
        sid = self._resolve_site(site_id, site_name)
        url = (
            f"{GRAPH_BASE}/sites/{sid}"
            f"/drives/{drive_id}/items/{item_id}/versions"
        )
        return pd.json_normalize(self._fetch(url, limit=limit), sep="_")

    # ------------------------------------------------------------------
    # Streaming (iter_*) — for use with DuckAPI.stream()
    # ------------------------------------------------------------------

    def iter_sites(self) -> Iterator[pd.DataFrame]:
        """Yields one page of sites at a time."""
        for page in self._iter_pages(f"{GRAPH_BASE}/sites?search=*"):
            yield pd.json_normalize(page, sep="_")

    def iter_lists(
        self,
        site_id: Optional[str] = None,
        site_name: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of lists at a time."""
        sid = self._resolve_site(site_id, site_name)
        for page in self._iter_pages(f"{GRAPH_BASE}/sites/{sid}/lists"):
            yield pd.json_normalize(page, sep="_")

    def iter_list_items(
        self,
        site_id: Optional[str] = None,
        list_id: Optional[str] = None,
        site_name: Optional[str] = None,
        list_name: Optional[str] = None,
        column_names: str = "display",
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of items at a time (fields expanded).

        See ``list_items`` for the meaning of ``column_names``.
        """
        if column_names not in ("display", "internal"):
            raise ValueError("column_names must be 'display' or 'internal'.")
        sid = self._resolve_site(site_id, site_name)
        lid = self._resolve_list(sid, list_id, list_name)
        column_map = (
            self._get_column_display_map(sid, lid) if column_names == "display" else None
        )
        url = f"{GRAPH_BASE}/sites/{sid}/lists/{lid}/items?expand=fields"
        for page in self._iter_pages(url):
            yield self._normalize_list_items(page, column_map=column_map)

    def iter_drive_items(
        self,
        site_id: Optional[str] = None,
        drive_id: Optional[str] = None,
        site_name: Optional[str] = None,
        folder_path: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of files/folders at a time."""
        if not drive_id:
            raise ValueError("drive_id is required.")
        sid = self._resolve_site(site_id, site_name)
        if folder_path:
            path = f"/sites/{sid}/drives/{drive_id}/root:{folder_path}:/children"
        else:
            path = f"/sites/{sid}/drives/{drive_id}/root/children"

        for page in self._iter_pages(f"{GRAPH_BASE}{path}"):
            df = pd.json_normalize(page, sep="_")
            if not df.empty and "file_mimeType" in df.columns:
                df["is_file"] = df["file_mimeType"].notna()
            yield df

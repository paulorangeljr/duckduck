"""
Axonius REST API v2 wrapper for use with DuckAPI.

.. warning::
   Axonius's public REST API v2 documentation could not be fetched live
   while building this wrapper (network policy in that session blocked
   ``docs.axonius.com``); the request/response shape below is based on
   published examples (the ``api-key``/``api-secret`` header auth and the
   ``entity_request_schema`` POST body are confirmed from Axonius's own
   sample requests) rather than a full read of the reference docs.
   Verify against your instance's own API reference — usually at
   ``https://<instance>/api-docs`` — before relying on this in
   production, particularly the exact response envelope
   ``_normalize_assets`` assumes and the AQL syntax used by the
   ``hostname=``/``os_type=`` convenience filters below. Both are easy to
   adjust: change ``_normalize_assets`` for the response shape, and the
   AQL fragments inside ``devices()``/``users()`` for the query syntax.

Authenticates via the ``api-key``/``api-secret`` HTTP headers and
queries asset endpoints (``/api/devices``, ``/api/users``) via Axonius's
POST-based query API, which takes a JSON body like::

    {
        "meta": null,
        "data": {
            "type": "entity_request_schema",
            "attributes": {
                "page": {"offset": 0, "limit": 100},
                "filter": "<AQL query>"
            }
        }
    }

Usage convention
-----------------
Same convention as the rest of ``duckduck``: column filters
(``hostname``, ``os_type``, etc.) belong in ``WHERE`` and are pushed down
by translating them into AQL fragments ANDed together. Pass a raw AQL
string via ``filter=`` for anything the convenience filters can't express.

Supported query examples
-------------------------
::

    SELECT * FROM devices WHERE hostname = 'web-prod-01' LIMIT 50
    SELECT * FROM users WHERE username = 'jdoe'

    # Raw AQL escape hatch
    SELECT * FROM devices(filter='specific_data.data.os.type == "Windows"')
"""

from typing import Any, Dict, Iterator, List, Optional

import pandas as pd
import requests


class Axonius:
    """
    Client for the Axonius REST API v2 (asset management platform).

    Parameters
    ----------
    instance : str
        Hostname of the Axonius instance (no protocol), e.g.
        ``"axonius.mycompany.com"``.
    api_key : str
    api_secret : str
    default_page_size : int
        Page size used when ``limit`` isn't passed by DuckAPI.
    verify : bool
        TLS certificate verification.
    """

    def __init__(
        self,
        instance: str,
        api_key: str,
        api_secret: str,
        default_page_size: int = 200,
        verify: bool = True,
    ):
        self.base_url = f"https://{instance}/api"
        self.default_page_size = default_page_size

        self.session = requests.Session()
        self.session.headers.update({
            "api-key": api_key,
            "api-secret": api_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.session.verify = verify

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "Axonius":
        """
        Builds Axonius from a credentials dict (e.g. a secret fetched by
        ``auto_register()``).

        Parameters
        ----------
        secret : dict
            Expected keys: ``instance``, ``api_key``, ``api_secret``.
        """
        instance = overrides.pop("instance", None) or secret["instance"]
        api_key = overrides.pop("api_key", None) or secret["api_key"]
        api_secret = overrides.pop("api_secret", None) or secret["api_secret"]
        return cls(instance, api_key, api_secret, **overrides)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        r = self.session.post(f"{self.base_url}{path}", json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _build_body(offset: int, limit: int, filter_aql: Optional[str]) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {"page": {"offset": offset, "limit": limit}}
        if filter_aql:
            attributes["filter"] = filter_aql
        return {"meta": None, "data": {"type": "entity_request_schema", "attributes": attributes}}

    def _iter_pages(self, path: str, filter_aql: Optional[str] = None) -> Iterator[List[Dict]]:
        """Generator that pages via offset/limit, yielding one page at a time."""
        offset = 0
        while True:
            body = self._build_body(offset, self.default_page_size, filter_aql)
            payload = self._post(path, body)
            items = payload.get("data", [])
            if items:
                yield items
            if len(items) < self.default_page_size:
                break
            offset += self.default_page_size

    def _fetch(
        self, path: str, filter_aql: Optional[str] = None, limit: Optional[int] = None
    ) -> List[Dict]:
        """A single request if ``limit`` is set; full offset/limit pagination otherwise."""
        if limit is not None:
            payload = self._post(path, self._build_body(0, limit, filter_aql))
            return payload.get("data", [])

        all_items: List[Dict] = []
        for page in self._iter_pages(path, filter_aql=filter_aql):
            all_items.extend(page)
        return all_items

    @staticmethod
    def _build_aql(**filters: Any) -> Optional[str]:
        """Joins AQL fragments for non-None kwargs with ' and '."""
        parts = [v for v in filters.values() if v]
        return " and ".join(parts) if parts else None

    @staticmethod
    def _normalize_assets(items: List[Dict]) -> pd.DataFrame:
        """
        Elevates each asset's ``attributes`` object into top-level
        columns, prefixing ``id``/``type`` metadata with ``_``. See the
        module warning: adjust this if your instance's response envelope
        differs from the assumed ``{"id", "type", "attributes"}`` shape.
        """
        records = []
        for item in items:
            record: Dict[str, Any] = {
                "_id": item.get("id"),
                "_type": item.get("type"),
            }
            record.update(item.get("attributes", {}))
            records.append(record)
        return pd.json_normalize(records, sep="_")

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    def devices(
        self,
        filter: Optional[str] = None,
        hostname: Optional[str] = None,
        os_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists device assets (``/api/devices``).

        Parameters
        ----------
        filter : str, optional
            Raw AQL query, ANDed with the convenience filters below if
            both are given.
        hostname : str, optional
            Exact hostname match (``specific_data.data.hostname``).
        os_type : str, optional
            OS type, e.g. ``"Windows"``, ``"Linux"`` (``specific_data.data.os.type``).
        limit : int, optional
            Maximum number of records.
        """
        aql_parts = {
            "filter": filter,
            "hostname": f'specific_data.data.hostname == "{hostname}"' if hostname else None,
            "os_type": f'specific_data.data.os.type == "{os_type}"' if os_type else None,
        }
        aql = self._build_aql(**aql_parts)
        items = self._fetch("/devices", filter_aql=aql, limit=limit)
        return self._normalize_assets(items)

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def users(
        self,
        filter: Optional[str] = None,
        username: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists user assets (``/api/users``).

        Parameters
        ----------
        filter : str, optional
            Raw AQL query, ANDed with ``username`` if both are given.
        username : str, optional
            Exact username match (``specific_data.data.username``).
        limit : int, optional
            Maximum number of records.
        """
        aql_parts = {
            "filter": filter,
            "username": f'specific_data.data.username == "{username}"' if username else None,
        }
        aql = self._build_aql(**aql_parts)
        items = self._fetch("/users", filter_aql=aql, limit=limit)
        return self._normalize_assets(items)

    # ------------------------------------------------------------------
    # Streaming (iter_*) — for use with DuckAPI.stream()
    # ------------------------------------------------------------------

    def iter_devices(
        self, hostname: Optional[str] = None, os_type: Optional[str] = None
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of device assets at a time."""
        aql = self._build_aql(
            hostname=f'specific_data.data.hostname == "{hostname}"' if hostname else None,
            os_type=f'specific_data.data.os.type == "{os_type}"' if os_type else None,
        )
        for page in self._iter_pages("/devices", filter_aql=aql):
            yield self._normalize_assets(page)

    def iter_users(self, username: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """Yields one page of user assets at a time."""
        aql = self._build_aql(
            username=f'specific_data.data.username == "{username}"' if username else None,
        )
        for page in self._iter_pages("/users", filter_aql=aql):
            yield self._normalize_assets(page)

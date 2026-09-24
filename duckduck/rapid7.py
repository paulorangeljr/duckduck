"""
InsightVM API wrapper for use with DuckAPI.

Usage convention
-----------------
The ``FROM func(param=val)`` syntax is reserved for **structural**
parameters — the ones the API needs to build the URL or request body and
that *don't* correspond to columns in the result (e.g. ``asset_id``,
which becomes ``/assets/{id}/vulnerabilities``).

Regular column filters (``hostname``, ``severity``, ``status``, etc.)
belong in the SQL ``WHERE`` clause and are injected into the function via
DuckAPI's automatic push-down.

Supported query examples
-------------------------
::

    # Paginated assets — limit is passed as the API's page_size
    SELECT * FROM assets LIMIT 25

    # hostname filter via WHERE → pushed down to the API
    SELECT * FROM assets WHERE hostname = 'web-prod'

    # Combining WHERE and LIMIT
    SELECT * FROM assets WHERE hostname = 'web-prod' LIMIT 50

    # asset_id is structural (builds the URL) → required inline
    SELECT * FROM asset_vulnerabilities(asset_id=42) LIMIT 100

    # Column filter via WHERE on a structural endpoint
    SELECT * FROM asset_vulnerabilities(asset_id=42)
     WHERE severity = 'critical'

    # Policies: policy_id structural, status via WHERE
    SELECT * FROM policy_rules(policy_id=7) WHERE status = 'failed'
"""

from typing import Any, Dict, Iterator, List, Optional, Union

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class InsightVM:
    """
    Client for the Rapid7 InsightVM / Nexpose v3 API.

    Parameters
    ----------
    host : str
        Hostname or IP of the InsightVM console (no protocol).
    username : str
    password : str
    verify : bool
        TLS certificate verification. Defaults to False for environments
        with self-signed certificates.
    default_page_size : int
        Default page size when ``limit`` isn't passed by DuckAPI.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify: bool = False,
        default_page_size: int = 500,
    ):
        self.base_url = f"https://{host}/api/3"
        self.verify = verify
        self.default_page_size = default_page_size

        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.verify = verify

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "InsightVM":
        """
        Builds an InsightVM client from a credentials dict (e.g. an AWS
        Secrets Manager secret via ``SecretsManager.get_secret``).

        Parameters
        ----------
        secret : dict
            Expected keys: ``host``, ``username``, ``password``.
        **overrides
            Overrides/adds constructor kwargs (e.g. ``verify``,
            ``default_page_size``) — useful when those values aren't in
            the secret but come from the ``auto_register`` config instead.
        """
        host = overrides.pop("host", None) or secret["host"]
        username = overrides.pop("username", None) or secret["username"]
        password = overrides.pop("password", None) or secret["password"]
        return cls(host, username, password, **overrides)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        params = params or {}
        r = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: Dict, params: Optional[Dict] = None) -> Dict:
        params = params or {}
        r = self.session.post(
            f"{self.base_url}{path}",
            params=params,
            json=body,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def _iter_pages(
        self,
        path: str,
        params: Optional[Dict] = None,
    ) -> Iterator[List[Dict]]:
        """
        Generator that yields one page of records at a time.

        Useful for incremental streaming: the first page is returned as
        soon as the first request completes, without waiting for the
        total.
        """
        params = params or {}
        page = 0
        while True:
            payload = self._get(
                path, {**params, "size": self.default_page_size, "page": page}
            )
            resources = payload.get("resources", [])
            if resources:
                yield resources
            total_pages = payload.get("page", {}).get("totalPages", 1)
            page += 1
            if page >= total_pages:
                break

    def _fetch(
        self,
        path: str,
        params: Optional[Dict] = None,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        """
        Fetches records from a paginated endpoint.

        If ``limit`` is provided, makes a single request with
        ``size=limit`` (first page only) — without iterating every page.
        Without ``limit``, paginates fully with ``default_page_size``.
        """
        params = params or {}

        if limit is not None:
            payload = self._get(path, {**params, "size": limit, "page": 0})
            return payload.get("resources", [])

        # Full pagination
        page = 0
        all_resources: List[Dict] = []
        while True:
            payload = self._get(path, {**params, "size": self.default_page_size, "page": page})
            resources = payload.get("resources", [])
            all_resources.extend(resources)
            total_pages = payload.get("page", {}).get("totalPages", 1)
            page += 1
            if page >= total_pages:
                break

        return all_resources

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    def assets(
        self,
        hostname: Optional[str] = None,
        ip: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists assets, with optional push-down of hostname, ip and limit.

        WHERE push-down
        ----------------
        - ``hostname`` → searches for ``host-name contains <value>``
        - ``ip``       → searches for ``ip-address is <value>``

        When hostname or ip are passed, uses the ``/assets/search``
        endpoint, which supports server-side filters.
        Without filters, paginates ``/assets`` normally.

        Parameters
        ----------
        hostname : str, optional
            Hostname fragment for the server-side filter.
        ip : str, optional
            Exact IP address for the server-side filter.
        limit : int, optional
            Maximum number of records (the API's page_size).
        """
        if hostname or ip:
            filters = []
            if hostname:
                filters.append({
                    "field": "host-name",
                    "operator": "contains",
                    "value": hostname,
                })
            if ip:
                filters.append({
                    "field": "ip-address",
                    "operator": "is",
                    "value": ip,
                })

            body = {"filters": filters, "match": "all"}
            payload = self._post(
                "/assets/search",
                body,
                params={"size": limit or self.default_page_size, "page": 0},
            )
            return pd.json_normalize(
                payload.get("resources", []), sep="_"
            )

        resources = self._fetch("/assets", limit=limit)
        return pd.json_normalize(resources, sep="_")

    # ------------------------------------------------------------------
    # Vulnerabilities
    # ------------------------------------------------------------------

    def vulnerabilities(
        self,
        severity: Optional[str] = None,
        cvss_score: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists vulnerabilities from the InsightVM vulnerability database.

        WHERE push-down
        ----------------
        - ``severity``    → filters client-side on the severity field
          (Critical, Severe, Moderate)
        - ``cvss_score``  → filters client-side by cvss >= value

        Parameters
        ----------
        severity : str, optional
            Exact severity level (Critical, Severe, Moderate).
        cvss_score : float, optional
            Minimum CVSS score.
        limit : int, optional
            Maximum number of records returned.
        """
        resources = self._fetch("/vulnerabilities", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if severity and not df.empty and "severity" in df.columns:
            df = df[df["severity"].str.lower() == severity.lower()]

        if cvss_score is not None and not df.empty:
            score_col = next(
                (c for c in df.columns if "cvss" in c.lower() and "score" in c.lower()),
                None,
            )
            if score_col:
                df = df[pd.to_numeric(df[score_col], errors="coerce") >= cvss_score]

        return df

    # ------------------------------------------------------------------
    # Vulnerabilities of a specific asset
    # ------------------------------------------------------------------

    def asset_vulnerabilities(
        self,
        asset_id: int,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Vulnerabilities found on a specific asset.

        Parameters
        ----------
        asset_id : int
            Asset ID (required).
        status : str, optional
            ``vulnerable``, ``vulnerable-version``, ``vulnerable-potential``.
        severity : str, optional
            Critical, Severe, Moderate.
        limit : int, optional
            Maximum number of records.
        """
        resources = self._fetch(f"/assets/{asset_id}/vulnerabilities", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        if severity and not df.empty and "severity" in df.columns:
            df = df[df["severity"].str.lower() == severity.lower()]

        return df

    # ------------------------------------------------------------------
    # Sites
    # ------------------------------------------------------------------

    def sites(
        self,
        name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists scan sites.

        Parameters
        ----------
        name : str, optional
            Site name fragment for the client-side filter.
        limit : int, optional
            Maximum number of records.
        """
        resources = self._fetch("/sites", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "name" in df.columns:
            df = df[df["name"].str.contains(name, case=False, na=False)]

        return df

    # ------------------------------------------------------------------
    # Scan Engines
    # ------------------------------------------------------------------

    def scan_engines(
        self,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Lists registered scan engines."""
        resources = self._fetch("/scan_engines", limit=limit)
        return pd.json_normalize(resources, sep="_")

    # ------------------------------------------------------------------
    # Scans
    # ------------------------------------------------------------------

    def scans(
        self,
        status: Optional[str] = None,
        site_id: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists scan runs.

        Parameters
        ----------
        status : str, optional
            running, finished, stopped, error, paused, aborted, unknown.
        site_id : int, optional
            Filters scans for a specific site.
        limit : int, optional
            Maximum number of records.
        """
        path = f"/sites/{site_id}/scans" if site_id else "/scans"
        resources = self._fetch(path, limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        return df

    # ------------------------------------------------------------------
    # Report Templates
    # ------------------------------------------------------------------

    def report_templates(
        self,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Lists available report templates."""
        payload = self._get("/report_templates")
        resources = payload.get("resources", [payload] if isinstance(payload, dict) else payload)
        df = pd.json_normalize(resources, sep="_")
        if limit:
            df = df.head(limit)
        return df

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def reports(
        self,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Lists generated reports."""
        resources = self._fetch("/reports", limit=limit)
        return pd.json_normalize(resources, sep="_")

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    def tags(
        self,
        name: Optional[str] = None,
        tag_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists asset tags.

        Parameters
        ----------
        name : str, optional
            Tag name fragment for the client-side filter.
        tag_type : str, optional
            owner, location, custom, criticality.
        limit : int, optional
            Maximum number of records.
        """
        resources = self._fetch("/tags", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "name" in df.columns:
            df = df[df["name"].str.contains(name, case=False, na=False)]

        if tag_type and not df.empty and "type" in df.columns:
            df = df[df["type"].str.lower() == tag_type.lower()]

        return df

    # ------------------------------------------------------------------
    # Asset Groups
    # ------------------------------------------------------------------

    def asset_groups(
        self,
        name: Optional[str] = None,
        group_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists asset groups.

        Parameters
        ----------
        name : str, optional
            Group name fragment.
        group_type : str, optional
            static or dynamic.
        limit : int, optional
            Maximum number of records.
        """
        resources = self._fetch("/asset_groups", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "name" in df.columns:
            df = df[df["name"].str.contains(name, case=False, na=False)]

        if group_type and not df.empty and "type" in df.columns:
            df = df[df["type"].str.lower() == group_type.lower()]

        return df

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def users(
        self,
        login: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists console users.

        Parameters
        ----------
        login : str, optional
            Exact login for the client-side filter.
        limit : int, optional
            Maximum number of records.
        """
        resources = self._fetch("/users", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if login and not df.empty and "login" in df.columns:
            df = df[df["login"].str.lower() == login.lower()]

        return df

    # ------------------------------------------------------------------
    # Policies (compliance)
    # ------------------------------------------------------------------

    def policies(
        self,
        name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists available compliance policies.

        Parameters
        ----------
        name : str, optional
            Policy name fragment.
        limit : int, optional
            Maximum number of records.
        """
        resources = self._fetch("/policies", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if name and not df.empty and "title" in df.columns:
            df = df[df["title"].str.contains(name, case=False, na=False)]

        return df

    # ------------------------------------------------------------------
    # Policy Rules
    # ------------------------------------------------------------------

    def policy_rules(
        self,
        policy_id: int,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Rules of a compliance policy.

        Parameters
        ----------
        policy_id : int
            Policy ID (required).
        status : str, optional
            passed, failed, not-applicable.
        limit : int, optional
            Maximum number of records.
        """
        resources = self._fetch(f"/policies/{policy_id}/rules", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        return df

    # ------------------------------------------------------------------
    # Remediation Projects
    # ------------------------------------------------------------------

    def remediation_projects(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists remediation projects.

        Parameters
        ----------
        status : str, optional
            active, expired, paused, completed.
        limit : int, optional
            Maximum number of records.
        """
        resources = self._fetch("/remediation/projects", limit=limit)

        df = pd.json_normalize(resources, sep="_")

        if status and not df.empty and "status" in df.columns:
            df = df[df["status"].str.lower() == status.lower()]

        return df

    # ------------------------------------------------------------------
    # Streaming (iter_*) — for use with DuckAPI.stream()
    # ------------------------------------------------------------------
    #
    # Each iter_* method accepts the same column filters as the regular
    # version, but yields one pd.DataFrame per page as each HTTP request
    # returns — without accumulating everything in memory.
    #
    # Registration:
    #   duck.register_api_function("assets", r7.assets)
    #   duck.register_streaming_function("assets", r7.iter_assets)
    # ------------------------------------------------------------------

    def iter_assets(
        self,
        hostname: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of assets at a time."""
        if hostname or ip:
            # The search endpoint returns everything in a single call; single yield.
            yield self.assets(hostname=hostname, ip=ip)
            return
        for page in self._iter_pages("/assets"):
            yield pd.json_normalize(page, sep="_")

    def iter_vulnerabilities(
        self,
        severity: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of vulnerabilities at a time."""
        for page in self._iter_pages("/vulnerabilities"):
            df = pd.json_normalize(page, sep="_")
            if severity and "severity" in df.columns:
                df = df[df["severity"].str.lower() == severity.lower()]
            if not df.empty:
                yield df

    def iter_asset_vulnerabilities(
        self,
        asset_id: int,
        status: Optional[str] = None,
        severity: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of the asset's vulnerabilities at a time."""
        for page in self._iter_pages(f"/assets/{asset_id}/vulnerabilities"):
            df = pd.json_normalize(page, sep="_")
            if status and "status" in df.columns:
                df = df[df["status"].str.lower() == status.lower()]
            if severity and "severity" in df.columns:
                df = df[df["severity"].str.lower() == severity.lower()]
            if not df.empty:
                yield df

    def iter_sites(
        self,
        name: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of sites at a time."""
        for page in self._iter_pages("/sites"):
            df = pd.json_normalize(page, sep="_")
            if name and "name" in df.columns:
                df = df[df["name"].str.contains(name, case=False, na=False)]
            if not df.empty:
                yield df

    def iter_scans(
        self,
        status: Optional[str] = None,
        site_id: Optional[int] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of scans at a time."""
        path = f"/sites/{site_id}/scans" if site_id else "/scans"
        for page in self._iter_pages(path):
            df = pd.json_normalize(page, sep="_")
            if status and "status" in df.columns:
                df = df[df["status"].str.lower() == status.lower()]
            if not df.empty:
                yield df

    def iter_policy_rules(
        self,
        policy_id: int,
        status: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of the policy's rules at a time."""
        for page in self._iter_pages(f"/policies/{policy_id}/rules"):
            df = pd.json_normalize(page, sep="_")
            if status and "status" in df.columns:
                df = df[df["status"].str.lower() == status.lower()]
            if not df.empty:
                yield df

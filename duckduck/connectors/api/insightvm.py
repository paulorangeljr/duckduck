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

from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

import pandas as pd
import requests
import urllib3

from ...common import slicing
from ...common.logs import PageProgress, instrument_session
from ...common.retry import RetryPolicy, send
from ...common.pushdown import require_like, sortable
from ...common.sparkplan import spark_plan

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


#: assets columns the API's ``sort`` orders by (``property,ASC|DESC``) — numbers only (``riskScore`` is the
#: documented example; text would follow the console database's collation). Other tables: not sent — which
#: of their properties the API sorts by isn't documented, and an unknown one is a 400.
_ASSET_SORTS = {"riskscore": "riskScore", "id": "id"}


def _sort_param(order_by: Optional[List[Tuple[str, bool]]]) -> Dict[str, str]:
    """DuckAPI's ``order_by`` as InsightVM's ``sort`` query parameter (one column)."""
    if not order_by:
        return {}
    column, descending = order_by[0]
    return {"sort": f"{_ASSET_SORTS.get(column.lower(), column)},{'DESC' if descending else 'ASC'}"}


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
        instrument_session(self.session, "insightvm")
        self.retry = RetryPolicy()  # the service's "retry" in duckduck.json replaces it
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
        r = send(self.retry, "insightvm", lambda timeout: self.session.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=timeout,
        ), 60, what=path)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: Dict, params: Optional[Dict] = None) -> Dict:
        params = params or {}
        r = send(self.retry, "insightvm", lambda timeout: self.session.post(
            f"{self.base_url}{path}",
            params=params,
            json=body,
            timeout=timeout,
        ), 60, what=path)
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
        yield from slicing.pages(self._page_reader(path, params or {}), self.default_page_size)

    def _page_reader(self, path: str, params: Dict):
        """``fetch_page(n)`` for ``duckduck.common.slicing.pages``: page ``n`` and the total the API reports."""
        progress = PageProgress("insightvm", path)

        def fetch_page(page: int):
            payload = self._get(path, {**params, "size": self.default_page_size, "page": page})
            resources = payload.get("resources", [])
            page_info = payload.get("page", {})
            progress.page(len(resources), total_pages=page_info.get("totalPages", 1),
                          total_rows=page_info.get("totalResources"))
            total = page_info.get("totalResources")
            if total is None and "totalPages" in page_info:
                total = int(page_info["totalPages"]) * self.default_page_size
            return resources, (int(total) if total is not None else None)

        return fetch_page

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

        # Full pagination (or the pages a Spark read asked for: ``duckduck.common.slicing``)
        all_resources: List[Dict] = []
        for resources in slicing.pages(self._page_reader(path, params), self.default_page_size):
            all_resources.extend(resources)
        return all_resources

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    #: SQL LIKE pattern kind → InsightVM asset-search operator.
    _SEARCH_OPERATORS = {"contains": "contains", "startswith": "starts-with", "endswith": "ends-with", "equals": "is"}

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
    @sortable(*_ASSET_SORTS)
    def assets(
        self,
        hostname: Optional[str] = None,
        ip: Optional[str] = None,
        hostname_ilike: Optional[str] = None,
        order_by: Optional[List[Tuple[str, bool]]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists assets, with optional push-down of hostname, ip and limit.

        WHERE push-down
        ----------------
        - ``hostname`` → searches for ``host-name contains <value>``
        - ``ip``       → searches for ``ip-address is <value>``
        - ``hostname LIKE 'p'`` (``hostname_ilike``) → ``host-name``
          ``contains`` / ``starts-with`` / ``ends-with`` / ``is``, by the
          pattern's shape (see ``duckduck.common.pushdown``)

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
        if hostname or ip or hostname_ilike:
            filters = []
            if hostname:
                filters.append({
                    "field": "host-name",
                    "operator": "contains",
                    "value": hostname,
                })
            if hostname_ilike:
                pattern = require_like(hostname_ilike, "hostname_ilike")
                filters.append({
                    "field": "host-name",
                    "operator": self._SEARCH_OPERATORS[pattern.kind],
                    "value": pattern.text,
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
                params={"size": limit or self.default_page_size, "page": 0, **_sort_param(order_by)},
            )
            return pd.json_normalize(
                payload.get("resources", []), sep="_"
            )

        resources = self._fetch("/assets", params=_sort_param(order_by), limit=limit)
        return pd.json_normalize(resources, sep="_")

    # ------------------------------------------------------------------
    # Vulnerabilities
    # ------------------------------------------------------------------

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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
        return _vulnerabilities_kept(pd.json_normalize(self._fetch("/vulnerabilities", limit=limit), sep="_"),
                                     severity, cvss_score)

    # ------------------------------------------------------------------
    # Vulnerabilities of a specific asset
    # ------------------------------------------------------------------

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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

    @spark_plan("driver", why="one request, no pages")
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

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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
        return _keep(pd.json_normalize(self._fetch("/tags", limit=limit), sep="_"),
                     contains={"name": name}, equals={"type": tag_type})

    # ------------------------------------------------------------------
    # Asset Groups
    # ------------------------------------------------------------------

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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
        return _keep(pd.json_normalize(self._fetch("/asset_groups", limit=limit), sep="_"),
                     contains={"name": name}, equals={"type": group_type})

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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
        return _keep(pd.json_normalize(self._fetch("/users", limit=limit), sep="_"), equals={"login": login})

    # ------------------------------------------------------------------
    # Policies (compliance)
    # ------------------------------------------------------------------

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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
        return _keep(pd.json_normalize(self._fetch("/policies", limit=limit), sep="_"), contains={"title": name})

    # ------------------------------------------------------------------
    # Policy Rules
    # ------------------------------------------------------------------

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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

    @spark_plan("partitioned", by="pages", max_parallel=4, why="page=N with page.totalResources: any page can be read on its own")
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
        return _keep(pd.json_normalize(self._fetch("/remediation/projects", limit=limit), sep="_"),
                     equals={"status": status})

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

    def _pages_kept(self, path: str, contains=None, equals=None) -> Iterator[pd.DataFrame]:
        """One DataFrame per page of ``path``, with the same client-side filters as the table method."""
        for page in self._iter_pages(path):
            df = _keep(pd.json_normalize(page, sep="_"), contains=contains, equals=equals)
            if not df.empty:
                yield df

    def iter_scan_engines(self) -> Iterator[pd.DataFrame]:
        """Yields one page of scan engines at a time."""
        return self._pages_kept("/scan_engines")

    def iter_reports(self) -> Iterator[pd.DataFrame]:
        """Yields one page of reports at a time."""
        return self._pages_kept("/reports")

    def iter_tags(self, name: Optional[str] = None, tag_type: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """Yields one page of tags at a time."""
        return self._pages_kept("/tags", contains={"name": name}, equals={"type": tag_type})

    def iter_asset_groups(self, name: Optional[str] = None, group_type: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """Yields one page of asset groups at a time."""
        return self._pages_kept("/asset_groups", contains={"name": name}, equals={"type": group_type})

    def iter_users(self, login: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """Yields one page of console users at a time."""
        return self._pages_kept("/users", equals={"login": login})

    def iter_policies(self, name: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """Yields one page of compliance policies at a time."""
        return self._pages_kept("/policies", contains={"title": name})

    def iter_remediation_projects(self, status: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """Yields one page of remediation projects at a time."""
        return self._pages_kept("/remediation/projects", equals={"status": status})

    @sortable(*_ASSET_SORTS)
    def iter_assets(
        self,
        hostname: Optional[str] = None,
        ip: Optional[str] = None,
        hostname_ilike: Optional[str] = None,
        order_by: Optional[List[Tuple[str, bool]]] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of assets at a time (``order_by``: in the API's ``sort`` order)."""
        if hostname or ip or hostname_ilike:
            # The search endpoint returns everything in a single call; single yield.
            yield self.assets(hostname=hostname, ip=ip, hostname_ilike=hostname_ilike, order_by=order_by)
            return
        for page in self._iter_pages("/assets", _sort_param(order_by)):
            yield pd.json_normalize(page, sep="_")

    def iter_vulnerabilities(
        self,
        severity: Optional[str] = None,
        cvss_score: Optional[float] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of vulnerabilities at a time (the filters of ``vulnerabilities``)."""
        for page in self._iter_pages("/vulnerabilities"):
            df = _vulnerabilities_kept(pd.json_normalize(page, sep="_"), severity, cvss_score)
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


def _keep(df: pd.DataFrame, contains: Optional[Dict[str, Optional[str]]] = None,
          equals: Optional[Dict[str, Optional[str]]] = None) -> pd.DataFrame:
    """The client-side filters the small tables share: ``column`` contains / equals (case-insensitive) a value;
    an unset value or a column the page doesn't have filters nothing."""
    for column, text in (contains or {}).items():
        if text and not df.empty and column in df.columns:
            df = df[df[column].str.contains(text, case=False, na=False, regex=False)]
    for column, text in (equals or {}).items():
        if text and not df.empty and column in df.columns:
            df = df[df[column].str.lower() == text.lower()]
    return df


def _vulnerabilities_kept(df: pd.DataFrame, severity: Optional[str], cvss_score: Optional[float]) -> pd.DataFrame:
    """``vulnerabilities``' client-side filters: the exact severity, a CVSS score of at least ``cvss_score``."""
    if severity and not df.empty and "severity" in df.columns:
        df = df[df["severity"].str.lower() == severity.lower()]
    if cvss_score is not None and not df.empty:
        score_col = next((c for c in df.columns if "cvss" in c.lower() and "score" in c.lower()), None)
        if score_col:
            df = df[pd.to_numeric(df[score_col], errors="coerce") >= cvss_score]
    return df

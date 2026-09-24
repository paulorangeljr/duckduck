"""
ServiceNow Table API wrapper for use with DuckAPI.

Authenticates via HTTP Basic auth and queries any ServiceNow table
through the generic Table API (``/api/now/table/{tableName}``), plus
dedicated methods with real server-side push-down for the tables most
commonly queried (incidents, problems, changes, users, CIs).

Usage convention
-----------------
Same convention as the rest of ``duckduck``: ``FROM func(param=val)`` is
for **structural** parameters (here, only ``table_name`` on the generic
``table()`` method — everything else has a fixed table). Column filters
(``state``, ``priority``, ``assigned_to``, etc.) belong in ``WHERE`` and
are pushed down as ``sysparm_query`` — ServiceNow's own encoded query
syntax (``field=value^field2=value2`` for AND, ``^OR`` for OR).

Supported query examples
-------------------------
::

    # Any table, via the generic method — table_name is structural
    SELECT * FROM table(table_name='incident') WHERE priority = '1' LIMIT 50

    # Dedicated methods with real push-down
    SELECT * FROM incidents WHERE state = '2' AND priority = '1'
    SELECT * FROM problems WHERE state = 'open'
    SELECT * FROM change_requests WHERE type = 'normal'
    SELECT * FROM users WHERE active = 'true'
    SELECT * FROM cmdb_ci WHERE sys_class_name = 'cmdb_ci_server'

Notes on the Table API
-----------------------
- Pagination uses ``sysparm_limit``/``sysparm_offset`` — no
  ``page``/``totalPages`` like InsightVM, no ``@odata.nextLink`` like
  Graph. ``_iter_pages`` increments ``sysparm_offset`` until a page comes
  back smaller than the page size.
- ``sysparm_query`` is ServiceNow's own encoded query language, not SQL:
  ``^`` joins conditions with AND, ``^OR`` with OR, ``^NQ`` groups
  conditions with OR between groups. Dedicated methods below build a
  simple ``^``-joined equality query from their kwargs; pass a raw string
  via ``query=`` on ``table()`` for anything more advanced (``!=``,
  ``STARTSWITH``, ``CONTAINS``, date ranges, etc.).
"""

from typing import Any, Dict, Iterator, List, Optional

import pandas as pd
import requests


class ServiceNow:
    """
    Client for the ServiceNow Table API.

    Parameters
    ----------
    instance : str, optional
        The instance name, e.g. ``"dev12345"`` for
        ``dev12345.service-now.com`` (don't include the domain). Provide
        **either** ``instance`` **or** ``host``.
    username : str
    password : str
    default_page_size : int
        Page size used when ``limit`` isn't passed by DuckAPI.
    verify : bool
        TLS certificate verification. Defaults to True — ServiceNow
        instances always have valid certificates.
    host : str, optional
        Full hostname to use as-is instead of ``{instance}.service-now.com``
        — for a custom domain / on-prem deployment that isn't on the
        standard ServiceNow cloud domain. E.g. ``"servicenow.mycompany.com"``.
    """

    def __init__(
        self,
        instance: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        default_page_size: int = 200,
        verify: bool = True,
        host: Optional[str] = None,
    ):
        if not username or not password:
            raise ValueError("ServiceNow requires 'username' and 'password'.")
        if not instance and not host:
            raise ValueError("Provide either 'instance' or 'host'.")

        resolved_host = host or f"{instance}.service-now.com"
        self.base_url = f"https://{resolved_host}/api/now"
        self.default_page_size = default_page_size

        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.verify = verify
        self.session.headers.update({"Accept": "application/json"})

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "ServiceNow":
        """
        Builds ServiceNow from a credentials dict (e.g. a secret fetched
        by ``auto_register()``).

        Parameters
        ----------
        secret : dict
            Expected keys: ``username``, ``password``, plus **either**
            ``instance`` **or** ``host`` (see ``__init__``).
        """
        instance = overrides.pop("instance", None) or secret.get("instance")
        host = overrides.pop("host", None) or secret.get("host")
        username = overrides.pop("username", None) or secret["username"]
        password = overrides.pop("password", None) or secret["password"]
        return cls(instance=instance, username=username, password=password, host=host, **overrides)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, table_name: str, params: Dict[str, Any]) -> Dict:
        r = self.session.get(
            f"{self.base_url}/table/{table_name}",
            params=params,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _build_query(**filters: Any) -> Optional[str]:
        """Joins non-None kwargs into a ``field=value^field2=value2`` encoded query."""
        parts = [f"{key}={value}" for key, value in filters.items() if value is not None]
        return "^".join(parts) if parts else None

    def _iter_pages(
        self,
        table_name: str,
        query: Optional[str] = None,
        fields: Optional[List[str]] = None,
        display_value: bool = False,
    ) -> Iterator[List[Dict]]:
        """
        Generator that pages through a table via sysparm_limit/sysparm_offset,
        yielding one page of records at a time.
        """
        offset = 0
        while True:
            params: Dict[str, Any] = {
                "sysparm_limit": self.default_page_size,
                "sysparm_offset": offset,
            }
            if query:
                params["sysparm_query"] = query
            if fields:
                params["sysparm_fields"] = ",".join(fields)
            if display_value:
                params["sysparm_display_value"] = "true"

            payload = self._get(table_name, params)
            results = payload.get("result", [])
            if results:
                yield results
            if len(results) < self.default_page_size:
                break
            offset += self.default_page_size

    def _fetch(
        self,
        table_name: str,
        query: Optional[str] = None,
        fields: Optional[List[str]] = None,
        display_value: bool = False,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        """
        A single request if ``limit`` is set; full sysparm_offset
        pagination otherwise.
        """
        if limit is not None:
            params: Dict[str, Any] = {"sysparm_limit": limit, "sysparm_offset": 0}
            if query:
                params["sysparm_query"] = query
            if fields:
                params["sysparm_fields"] = ",".join(fields)
            if display_value:
                params["sysparm_display_value"] = "true"
            payload = self._get(table_name, params)
            return payload.get("result", [])

        all_results: List[Dict] = []
        for page in self._iter_pages(table_name, query=query, fields=fields, display_value=display_value):
            all_results.extend(page)
        return all_results

    # ------------------------------------------------------------------
    # Generic table access — any table, no dedicated method needed
    # ------------------------------------------------------------------

    def table(
        self,
        table_name: str,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Reads records from any ServiceNow table.

        Parameters
        ----------
        table_name : str
            Structural — the table to query (e.g. ``"incident"``,
            ``"sys_user"``, ``"cmdb_ci_server"``, or any custom table).
            Inline only: ``table(table_name='incident')``.
        query : str, optional
            Raw ``sysparm_query`` encoded string, for anything the
            dedicated methods' simple equality push-down can't express
            (``!=``, ``STARTSWITH``, ``CONTAINS``, date ranges, ``^OR``, etc.).
        limit : int, optional
            Maximum number of records.
        """
        results = self._fetch(table_name, query=query, limit=limit)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # Incidents
    # ------------------------------------------------------------------

    def incidents(
        self,
        number: Optional[str] = None,
        state: Optional[str] = None,
        priority: Optional[str] = None,
        assigned_to: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists incidents (``incident`` table), with push-down of common filters.

        Parameters
        ----------
        number : str, optional
            Exact incident number (e.g. ``"INC0010001"``).
        state : str, optional
            Numeric state value (e.g. ``"1"`` New, ``"2"`` In Progress,
            ``"6"`` Resolved, ``"7"`` Closed — varies by instance config).
        priority : str, optional
            Numeric priority (``"1"`` Critical .. ``"5"`` Planning).
        assigned_to : str, optional
            sys_id of the assigned user.
        limit : int, optional
            Maximum number of records.
        """
        query = self._build_query(
            number=number, state=state, priority=priority, assigned_to=assigned_to
        )
        results = self._fetch("incident", query=query, limit=limit)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # Problems
    # ------------------------------------------------------------------

    def problems(
        self,
        number: Optional[str] = None,
        state: Optional[str] = None,
        priority: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists problems (``problem`` table), with push-down of common filters.
        """
        query = self._build_query(number=number, state=state, priority=priority)
        results = self._fetch("problem", query=query, limit=limit)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # Change Requests
    # ------------------------------------------------------------------

    def change_requests(
        self,
        number: Optional[str] = None,
        state: Optional[str] = None,
        type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists change requests (``change_request`` table).

        Parameters
        ----------
        type : str, optional
            ``"standard"``, ``"normal"``, or ``"emergency"``.
        """
        query = self._build_query(number=number, state=state, type=type)
        results = self._fetch("change_request", query=query, limit=limit)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def users(
        self,
        user_name: Optional[str] = None,
        active: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists users (``sys_user`` table).

        Parameters
        ----------
        user_name : str, optional
            Exact login/user_name.
        active : str, optional
            ``"true"`` or ``"false"``.
        """
        query = self._build_query(user_name=user_name, active=active)
        results = self._fetch("sys_user", query=query, limit=limit)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # CMDB Configuration Items
    # ------------------------------------------------------------------

    def cmdb_ci(
        self,
        name: Optional[str] = None,
        sys_class_name: Optional[str] = None,
        operational_status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists configuration items (``cmdb_ci`` table — the base CMDB table;
        pass a more specific class via ``table(table_name='cmdb_ci_server')``
        etc. if you need one CI class server-side).
        """
        query = self._build_query(
            name=name, sys_class_name=sys_class_name, operational_status=operational_status
        )
        results = self._fetch("cmdb_ci", query=query, limit=limit)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # Streaming (iter_*) — for use with DuckAPI.stream()
    # ------------------------------------------------------------------

    def iter_table(self, table_name: str, query: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """Yields one page of records at a time from any table."""
        for page in self._iter_pages(table_name, query=query):
            yield pd.json_normalize(page, sep="_")

    def iter_incidents(
        self,
        state: Optional[str] = None,
        priority: Optional[str] = None,
        assigned_to: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of incidents at a time."""
        query = self._build_query(state=state, priority=priority, assigned_to=assigned_to)
        for page in self._iter_pages("incident", query=query):
            yield pd.json_normalize(page, sep="_")

    def iter_problems(self, state: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """Yields one page of problems at a time."""
        query = self._build_query(state=state)
        for page in self._iter_pages("problem", query=query):
            yield pd.json_normalize(page, sep="_")

    def iter_change_requests(
        self, state: Optional[str] = None, type: Optional[str] = None
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of change requests at a time."""
        query = self._build_query(state=state, type=type)
        for page in self._iter_pages("change_request", query=query):
            yield pd.json_normalize(page, sep="_")

    def iter_users(self, active: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """Yields one page of users at a time."""
        query = self._build_query(active=active)
        for page in self._iter_pages("sys_user", query=query):
            yield pd.json_normalize(page, sep="_")

    def iter_cmdb_ci(self, sys_class_name: Optional[str] = None) -> Iterator[pd.DataFrame]:
        """Yields one page of configuration items at a time."""
        query = self._build_query(sys_class_name=sys_class_name)
        for page in self._iter_pages("cmdb_ci", query=query):
            yield pd.json_normalize(page, sep="_")

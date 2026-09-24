"""
ServiceNow Table API wrapper for use with DuckAPI.

Authenticates via HTTP Basic auth (``ServiceNow(...)``) or OAuth2
client-credentials (``ServiceNow.from_oauth2(...)``) and queries any
ServiceNow table through the Table API, plus dedicated methods with real
server-side push-down for the tables most commonly queried (incidents,
problems, changes, users, CIs).

Many enterprise ServiceNow deployments front the API with an external
identity provider (e.g. Azure AD issuing the OAuth2 token) and/or an API
gateway that isn't ``{instance}.service-now.com`` at all — ``from_oauth2``
takes the token URL and the data API's base URL as separate, fully
independent parameters to cover that, rather than assuming ServiceNow's
own ``/oauth_token.do`` and ``/api/now`` path.

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

import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd
import requests

from .logs import PageProgress, get_logger, instrument_session, log_http
from .pushdown import Condition, parse_like, require_like

logger = get_logger("servicenow")


class ServiceNow:
    """
    Client for the ServiceNow Table API.

    This constructor is HTTP Basic auth. For OAuth2 client-credentials
    (Azure AD, ServiceNow's own OAuth2 endpoint, or a gateway in front of
    either), use ``ServiceNow.from_oauth2(...)`` instead.

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
        self._setup(f"https://{resolved_host}/api/now", default_page_size, verify)
        self.session.auth = (username, password)
        self._auth_mode = "basic"

    @classmethod
    def from_oauth2(
        cls,
        token_url: str,
        client_id: str,
        client_secret: str,
        instance: Optional[str] = None,
        host: Optional[str] = None,
        api_base: Optional[str] = None,
        resource: Optional[str] = None,
        scope: Optional[str] = None,
        default_page_size: int = 200,
        verify: bool = True,
    ) -> "ServiceNow":
        """
        Authentication via OAuth2 client-credentials — for deployments
        where an external identity provider issues the token (e.g. Azure
        AD in front of ServiceNow or an API gateway), or ServiceNow's own
        OAuth2 endpoint.

        ``token_url`` and the data API's base URL are independent: the
        token issuer is often a completely different host (and sometimes
        a different API path convention, e.g. ``/v1/now/table/...``
        behind a gateway rather than ``/api/now/table/...`` straight
        against ServiceNow) than where records are actually read from.

        Parameters
        ----------
        token_url : str
            Full URL of the OAuth2 token endpoint (client-credentials
            grant), e.g. ``https://login.microsoftonline.com/{tenant}/oauth2/token``
            or ``https://{instance}.service-now.com/oauth_token.do``.
        client_id, client_secret : str
        instance / host : str, optional
            Same as ``__init__`` — used to build the data API's base URL
            when ``api_base`` isn't given directly. Provide one of
            ``instance``, ``host``, or ``api_base``.
        api_base : str, optional
            Full base URL for the Table API, used as-is (no ``/api/now``
            appended) — for a gateway/proxy in front of ServiceNow that
            uses its own path convention. E.g.
            ``"https://internal-gateway.mycompany.com/v1/now"``.
        resource : str, optional
            Sent as the ``resource`` form field — Azure AD's audience
            parameter for the client-credentials grant.
        scope : str, optional
            Sent as the ``scope`` form field, for identity providers that
            use OAuth2 scopes instead of (or alongside) ``resource``.
        """
        obj = cls.__new__(cls)
        if api_base:
            base_url = api_base.rstrip("/")
        else:
            if not instance and not host:
                raise ValueError("Provide 'instance', 'host', or 'api_base'.")
            base_url = f"https://{host or f'{instance}.service-now.com'}/api/now"
        obj._setup(base_url, default_page_size, verify)
        obj._auth_mode = "oauth2"
        obj._token_url = token_url
        obj._client_id = client_id
        obj._client_secret = client_secret
        obj._resource = resource
        obj._scope = scope
        obj._token_expires_at = None
        return obj

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides) -> "ServiceNow":
        """
        Builds ServiceNow from a credentials dict (e.g. a secret fetched
        by ``auto_register()``).

        Detects the authentication mode from the keys present:

        - ``client_id`` + ``client_secret`` + ``token_url`` → ``from_oauth2``
        - ``username`` + ``password`` (+ ``instance``/``host``) → Basic auth

        Parameters
        ----------
        secret : dict
            OAuth2: ``token_url``, ``client_id``, ``client_secret``, plus
            optional ``instance``/``host``/``api_base``/``resource``/``scope``.
            Basic auth: ``username``, ``password``, plus **either**
            ``instance`` **or** ``host``.
        """
        if "client_id" in secret and "client_secret" in secret and (
            "token_url" in secret or "token_url" in overrides
        ):
            return cls.from_oauth2(
                token_url=overrides.pop("token_url", None) or secret["token_url"],
                client_id=overrides.pop("client_id", None) or secret["client_id"],
                client_secret=overrides.pop("client_secret", None) or secret["client_secret"],
                instance=overrides.pop("instance", None) or secret.get("instance"),
                host=overrides.pop("host", None) or secret.get("host"),
                api_base=overrides.pop("api_base", None) or secret.get("api_base"),
                resource=overrides.pop("resource", None) or secret.get("resource"),
                scope=overrides.pop("scope", None) or secret.get("scope"),
                **overrides,
            )

        instance = overrides.pop("instance", None) or secret.get("instance")
        host = overrides.pop("host", None) or secret.get("host")
        username = overrides.pop("username", None) or secret["username"]
        password = overrides.pop("password", None) or secret["password"]
        return cls(instance=instance, username=username, password=password, host=host, **overrides)

    # ------------------------------------------------------------------
    # Internal setup
    # ------------------------------------------------------------------

    def _setup(self, base_url: str, default_page_size: int, verify: bool) -> None:
        self.base_url = base_url
        self.default_page_size = default_page_size
        self.session = requests.Session()
        instrument_session(self.session, "servicenow")
        self._last_total: Optional[int] = None
        self.session.verify = verify
        self.session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------
    # OAuth2 token handling
    # ------------------------------------------------------------------

    def _ensure_token(self) -> None:
        """Acquires (or refreshes, with a 60s buffer before expiry) the OAuth2 token."""
        if self._token_expires_at is not None and datetime.now() < self._token_expires_at:
            return

        body = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._resource:
            body["resource"] = self._resource
        if self._scope:
            body["scope"] = self._scope

        r = requests.post(
            self._token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        log_http("servicenow", r, log_body=False)  # the body carries client_secret
        if not r.ok:
            # r.raise_for_status() drops the response body — but that's exactly
            # where Azure AD (and most OAuth2 providers) put the actual reason
            # ("error"/"error_description", e.g. AADSTS7000215 for a bad
            # client_secret or AADSTS900023 for a bad tenant), so surface it.
            raise ValueError(
                f"ServiceNow OAuth2 token request failed ({r.status_code}): {r.text}"
            )
        payload = r.json()

        self.session.headers["Authorization"] = f"Bearer {payload['access_token']}"
        expires_in = int(payload.get("expires_in", 3600))
        self._token_expires_at = datetime.now() + timedelta(seconds=max(expires_in - 60, 0))

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, table_name: str, params: Dict[str, Any]) -> Dict:
        if self._auth_mode == "oauth2":
            self._ensure_token()
        r = self.session.get(
            f"{self.base_url}/table/{table_name}",
            params=params,
            timeout=60,
        )
        r.raise_for_status()
        # ServiceNow reports the query's total row count in a header.
        total = r.headers.get("X-Total-Count")
        self._last_total = int(total) if isinstance(total, str) and total.isdigit() else None
        return r.json()

    #: SQL LIKE pattern kind (``duckduck.pushdown.parse_like``) → encoded-query operator.
    _LIKE_OPERATORS = {"contains": "LIKE", "startswith": "STARTSWITH", "endswith": "ENDSWITH", "equals": "="}

    @staticmethod
    def _check_value(field: str, value: Any) -> str:
        text = str(value)
        # "^" separates encoded-query clauses: a value containing it would
        # silently turn into extra conditions (e.g. "x^ORactive=false").
        if "^" in text or "\n" in text:
            raise ValueError(f"{field}: value {text!r} can't contain '^' or newlines in a ServiceNow encoded query.")
        return text

    @classmethod
    def _build_query(cls, **filters: Any) -> Optional[str]:
        """
        Builds a ``^``-joined encoded query from non-None kwargs: ``field=value``
        for equality, and for ``<field>_ilike`` kwargs (a SQL LIKE pattern,
        see ``duckduck.pushdown``) ``fieldLIKEx`` / ``fieldSTARTSWITHx`` /
        ``fieldENDSWITHx`` — ServiceNow's text operators, case-insensitive
        on standard instances.
        """
        parts = []
        for key, value in filters.items():
            if value is None:
                continue
            if key.endswith("_ilike"):
                field = key[: -len("_ilike")]
                pattern = require_like(value, key)
                text = cls._check_value(key, pattern.text)
                parts.append(f"{field}{cls._LIKE_OPERATORS[pattern.kind]}{text}")
            else:
                parts.append(f"{key}={cls._check_value(key, value)}")
        return "^".join(parts) if parts else None

    #: SQL comparison → encoded-query operator.
    _COMPARISON_OPERATORS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

    @classmethod
    def _condition_clause(cls, cond: Condition) -> Tuple[Optional[str], str]:
        """
        One DuckAPI push-down condition as an encoded-query clause, or
        ``(None, reason)`` when it has to stay with DuckDB.
        """
        field = cond.column
        if not re.fullmatch(r"[a-z][a-z0-9_]*", field):
            return None, "not a ServiceNow field name"
        if field.endswith(("_link", "_value")):
            # json_normalize flattens reference fields ({link, value}) into
            # <field>_link / <field>_value — neither is a real field name.
            return None, "reference sub-column (<field>_link/_value)"
        value = cond.value
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        if "^" in text or "\n" in text:
            return None, "value contains '^' (the encoded-query separator)"
        if cond.op in ("like", "ilike"):
            pattern = parse_like(text)
            if pattern is None:
                return None, "LIKE pattern not translatable ('_' wildcard / inner '%')"
            # ServiceNow text operators are case-insensitive: exact for ILIKE,
            # a superset for LIKE — DuckDB re-applies the real predicate.
            return f"{field}{cls._LIKE_OPERATORS[pattern.kind]}{pattern.text}", ""
        if cond.op == "eq":
            return f"{field}={text}", ""
        if cond.op in cls._COMPARISON_OPERATORS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                # e.g. dates as text: ServiceNow reads them in the session
                # user's timezone, so the comparison could drop valid rows.
                return None, "non-numeric comparison"
            return f"{field}{cls._COMPARISON_OPERATORS[cond.op]}{text}", ""
        return None, f"operator {cond.op!r}"

    def pushdown_blocker(self, cond: Condition) -> Optional[str]:
        """
        DuckAPI push-down hook (``duckduck.pushdown.BLOCKER_HOOK``): None if
        ``cond`` can go into ``sysparm_query``, else why not — so DuckAPI
        keeps it (and the LIMIT decision) on its side and reports it.
        """
        return self._condition_clause(cond)[1] or None

    def _with_where(
        self, table_name: str, query: Optional[str], where: Optional[List[Condition]]
    ) -> Tuple[Optional[str], bool]:
        """
        ``query`` with the push-down conditions ANDed in, and whether all of
        them made it (False → some stay with DuckDB, so a limit is unsafe).
        """
        if not where:
            return query, True
        if query and "^NQ" in query:
            # ^NQ starts a new OR'd query group: appending ^cond would only
            # constrain the last group, so leave everything to DuckDB.
            logger.info("%s: raw query has ^NQ — WHERE conditions left to DuckDB", table_name)
            return query, False
        clauses, complete = [], True
        for cond in where:
            clause, reason = self._condition_clause(cond)
            if clause is None:
                complete = False
                logger.info("%s: %s %s %r not sent to ServiceNow — %s", table_name, cond.column, cond.op, cond.value, reason)
            else:
                clauses.append(clause)
        combined = "^".join(([query] if query else []) + clauses) or None
        return combined, complete

    def _iter_pages(
        self,
        table_name: str,
        query: Optional[str] = None,
        fields: Optional[List[str]] = None,
        display_value: bool = False,
        where: Optional[List[Condition]] = None,
    ) -> Iterator[List[Dict]]:
        """
        Generator that pages through a table via sysparm_limit/sysparm_offset,
        yielding one page of records at a time.
        """
        query, _ = self._with_where(table_name, query, where)
        offset = 0
        progress = PageProgress("servicenow", table_name)
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
            total = self._last_total
            pages = -(-total // self.default_page_size) if total else None
            progress.page(len(results), total_pages=pages, total_rows=total)
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
        where: Optional[List[Condition]] = None,
    ) -> List[Dict]:
        """
        A single request if ``limit`` is set; full sysparm_offset
        pagination otherwise. ``where`` (DuckAPI push-down) is folded into
        the encoded query; if any condition can't be expressed there,
        ``limit`` is dropped — DuckDB then filters and limits instead, since
        N rows fetched *before* that filter could hold fewer than N matches.
        """
        query, complete = self._with_where(table_name, query, where)
        if not complete and limit is not None:
            logger.info("%s: LIMIT %s not sent — DuckDB still has conditions to apply", table_name, limit)
            limit = None
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
        where: Optional[List[Condition]] = None,
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
        results = self._fetch(table_name, query=query, limit=limit, where=where)
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
        number_ilike: Optional[str] = None,
        short_description_ilike: Optional[str] = None,
        where: Optional[List[Condition]] = None,
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
        number_ilike : str, optional
            SQL LIKE pattern pushed down as ServiceNow's LIKE / STARTSWITH /
            ENDSWITH (``WHERE number LIKE '%x%'`` arrives here automatically).
        short_description_ilike : str, optional
            SQL LIKE pattern pushed down as ServiceNow's LIKE / STARTSWITH /
            ENDSWITH (``WHERE short_description LIKE '%x%'`` arrives here automatically).
        limit : int, optional
            Maximum number of records.
        """
        query = self._build_query(
            number=number, state=state, priority=priority, assigned_to=assigned_to,
            number_ilike=number_ilike, short_description_ilike=short_description_ilike,
        )
        results = self._fetch("incident", query=query, limit=limit, where=where)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # Problems
    # ------------------------------------------------------------------

    def problems(
        self,
        number: Optional[str] = None,
        state: Optional[str] = None,
        priority: Optional[str] = None,
        number_ilike: Optional[str] = None,
        short_description_ilike: Optional[str] = None,
        where: Optional[List[Condition]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists problems (``problem`` table), with push-down of common filters
        (``*_ilike``: LIKE patterns, see ``incidents``).
        """
        query = self._build_query(
            number=number, state=state, priority=priority,
            number_ilike=number_ilike, short_description_ilike=short_description_ilike,
        )
        results = self._fetch("problem", query=query, limit=limit, where=where)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # Change Requests
    # ------------------------------------------------------------------

    def change_requests(
        self,
        number: Optional[str] = None,
        state: Optional[str] = None,
        type: Optional[str] = None,
        number_ilike: Optional[str] = None,
        short_description_ilike: Optional[str] = None,
        where: Optional[List[Condition]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists change requests (``change_request`` table) (``*_ilike``: LIKE
        patterns, see ``incidents``).

        Parameters
        ----------
        type : str, optional
            ``"standard"``, ``"normal"``, or ``"emergency"``.
        """
        query = self._build_query(
            number=number, state=state, type=type,
            number_ilike=number_ilike, short_description_ilike=short_description_ilike,
        )
        results = self._fetch("change_request", query=query, limit=limit, where=where)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def users(
        self,
        user_name: Optional[str] = None,
        active: Optional[str] = None,
        user_name_ilike: Optional[str] = None,
        name_ilike: Optional[str] = None,
        email_ilike: Optional[str] = None,
        where: Optional[List[Condition]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists users (``sys_user`` table) (``*_ilike``: LIKE patterns, see
        ``incidents``).

        Parameters
        ----------
        user_name : str, optional
            Exact login/user_name.
        active : str, optional
            ``"true"`` or ``"false"``.
        """
        query = self._build_query(
            user_name=user_name, active=active, user_name_ilike=user_name_ilike,
            name_ilike=name_ilike, email_ilike=email_ilike,
        )
        results = self._fetch("sys_user", query=query, limit=limit, where=where)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # CMDB Configuration Items
    # ------------------------------------------------------------------

    def cmdb_ci(
        self,
        name: Optional[str] = None,
        sys_class_name: Optional[str] = None,
        operational_status: Optional[str] = None,
        name_ilike: Optional[str] = None,
        where: Optional[List[Condition]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Lists configuration items (``cmdb_ci`` table — the base CMDB table;
        pass a more specific class via ``table(table_name='cmdb_ci_server')``
        etc. if you need one CI class server-side).
        """
        query = self._build_query(
            name=name, sys_class_name=sys_class_name, operational_status=operational_status,
            name_ilike=name_ilike,
        )
        results = self._fetch("cmdb_ci", query=query, limit=limit, where=where)
        return pd.json_normalize(results, sep="_")

    # ------------------------------------------------------------------
    # Streaming (iter_*) — for use with DuckAPI.stream()
    # ------------------------------------------------------------------

    def iter_table(
        self,
        table_name: str,
        query: Optional[str] = None,
        where: Optional[List[Condition]] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of records at a time from any table."""
        for page in self._iter_pages(table_name, query=query, where=where):
            yield pd.json_normalize(page, sep="_")

    def iter_incidents(
        self,
        state: Optional[str] = None,
        priority: Optional[str] = None,
        assigned_to: Optional[str] = None,
        short_description_ilike: Optional[str] = None,
        where: Optional[List[Condition]] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of incidents at a time."""
        query = self._build_query(
            state=state, priority=priority, assigned_to=assigned_to,
            short_description_ilike=short_description_ilike,
        )
        for page in self._iter_pages("incident", query=query, where=where):
            yield pd.json_normalize(page, sep="_")

    def iter_problems(
        self,
        state: Optional[str] = None,
        short_description_ilike: Optional[str] = None,
        where: Optional[List[Condition]] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of problems at a time."""
        query = self._build_query(state=state, short_description_ilike=short_description_ilike)
        for page in self._iter_pages("problem", query=query, where=where):
            yield pd.json_normalize(page, sep="_")

    def iter_change_requests(
        self,
        state: Optional[str] = None,
        type: Optional[str] = None,
        where: Optional[List[Condition]] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of change requests at a time."""
        query = self._build_query(state=state, type=type)
        for page in self._iter_pages("change_request", query=query, where=where):
            yield pd.json_normalize(page, sep="_")

    def iter_users(
        self,
        active: Optional[str] = None,
        name_ilike: Optional[str] = None,
        where: Optional[List[Condition]] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of users at a time."""
        query = self._build_query(active=active, name_ilike=name_ilike)
        for page in self._iter_pages("sys_user", query=query, where=where):
            yield pd.json_normalize(page, sep="_")

    def iter_cmdb_ci(
        self,
        sys_class_name: Optional[str] = None,
        name_ilike: Optional[str] = None,
        where: Optional[List[Condition]] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yields one page of configuration items at a time."""
        query = self._build_query(sys_class_name=sys_class_name, name_ilike=name_ilike)
        for page in self._iter_pages("cmdb_ci", query=query, where=where):
            yield pd.json_normalize(page, sep="_")

"""
Okta — the Management API (``https://{yourOktaDomain}/api/v1/…``) as tables.

Tables (``{service}_<table>``):

- ``users``          GET /users                       every user, ``DEPROVISIONED`` included (always read through
                                                       ``search`` — the plain list leaves those out)
- ``groups``         GET /groups
- ``group_members``  GET /groups/{group_id}/users     + a ``group_id`` column
- ``user_groups``    GET /users/{user_id}/groups      + a ``user_id`` column
- ``user_factors``   GET /users/{user_id}/factors     + a ``user_id`` column
- ``apps``           GET /apps                        active and inactive (``includeNonDeleted=true``)
- ``app_users``      GET /apps/{app_id}/users         + an ``app_id`` column
- ``app_groups``     GET /apps/{app_id}/groups        + an ``app_id`` column
- ``logs``           GET /logs                        the System Log; ``published`` → ``since`` / ``until``
                                                       (default: the last ``log_days`` = 90 days, Okta's retention)

The per-id tables are lookups: ``JOIN okta_group_members m ON m.group_id = g.id`` reads one group per id
(DuckAPI's join push-down), ``WHERE group_id = '00g…'`` one group.

**Push-down** (each table's own rules — ``@blocked_by``): users and groups through Okta's ``search`` (``eq`` /
``sw`` / ``co`` — ``co`` only on the attributes Okta allows — and, for users, date comparisons), groups also through
``filter`` (``lastUpdated`` / ``lastMembershipUpdated`` comparisons), apps through ``filter`` (id, status, name) or
``q`` (a name/label prefix), the System Log through ``since`` / ``until`` + its SCIM ``filter``. Okta's user search
matches string values case-insensitively (documented): ``=`` and ``LIKE`` send a superset, ``ILIKE`` is exact; for
groups, apps and the log the case rule isn't documented, so only ``=`` and ``LIKE`` go. A date written without an
offset is read by DuckDB in its own time zone, so it reaches Okta widened by ±14 h (any zone) — a superset. When a
``limit`` comes with conditions, each page is checked here with DuckDB's own semantics before counting, so a
superset never costs rows.

**Sorting**: users (``created``, ``activated``, ``statusChanged``, ``lastUpdated``) and groups (``created``,
``lastUpdated``, ``lastMembershipUpdated``) by ``sortBy`` / ``sortOrder`` (search only), the log by
``sortOrder`` on ``published``.

**Pages** are a cursor (``Link: <…after=…>; rel="next"``) with no total — read one after another (Spark: driver).
A next link to another host is refused (the token would follow it). Rate limits: a 429 waits until
``X-Rate-Limit-Reset``; a response with ``X-Rate-Limit-Remaining: 0`` waits before the next request.

**Authentication** (``from_secret`` picks by the keys present):

- ``api_token`` → ``Authorization: SSWS <token>`` (acts as the admin who created it);
- ``client_id`` + ``private_key`` (PEM, or a JWK as JSON; ``key_id`` = its ``kid``) → OAuth 2.0 for Okta, a
  service app signing a ``private_key_jwt`` for ``/oauth2/v1/token`` with ``scopes`` (default the read scopes of
  these tables). DPoP (RFC 9449, on by default for new Okta service apps): ``dpop`` true / false, or unset — tried
  without, and turned on when Okta answers ``invalid_dpop_proof``; the ``use_dpop_nonce`` challenge is answered.

Written from Okta's published OpenAPI spec (``okta-management-openapi-spec``, management 2026.41) and its Python
SDK's OAuth/DPoP code — developer.okta.com was unreachable while building this; the tests replay those shapes.
The parts not stated there: the System Log's page cap (1000 is used), whether ``since`` older than the retention
is refused (``log_days`` lowers it), and the case rule of group/app/log matching (treated as unknown).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import pandas as pd
import requests

from ...common import progress as _progress
from ...common.idempotency import checkpoint
from ...common.logs import PageProgress, get_logger, instrument_session, log_http
from ...common.pushdown import Condition, blocked_by, conditions_to_sql, parse_like, sortable
from ...common.retry import RetryPolicy, send
from ...common.sparkplan import spark_plan

logger = get_logger("okta")

PAGE_SIZE = 200  # Okta's recommended page size for users, groups, group members and apps
LOG_PAGE_SIZE = 1000
APP_USERS_PAGE_SIZE = 500  # the documented maximum
LOG_DAYS = 90  # the System Log's retention
SCOPES = ("okta.users.read", "okta.groups.read", "okta.apps.read", "okta.logs.read")
#: a date without an offset: DuckDB reads it in its own time zone (any of UTC−12…UTC+14)
_WIDEN = dt.timedelta(hours=14)
_DRIVER_WHY = "cursor pages (Link rel=next, after=) with no total: read one after another"
_ONE_WHY = "one response, no pages"

#: every user, as one search expression (the plain list leaves DEPROVISIONED users out; ``ne`` isn't supported —
#: this is the docs' own ``lt … or … gt`` form, plus ``eq``)
ALL_USERS = '(status lt "STAGED" or status eq "STAGED" or status gt "STAGED")'
#: every group type Okta has (``GroupType``) — a search must say something; used only to sort without a condition
ALL_GROUPS = '(type eq "OKTA_GROUP" or type eq "APP_GROUP" or type eq "BUILT_IN")'

#: the default user profile's attributes (``UserProfile``): searchable, string values
USER_PROFILE = ("city", "costCenter", "countryCode", "department", "displayName", "division", "email",
                "employeeNumber", "firstName", "honorificPrefix", "honorificSuffix", "lastName", "locale", "login",
                "manager", "managerId", "middleName", "mobilePhone", "nickName", "organization", "postalAddress",
                "preferredLanguage", "primaryPhone", "profileUrl", "secondEmail", "state", "streetAddress",
                "timezone", "title", "userType", "zipCode")
#: ``co`` (contains) works on these user attributes only
USER_CONTAINS = {"profile.firstName", "profile.lastName", "profile.email", "profile.login"}
GROUP_CONTAINS = {"profile.name", "profile.description"}

#: System Log columns (json_normalize's names, lowercased) → the event's property and its type (``LogEvent``)
LOG_FIELDS = {
    "actor_alternateid": ("actor.alternateId", "string"), "actor_displayname": ("actor.displayName", "string"),
    "actor_id": ("actor.id", "string"), "actor_type": ("actor.type", "string"),
    "authenticationcontext_authenticationprovider": ("authenticationContext.authenticationProvider", "string"),
    "authenticationcontext_authenticationstep": ("authenticationContext.authenticationStep", "integer"),
    "authenticationcontext_credentialprovider": ("authenticationContext.credentialProvider", "string"),
    "authenticationcontext_credentialtype": ("authenticationContext.credentialType", "string"),
    "authenticationcontext_externalsessionid": ("authenticationContext.externalSessionId", "string"),
    "authenticationcontext_interface": ("authenticationContext.interface", "string"),
    "authenticationcontext_issuer_id": ("authenticationContext.issuer.id", "string"),
    "authenticationcontext_issuer_type": ("authenticationContext.issuer.type", "string"),
    "client_device": ("client.device", "string"),
    "client_geographicalcontext_city": ("client.geographicalContext.city", "string"),
    "client_geographicalcontext_country": ("client.geographicalContext.country", "string"),
    "client_geographicalcontext_postalcode": ("client.geographicalContext.postalCode", "string"),
    "client_geographicalcontext_state": ("client.geographicalContext.state", "string"),
    "client_id": ("client.id", "string"), "client_ipaddress": ("client.ipAddress", "string"),
    "client_useragent_browser": ("client.userAgent.browser", "string"),
    "client_useragent_os": ("client.userAgent.os", "string"),
    "client_useragent_rawuseragent": ("client.userAgent.rawUserAgent", "string"),
    "client_zone": ("client.zone", "string"), "displaymessage": ("displayMessage", "string"),
    "eventtype": ("eventType", "string"), "legacyeventtype": ("legacyEventType", "string"),
    "outcome_reason": ("outcome.reason", "string"), "outcome_result": ("outcome.result", "string"),
    "securitycontext_asnumber": ("securityContext.asNumber", "integer"),
    "securitycontext_asorg": ("securityContext.asOrg", "string"),
    "securitycontext_domain": ("securityContext.domain", "string"),
    "securitycontext_isp": ("securityContext.isp", "string"),
    "securitycontext_isproxy": ("securityContext.isProxy", "boolean"),
    "severity": ("severity", "string"), "transaction_id": ("transaction.id", "string"),
    "transaction_type": ("transaction.type", "string"), "uuid": ("uuid", "string"), "version": ("version", "string"),
}

#: sortable columns → the property ``sortBy`` takes (dates only: they sort the same everywhere)
USER_SORTS = {"created": "created", "activated": "activated", "statuschanged": "statusChanged",
              "lastupdated": "lastUpdated"}
GROUP_SORTS = {"created": "created", "lastupdated": "lastUpdated", "lastmembershipupdated": "lastMembershipUpdated"}

_DATES = {
    "users": ("created", "activated", "statusChanged", "lastLogin", "lastUpdated", "passwordChanged"),
    "groups": ("created", "lastUpdated", "lastMembershipUpdated"),
    "apps": ("created", "lastUpdated"),
    "app_users": ("created", "lastUpdated", "statusChanged", "passwordChanged", "lastSync"),
    "app_groups": ("lastUpdated",),
    "factors": ("created", "lastUpdated"),
    "logs": ("published",),
}


class OktaError(RuntimeError):
    """An Okta API error, with the HTTP status and what Okta said (``errorCode``, ``errorSummary``)."""

    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


class _Field(NamedTuple):
    path: str  # the property as Okta names it: ``profile.firstName``
    kind: str  # string / date / boolean / integer / number


#: the default user profile attributes, by their (lowercased) column
_STANDARD = {f"profile_{a}".lower(): _Field(f"profile.{a}", "string") for a in USER_PROFILE}


# ---------------------------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _quote(value: Any) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _instant(value: Any) -> Tuple[dt.datetime, bool]:
    """A pushed date as UTC, and whether it said its offset (False: DuckDB reads it in its own time zone)."""
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        moment = dt.datetime.combine(value, dt.time())
    else:
        text = str(value).strip().replace(" ", "T", 1)
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            moment = dt.datetime.combine(dt.date.fromisoformat(text[:10]), dt.time())
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc), False
    return moment.astimezone(dt.timezone.utc), True


def _okta_time(moment: dt.datetime, upper: bool = False) -> str:
    """``2026-10-01T12:00:00.000Z`` — milliseconds, rounded outward (down for a lower bound, up for an upper)."""
    moment = moment.astimezone(dt.timezone.utc)
    millis = moment.microsecond // 1000
    if upper and moment.microsecond % 1000:
        moment = moment.replace(microsecond=0) + dt.timedelta(milliseconds=millis + 1)
        millis = moment.microsecond // 1000
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{millis:03d}Z"


def _bound(value: Any, op: str) -> dt.datetime:
    """The instant to send for ``col <op> value``: widened by ±14 h when the value has no offset (a superset)."""
    moment, exact = _instant(value)
    if exact:
        return moment
    return moment - _WIDEN if op in ("gt", "gte") else moment + _WIDEN


def _literal(value: Any, kind: str) -> Optional[str]:
    """``value`` written for a search/filter on a property of ``kind`` (None: it doesn't fit)."""
    if kind == "boolean":
        return ("true" if value else "false") if isinstance(value, bool) else None
    if kind in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return repr(value)
    if isinstance(value, (dict, list, tuple)):
        return None
    return _quote(value)


_OPS = {"gt": "gt", "gte": "ge", "lt": "lt", "lte": "le"}


def _clause(field: _Field, cond: Condition, *, contains: bool, insensitive: bool,
            dates: bool) -> Tuple[Optional[str], str]:
    """One condition as a search / filter expression on ``field`` — or (None, why it can't go)."""
    path, kind = field
    if cond.op == "in":
        if kind == "date":
            return None, "a list of dates"
        values = [_literal(v, kind) for v in (cond.value or ())]
        if not values or any(v is None for v in values):
            return None, "a value of another type"
        return "(" + " or ".join(f"{path} eq {v}" for v in values) + ")", ""
    if cond.op == "eq":
        if kind == "date":
            return None, "equality on a date (an exact instant) — use a range"
        value = _literal(cond.value, kind)
        return (f"{path} eq {value}", "") if value is not None else (None, f"not a {kind} value")
    if cond.op in ("like", "ilike"):
        if kind != "string":
            return None, f"LIKE on a {kind}"
        if cond.op == "ilike" and not insensitive:
            return None, "ILIKE: Okta doesn't document case-insensitive matching here"
        pattern = parse_like(cond.value)
        if pattern is None:
            return None, "LIKE pattern not translatable ('_' wildcard / inner '%')"
        if pattern.kind == "equals":
            return f"{path} eq {_quote(pattern.text)}", ""
        if pattern.kind == "startswith":
            return f"{path} sw {_quote(pattern.text)}", ""
        if pattern.kind == "contains" and contains:
            return f"{path} co {_quote(pattern.text)}", ""
        return None, (f"Okta has no {'contains' if pattern.kind == 'contains' else 'ends-with'} "
                      f"for {path}")
    if cond.op in _OPS:
        if kind != "date" or not dates:
            return None, "only dates are compared at Okta (text follows its collation)"
        try:
            moment = _bound(cond.value, cond.op)
        except (TypeError, ValueError):
            return None, "not a date"
        return f"{path} {_OPS[cond.op]} {_quote(_okta_time(moment, upper=cond.op in ('lt', 'lte')))}", ""
    return None, f"operator {cond.op!r}"


def _same_origin(url: str, base: str) -> bool:
    a, b = urlparse(url), urlparse(base)
    return (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower())


def _htu(url: str) -> str:
    """The URL a DPoP proof names: no query, no fragment (RFC 9449 §4.2)."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


# ---------------------------------------------------------------------------------------------------------------
# OAuth 2.0: the client assertion and DPoP
# ---------------------------------------------------------------------------------------------------------------

def _signing_key(private_key: Any) -> Tuple[Any, Optional[str]]:
    """A PEM (``\\n`` as two characters is fixed) or a JWK (dict / JSON) → (an RSA key PyJWT signs with, its kid)."""
    import jwt

    if isinstance(private_key, str) and private_key.strip().startswith("{"):
        private_key = json.loads(private_key)
    if isinstance(private_key, dict):
        return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(private_key)), private_key.get("kid")
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    from .sharepoint import _normalize_pem

    pem = _normalize_pem(str(private_key)).strip().encode("ascii")
    return load_pem_private_key(pem, password=None), None


class _DPoP:
    """An ephemeral RSA key and the proofs it signs (``DPoP:`` header, RFC 9449)."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        self.jwk = {"kty": "RSA", "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
                    "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big"))}
        self.nonce: Optional[str] = None

    def proof(self, method: str, url: str, token: Optional[str] = None) -> str:
        import jwt

        claims: Dict[str, Any] = {"jti": str(uuid.uuid4()), "htm": method.upper(), "htu": _htu(url),
                                  "iat": int(time.time())}
        if self.nonce:
            claims["nonce"] = self.nonce
        if token:
            claims["ath"] = _b64(hashlib.sha256(token.encode("ascii")).digest())
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"typ": "dpop+jwt", "jwk": self.jwk})


# ---------------------------------------------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------------------------------------------

class Okta:
    """
    One Okta org. ``domain``: ``minhaempresa.okta.com`` (or a full ``https://…`` URL, a custom domain too);
    ``api_token`` for SSWS, or ``client_id`` + ``private_key`` (+ ``key_id``, ``scopes``, ``dpop``) for OAuth 2.0.
    ``log_days``: how far back the System Log reads when the query gives no ``published`` bound.
    """

    #: what a failed batched pipeline read does next (duckduck.common.idempotency)
    IDEMPOTENCY = checkpoint("users, groups and the System Log sort by their dates at Okta (sortBy / sortOrder) and "
                             "a date comparison reaches it; the tables differ — the pipeline names the column")
    #: ``where`` takes a join's key values as ``(id eq "a" or id eq "b" …)`` — at most ``IN_MAX`` per request
    WHERE_OPS = frozenset({"eq", "like", "ilike", "gt", "gte", "lt", "lte", "in"})
    IN_MAX = 50

    def __init__(
        self,
        domain: str,
        api_token: Optional[str] = None,
        client_id: Optional[str] = None,
        private_key: Optional[Any] = None,
        key_id: Optional[str] = None,
        scopes: Optional[Any] = None,
        dpop: Optional[bool] = None,
        default_page_size: int = PAGE_SIZE,
        log_days: int = LOG_DAYS,
        verify: bool = True,
    ):
        domain = str(domain or "").strip().rstrip("/")
        if not domain:
            raise ValueError('okta: "domain" is the org\'s host, e.g. "minhaempresa.okta.com"')
        self.base_url = domain if domain.startswith(("https://", "http://")) else f"https://{domain}"
        if not api_token and not (client_id and private_key):
            raise ValueError("okta: give api_token (SSWS), or client_id + private_key (OAuth 2.0 service app)")
        self.default_page_size = int(default_page_size)
        self.log_days = int(log_days)
        self.session = requests.Session()
        self.session.verify = verify
        self.session.headers.update({"Accept": "application/json"})
        instrument_session(self.session, "okta")
        self.retry = RetryPolicy()  # the service's "retry" in duckduck.json replaces it
        self._auth_mode = "ssws" if api_token else "oauth2"
        if api_token:
            self.session.headers["Authorization"] = f"SSWS {api_token}"
        self._client_id = client_id
        self._private_key = private_key
        self._key_id = key_id
        if isinstance(scopes, str):
            scopes = scopes.replace(",", " ").split()
        self.scopes = tuple(scopes or SCOPES)
        self.dpop = dpop
        self._dpop: Optional[_DPoP] = _DPoP() if dpop else None
        self._token: Optional[str] = None
        self._token_type = "Bearer"
        self._token_expires = 0.0
        self._lock = threading.Lock()
        self._profile: Optional[Dict[str, _Field]] = None  # lowercased column → user profile attribute
        self._seen: Dict[str, _Field] = {}  # profile attributes seen in what was read (their exact names)
        self._group_seen: Dict[str, _Field] = {}

    @classmethod
    def from_secret(cls, secret: Dict[str, Any], **overrides: Any) -> "Okta":
        """``{"api_token": …}`` or ``{"client_id": …, "private_key": …, "key_id": …}``; ``domain`` from either."""
        values = {**secret, **overrides}
        domain = values.pop("domain", None) or values.pop("org_url", None) or values.pop("host", None)
        token = values.pop("api_token", None) or values.pop("token", None)
        keys = ("client_id", "private_key", "key_id", "scopes", "dpop", "default_page_size", "log_days", "verify")
        return cls(domain=domain, api_token=token, **{k: values[k] for k in keys if k in values})

    # ---- HTTP ---------------------------------------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1{path}"

    def _ensure_token(self) -> None:
        if self._auth_mode != "oauth2" or (self._token and time.time() < self._token_expires):
            return
        with self._lock:
            if self._token and time.time() < self._token_expires:
                return
            self._fetch_token()

    def _assertion(self, url: str) -> str:
        import jwt

        key, kid = _signing_key(self._private_key)
        now = int(time.time())
        claims = {"iss": self._client_id, "sub": self._client_id, "aud": url, "iat": now, "exp": now + 300,
                  "jti": str(uuid.uuid4())}
        kid = self._key_id or kid
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid} if kid else None)

    def _fetch_token(self) -> None:
        url = f"{self.base_url}/oauth2/v1/token"
        for _ in range(4):
            body = {"grant_type": "client_credentials", "scope": " ".join(self.scopes),
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": self._assertion(url)}  # a fresh one each try: its jti is single-use
            headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
            if self._dpop is not None:
                headers["DPoP"] = self._dpop.proof("POST", url)
            r = send(self.retry, "okta", lambda timeout: requests.post(url, data=body, headers=headers,
                                                                       timeout=timeout, verify=self.session.verify),
                     30, what="OAuth 2.0 token")
            log_http("okta", r, log_body=False)  # the body is the signed client assertion
            if r.ok:
                break
            try:
                payload = r.json()
            except ValueError:
                payload = {}
            error = str(payload.get("error") or payload.get("errorCode") or "")
            nonce = r.headers.get("dpop-nonce")
            if error == "use_dpop_nonce" and nonce and self._dpop is not None:
                self._dpop.nonce = nonce
                continue
            if error == "invalid_dpop_proof" and self._dpop is None and self.dpop is None:
                logger.info("okta: the app requires DPoP — turning it on")
                self._dpop = _DPoP()
                continue
            description = payload.get("error_description") or payload.get("errorSummary") or r.text[:300]
            hint = ""
            if error == "invalid_scope":
                hint = f' — grant the app these scopes or set "scopes" (asked: {" ".join(self.scopes)})'
            elif error == "invalid_dpop_proof" and self.dpop is False:
                hint = ' — the app requires DPoP: remove "dpop": false'
            elif error == "invalid_client":
                hint = " — check client_id, the private key and its key_id (kid) registered on the app"
            raise OktaError(f"Okta OAuth 2.0 token request failed ({r.status_code}): {error}: {description}{hint}",
                            r.status_code, error)
        else:
            raise OktaError("Okta OAuth 2.0 token request kept asking for a new DPoP nonce")
        payload = r.json()
        self._token = payload["access_token"]
        self._token_type = payload.get("token_type") or "Bearer"
        if self._dpop is not None and r.headers.get("dpop-nonce"):
            self._dpop.nonce = r.headers["dpop-nonce"]
        self._token_expires = time.time() + max(int(payload.get("expires_in", 3600)) - 60, 0)

    def _headers(self, url: str) -> Dict[str, str]:
        if self._auth_mode != "oauth2":
            return {}
        if self._token_type.lower() == "dpop" and self._dpop is not None:
            return {"Authorization": f"DPoP {self._token}", "DPoP": self._dpop.proof("GET", url, self._token)}
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None, what: str = "") -> requests.Response:
        """One GET (retried as ``self.retry`` says); a DPoP nonce challenge is answered once; errors → ``OktaError``
        (a 404 is returned: an unknown id has no rows)."""
        self._ensure_token()
        r = None
        for _ in range(2):
            r = send(self.retry, "okta", lambda timeout: self.session.get(
                url, params=params, headers=self._headers(url), timeout=timeout), 60, what=what or _htu(url))
            nonce = r.headers.get("dpop-nonce")
            if r.status_code == 401 and nonce and self._dpop is not None and nonce != self._dpop.nonce:
                self._dpop.nonce = nonce
                continue
            break
        self._pace(r)
        if r.status_code == 404:
            return r
        if r.status_code >= 400:
            try:
                payload = r.json()
            except ValueError:
                payload = {}
            code = payload.get("errorCode") or payload.get("error")
            summary = payload.get("errorSummary") or payload.get("error_description") or r.text[:300]
            causes = "; ".join(c.get("errorSummary", "") for c in payload.get("errorCauses") or [] if isinstance(c, dict))
            hint = ""
            if r.status_code == 401:
                hint = " — the API token or OAuth token was refused (expired, revoked, or for another org)"
            elif r.status_code == 403:
                hint = (" — the token lacks a scope or an admin role for this (OAuth scopes asked: "
                        f"{' '.join(self.scopes)})" if self._auth_mode == "oauth2" else
                        " — the admin who created the API token lacks permission for this")
            raise OktaError(f"Okta {r.status_code} on {what or _htu(url)}: {code}: {summary}"
                            + (f" ({causes})" if causes else "") + hint, r.status_code, code)
        return r

    @staticmethod
    def _pace(r: requests.Response) -> None:
        """No requests left in this rate-limit window: wait for the next one (Okta's advice), at most a minute."""
        try:
            if r.headers.get("X-Rate-Limit-Remaining") == "0" and r.headers.get("X-Rate-Limit-Reset"):
                wait = min(60.0, max(0.0, float(r.headers["X-Rate-Limit-Reset"]) - time.time()) + 1)
                if wait > 0:
                    logger.info("okta: rate limit reached — waiting %.0fs for the next window", wait)
                    _progress.wait(wait, time.sleep)
        except (TypeError, ValueError):
            pass

    def _pages(self, path: str, params: Dict[str, Any], page_size: Optional[int], what: str) -> Iterator[List[Dict]]:
        """Every page of a list, following ``Link: rel="next"`` (never to another host). ``page_size`` None: an
        endpoint without paging parameters."""
        url: Optional[str] = self._url(path)
        query: Optional[Dict[str, Any]] = {k: v for k, v in params.items() if v is not None}
        if page_size:
            query["limit"] = page_size
        progress = PageProgress("okta", what)
        seen = set()
        while url:
            r = self._get(url, query, what)
            query = None  # the next link carries the whole query
            if r.status_code == 404:
                return
            rows = r.json()
            if not isinstance(rows, list):
                raise OktaError(f"Okta answered {what} with {type(rows).__name__}, not a list")
            progress.page(len(rows))
            if not rows:
                return
            yield rows
            following = (r.links.get("next") or {}).get("url")
            if not following:
                return
            if not _same_origin(following, self.base_url):
                raise OktaError(f"Okta's next page link points elsewhere ({urlparse(following).netloc}) — not followed")
            if following in seen:
                raise OktaError(f"Okta returned the same next page twice for {what}")
            seen.add(following)
            url = following

    # ---- frames -------------------------------------------------------------------------------------------------

    def _frame(self, rows: List[Dict], dates: Tuple[str, ...] = (), **columns: Any) -> pd.DataFrame:
        df = pd.json_normalize(rows, sep="_") if rows else pd.DataFrame()
        df = df[[c for c in df.columns if not c.startswith(("_links", "_embedded"))]]
        for column in dates:
            if column in df.columns:
                df[column] = pd.to_datetime(df[column], utc=True, errors="coerce", format="ISO8601")
        for name, value in reversed(list(columns.items())):
            df.insert(0, name, value)
        return df

    def _learn(self, df: pd.DataFrame, seen: Dict[str, _Field]) -> None:
        """Profile attributes as read: their exact names (a SQL column name arrives lowercased)."""
        for column in df.columns:
            if column.startswith("profile_") and column.lower() not in seen:
                attribute = column[len("profile_"):]
                dtype = df[column].dtype
                kind = ("boolean" if pd.api.types.is_bool_dtype(dtype) else
                        "number" if pd.api.types.is_numeric_dtype(dtype) else "string")
                seen[column.lower()] = _Field(f"profile.{attribute}", kind)

    def _limited(self, pages: Iterator[pd.DataFrame], limit: Optional[int],
                 where: Optional[List[Condition]]) -> pd.DataFrame:
        """``limit`` rows from the pages — each page checked first with DuckDB's own semantics when conditions
        went out (what Okta matched can be a superset: case, a widened date)."""
        frames: List[pd.DataFrame] = []
        kept = 0
        try:
            for frame in pages:
                if limit is not None and where and not frame.empty:
                    frame = _exact(frame, where)
                frames.append(frame)
                kept += len(frame)
                if limit is not None and kept >= limit:
                    break
        finally:
            close = getattr(pages, "close", None)
            if callable(close):
                close()
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return df.head(limit) if limit is not None else df

    # ---- users --------------------------------------------------------------------------------------------------

    def _schema_fields(self) -> Dict[str, _Field]:
        """The org's user schema (custom attributes and their types), read once — only for an attribute that
        isn't a default one nor seen in a read yet."""
        if self._profile is None:
            fields: Dict[str, _Field] = {}
            try:
                schema = self._get(self._url("/meta/schemas/user/default"), what="user schema").json()
                for part in ("base", "custom"):
                    props = (((schema or {}).get("definitions") or {}).get(part) or {}).get("properties") or {}
                    for name, spec in props.items():
                        kind = {"boolean": "boolean", "integer": "integer", "number": "number",
                                "string": "string"}.get((spec or {}).get("type"), "other")
                        fields[f"profile_{name}".lower()] = _Field(f"profile.{name}", kind)
            except Exception as exc:  # noqa: BLE001 — custom attributes are then known once read
                logger.info("okta: user schema not read (%s) — custom attributes go once a read has seen them", exc)
            self._profile = fields
        return self._profile

    def _user_field(self, column: str) -> Optional[_Field]:
        column = column.lower()
        top = {"id": _Field("id", "string"), "status": _Field("status", "string"),
               "type_id": _Field("type.id", "string")}
        if column in top:
            return top[column]
        if column in USER_SORTS:
            return _Field(USER_SORTS[column], "date")
        if column.startswith("profile_"):
            return _STANDARD.get(column) or self._seen.get(column) or self._schema_fields().get(column)
        return None

    def _user_clause(self, cond: Condition) -> Tuple[Optional[str], str]:
        field = self._user_field(cond.column)
        if field is None:
            return None, ("not an attribute Okta's user search takes" if not cond.column.startswith("profile_")
                          else "an attribute not in the user schema (or not read yet)")
        if field.kind == "other":
            return None, "an array or object attribute"
        return _clause(field, cond, contains=field.path in USER_CONTAINS, insensitive=True, dates=True)

    def user_blocker(self, cond: Condition) -> Optional[str]:
        return self._user_clause(cond)[1] or None

    def _user_search(self, where: Optional[List[Condition]], order_by) -> Dict[str, Any]:
        clauses = [c for c in (self._user_clause(cond)[0] for cond in where or []) if c]
        params: Dict[str, Any] = {"search": " and ".join(clauses) if clauses else ALL_USERS}
        params.update(_sort(order_by, USER_SORTS))
        return params

    def _user_pages(self, where, order_by, page_size: int) -> Iterator[pd.DataFrame]:
        for rows in self._pages("/users", self._user_search(where, order_by), page_size, "users"):
            df = self._frame(rows, _DATES["users"])
            self._learn(df, self._seen)
            yield df

    @spark_plan("driver", why=_DRIVER_WHY)
    @sortable(*USER_SORTS)
    @blocked_by("user_blocker")
    def users(self, where: Optional[List[Condition]] = None, order_by: Optional[List[Tuple[str, bool]]] = None,
              limit: Optional[int] = None) -> pd.DataFrame:
        """Every user of the org, DEPROVISIONED included (Okta's ``search`` — eventually consistent: a change
        shows within seconds). Profile attributes are ``profile_<attribute>`` columns."""
        size = min(self.default_page_size, limit) if limit and not where else self.default_page_size
        return self._limited(self._user_pages(where, order_by, size), limit, where)

    @sortable(*USER_SORTS)
    @blocked_by("user_blocker")
    def iter_users(self, where: Optional[List[Condition]] = None,
                   order_by: Optional[List[Tuple[str, bool]]] = None) -> Iterator[pd.DataFrame]:
        yield from self._user_pages(where, order_by, self.default_page_size)

    # ---- groups -------------------------------------------------------------------------------------------------

    def _group_field(self, column: str) -> Optional[_Field]:
        column = column.lower()
        top = {"id": _Field("id", "string"), "type": _Field("type", "string"),
               "profile_name": _Field("profile.name", "string"),
               "profile_description": _Field("profile.description", "string")}
        if column in top:
            return top[column]
        if column in GROUP_SORTS:
            return _Field(GROUP_SORTS[column], "date")
        return self._group_seen.get(column)

    def _group_clauses(self, cond: Condition) -> Tuple[Optional[str], Optional[str], str]:
        """(as a ``search`` clause, as a ``filter`` clause, why neither)."""
        field = self._group_field(cond.column)
        if field is None:
            return None, None, "not a property Okta's group search takes"
        searched = None
        if not (field.kind == "date" and cond.op in _OPS):
            searched, why = _clause(field, cond, contains=field.path in GROUP_CONTAINS, insensitive=False,
                                    dates=False)
        else:
            why = ""
        filtered = None
        if field.path in ("id", "type") and cond.op in ("eq", "in"):
            filtered = _clause(field, cond, contains=False, insensitive=False, dates=False)[0]
        elif field.path in ("lastUpdated", "lastMembershipUpdated") and cond.op in _OPS:
            filtered = _clause(field, cond, contains=False, insensitive=False, dates=True)[0]
        if searched is None and filtered is None:
            return None, None, why or "only lastUpdated / lastMembershipUpdated are compared at Okta"
        return searched, filtered, ""

    def group_blocker(self, cond: Condition) -> Optional[str]:
        return self._group_clauses(cond)[2] or None

    def _group_query(self, where, order_by) -> Tuple[Dict[str, Any], bool]:
        """The list's parameters and whether every condition is in them: ``filter`` when it takes them all (and
        nothing is sorted), else ``search`` with what it takes (the rest stays with DuckDB)."""
        conditions = list(where or [])
        if not conditions and not order_by:
            return {}, True
        parts = [self._group_clauses(c) for c in conditions]
        if not order_by and all(f for _, f, _ in parts):
            return {"filter": " and ".join(f for _, f, _ in parts)}, True
        searched = [s for s, _, _ in parts if s]
        params: Dict[str, Any] = {"search": " and ".join(searched) if searched else ALL_GROUPS}
        params.update(_sort(order_by, GROUP_SORTS))
        return params, len(searched) == len(conditions)

    def _group_pages(self, where, order_by, page_size: int) -> Iterator[pd.DataFrame]:
        params, complete = self._group_query(where, order_by)
        if not complete:
            logger.info("okta groups: some conditions don't fit the same request — DuckDB applies them")
        for rows in self._pages("/groups", params, page_size, "groups"):
            df = self._frame(rows, _DATES["groups"])
            self._learn(df, self._group_seen)
            yield df

    @spark_plan("driver", why=_DRIVER_WHY)
    @sortable(*GROUP_SORTS)
    @blocked_by("group_blocker")
    def groups(self, where: Optional[List[Condition]] = None, order_by: Optional[List[Tuple[str, bool]]] = None,
               limit: Optional[int] = None) -> pd.DataFrame:
        """Every group (Okta groups, app groups, built-in ones); ``profile_name``, ``profile_description``…"""
        size = min(self.default_page_size, limit) if limit and not where else self.default_page_size
        return self._limited(self._group_pages(where, order_by, size), limit, where)

    @sortable(*GROUP_SORTS)
    @blocked_by("group_blocker")
    def iter_groups(self, where: Optional[List[Condition]] = None,
                    order_by: Optional[List[Tuple[str, bool]]] = None) -> Iterator[pd.DataFrame]:
        yield from self._group_pages(where, order_by, self.default_page_size)

    # ---- apps ---------------------------------------------------------------------------------------------------

    def _app_clause(self, cond: Condition) -> Tuple[Optional[str], Optional[str], str]:
        """(as a ``filter`` clause, as a ``q`` prefix, why neither)."""
        column = cond.column.lower()
        if column in ("id", "status", "name") and cond.op in ("eq", "in"):
            return _clause(_Field(column, "string"), cond, contains=False, insensitive=False, dates=False)[0], None, ""
        if column in ("name", "label") and cond.op == "like":
            pattern = parse_like(cond.value)
            if pattern is not None and pattern.kind in ("startswith", "equals"):
                return None, pattern.text, ""  # q: name or label starts with it — a superset
            return None, None, "Okta's app q matches a prefix only"
        if column in ("name", "label") and cond.op == "ilike":
            return None, None, "ILIKE: Okta doesn't document case-insensitive app matching"
        return None, None, "Okta filters apps by id, status and name (=), and name/label by prefix"

    def app_blocker(self, cond: Condition) -> Optional[str]:
        return self._app_clause(cond)[2] or None

    def _app_pages(self, where, page_size: int) -> Iterator[pd.DataFrame]:
        parts = [self._app_clause(c) for c in where or []]
        filters = [f for f, _, _ in parts if f]
        prefixes = [q for _, q, _ in parts if q]
        params: Dict[str, Any] = {"includeNonDeleted": "true"}
        if filters:
            params["filter"] = " and ".join(filters)
        elif prefixes:
            params["q"] = prefixes[0]
        if len(filters) + (1 if prefixes and not filters else 0) < len(parts):
            logger.info("okta apps: filter and q don't combine — DuckDB applies the rest")
        for rows in self._pages("/apps", params, page_size, "apps"):
            yield self._frame(rows, _DATES["apps"])

    @spark_plan("driver", why=_DRIVER_WHY)
    @blocked_by("app_blocker")
    def apps(self, where: Optional[List[Condition]] = None, limit: Optional[int] = None) -> pd.DataFrame:
        """Every app integration, active and inactive (``label`` is the name shown, ``name`` the app's key)."""
        size = min(self.default_page_size, limit) if limit and not where else self.default_page_size
        return self._limited(self._app_pages(where, size), limit, where)

    @blocked_by("app_blocker")
    def iter_apps(self, where: Optional[List[Condition]] = None) -> Iterator[pd.DataFrame]:
        yield from self._app_pages(where, self.default_page_size)

    # ---- the System Log -----------------------------------------------------------------------------------------

    def _log_clause(self, cond: Condition) -> Tuple[Optional[str], str]:
        column = cond.column.lower()
        if column == "published":
            if cond.op in ("eq", *_OPS):
                try:
                    _instant(cond.value)
                except (TypeError, ValueError):
                    return None, "not a date"
                return "", ""  # since / until
            return None, f"{cond.op} on the event time"
        if column not in LOG_FIELDS:
            return None, "not a System Log property the filter takes (arrays like target aren't)"
        path, kind = LOG_FIELDS[column]
        if cond.op in _OPS and kind in ("integer", "number"):
            value = _literal(cond.value, kind)
            return (f"{path} {_OPS[cond.op]} {value}", "") if value else (None, "not a number")
        return _clause(_Field(path, kind), cond, contains=True, insensitive=False, dates=False)

    def log_blocker(self, cond: Condition) -> Optional[str]:
        clause, why = self._log_clause(cond)
        return None if clause is not None else why

    def _log_query(self, where, order_by) -> Optional[Dict[str, Any]]:
        """``since`` / ``until`` / ``filter`` / ``sortOrder`` — None when the bounds leave nothing to read."""
        now = until = dt.datetime.now(dt.timezone.utc)
        lower: Optional[dt.datetime] = None
        upper: Optional[dt.datetime] = None
        filters: List[str] = []
        for cond in where or []:
            clause, _ = self._log_clause(cond)
            if clause is None:
                continue
            if clause:
                filters.append(clause)
                continue
            # since/until: inclusive or not isn't documented — a second outward either way (a superset)
            if cond.op in ("gt", "gte", "eq"):
                bound = _bound(cond.value, "gte") - dt.timedelta(seconds=1)
                lower = bound if lower is None else max(lower, bound)
            if cond.op in ("lt", "lte", "eq"):
                bound = _bound(cond.value, "lte") + dt.timedelta(seconds=1)
                upper = bound if upper is None else min(upper, bound)
        until = min(until, upper) if upper else until
        since = lower or now - dt.timedelta(days=self.log_days)  # the retention counts from now
        if since >= until:
            return None
        params: Dict[str, Any] = {"since": _okta_time(since), "until": _okta_time(until, upper=True)}
        if filters:
            params["filter"] = " and ".join(filters)
        if order_by:
            params["sortOrder"] = "DESCENDING" if order_by[0][1] else "ASCENDING"
        return params

    def _log_pages(self, where, order_by, page_size: int) -> Iterator[pd.DataFrame]:
        params = self._log_query(where, order_by)
        if params is None:
            return
        for rows in self._pages("/logs", params, page_size, "system log"):
            yield self._frame(rows, _DATES["logs"])

    @spark_plan("driver", why=_DRIVER_WHY)
    @sortable("published")
    @blocked_by("log_blocker")
    def logs(self, where: Optional[List[Condition]] = None, order_by: Optional[List[Tuple[str, bool]]] = None,
             limit: Optional[int] = None) -> pd.DataFrame:
        """System Log events (``published`` bounds the window; without one, the last ``log_days`` days).
        ``target`` (the objects acted on) is a list per event."""
        size = min(LOG_PAGE_SIZE, limit) if limit and not where else LOG_PAGE_SIZE
        return self._limited(self._log_pages(where, order_by, size), limit, where)

    @sortable("published")
    @blocked_by("log_blocker")
    def iter_logs(self, where: Optional[List[Condition]] = None,
                  order_by: Optional[List[Tuple[str, bool]]] = None) -> Iterator[pd.DataFrame]:
        yield from self._log_pages(where, order_by, LOG_PAGE_SIZE)

    # ---- one id each --------------------------------------------------------------------------------------------

    def _of(self, path: str, page_size: Optional[int], what: str, dates: Tuple[str, ...], **ids: str) -> Iterator[pd.DataFrame]:
        for rows in self._pages(path, {}, page_size, what):
            yield self._frame(rows, dates, **ids)

    @staticmethod
    def _id(value: Any, name: str) -> str:
        text = str(value or "").strip()
        if not text or "/" in text or "?" in text or "#" in text:
            raise ValueError(f"okta: {name} {value!r} isn't an Okta id")
        return text

    @spark_plan("driver", why=_DRIVER_WHY)
    def group_members(self, group_id: str, limit: Optional[int] = None) -> pd.DataFrame:
        """The users in a group (required: ``group_id``)."""
        group_id = self._id(group_id, "group_id")
        size = min(self.default_page_size, limit) if limit else self.default_page_size
        return self._limited(self._of(f"/groups/{group_id}/users", size, f"group {group_id} members",
                                      _DATES["users"], group_id=group_id), limit, None)

    def iter_group_members(self, group_id: str) -> Iterator[pd.DataFrame]:
        group_id = self._id(group_id, "group_id")
        yield from self._of(f"/groups/{group_id}/users", self.default_page_size, f"group {group_id} members",
                            _DATES["users"], group_id=group_id)

    @spark_plan("driver", why=_ONE_WHY)
    def user_groups(self, user_id: str, limit: Optional[int] = None) -> pd.DataFrame:
        """The groups a user is in (required: ``user_id`` — an id or a login)."""
        user_id = self._id(user_id, "user_id")
        return self._limited(self._of(f"/users/{user_id}/groups", None, f"user {user_id} groups",
                                      _DATES["groups"], user_id=user_id), limit, None)

    @spark_plan("driver", why=_ONE_WHY)
    def user_factors(self, user_id: str, limit: Optional[int] = None) -> pd.DataFrame:
        """The factors a user enrolled (required: ``user_id``): type, provider, status."""
        user_id = self._id(user_id, "user_id")
        return self._limited(self._of(f"/users/{user_id}/factors", None,
                                      f"user {user_id} factors", _DATES["factors"], user_id=user_id), limit, None)

    @spark_plan("driver", why=_DRIVER_WHY)
    def app_users(self, app_id: str, limit: Optional[int] = None) -> pd.DataFrame:
        """The users assigned to an app (required: ``app_id``), with their app profile."""
        app_id = self._id(app_id, "app_id")
        size = min(APP_USERS_PAGE_SIZE, limit) if limit else APP_USERS_PAGE_SIZE
        return self._limited(self._of(f"/apps/{app_id}/users", size, f"app {app_id} users", _DATES["app_users"],
                                      app_id=app_id), limit, None)

    def iter_app_users(self, app_id: str) -> Iterator[pd.DataFrame]:
        app_id = self._id(app_id, "app_id")
        yield from self._of(f"/apps/{app_id}/users", APP_USERS_PAGE_SIZE, f"app {app_id} users",
                            _DATES["app_users"], app_id=app_id)

    @spark_plan("driver", why=_DRIVER_WHY)
    def app_groups(self, app_id: str, limit: Optional[int] = None) -> pd.DataFrame:
        """The groups assigned to an app (required: ``app_id``); ``id`` is the group's."""
        app_id = self._id(app_id, "app_id")
        size = min(self.default_page_size, limit) if limit else self.default_page_size
        return self._limited(self._of(f"/apps/{app_id}/groups", size, f"app {app_id} groups", _DATES["app_groups"],
                                      app_id=app_id), limit, None)

    def iter_app_groups(self, app_id: str) -> Iterator[pd.DataFrame]:
        app_id = self._id(app_id, "app_id")
        yield from self._of(f"/apps/{app_id}/groups", self.default_page_size, f"app {app_id} groups",
                            _DATES["app_groups"], app_id=app_id)


def _sort(order_by: Optional[List[Tuple[str, bool]]], sorts: Dict[str, str]) -> Dict[str, str]:
    """DuckAPI's ``order_by`` as ``sortBy`` / ``sortOrder``."""
    if not order_by:
        return {}
    column, descending = order_by[0]
    prop = sorts.get(column.lower())
    return {"sortBy": prop, "sortOrder": "desc" if descending else "asc"} if prop else {}


def _exact(frame: pd.DataFrame, where: List[Condition]) -> pd.DataFrame:
    """The rows of ``frame`` meeting ``where`` as DuckDB reads it (what a LIMIT counts)."""
    import duckdb

    body = conditions_to_sql(where, frame.columns)
    if not body:
        return frame
    conn = duckdb.connect()
    try:
        conn.register("page", frame)
        return conn.execute(f"SELECT * FROM page WHERE {body}").df()
    finally:
        conn.close()


__all__ = ["Okta", "OktaError"]

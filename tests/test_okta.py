"""Okta: the Management API as tables. A fake Okta mounted on the connector's session replays the shapes of Okta's
published OpenAPI spec — Link-header cursor pages, search / filter / q, the System Log's since / until / filter —
and its OAuth 2.0 token endpoint (private_key_jwt, DPoP with a nonce challenge)."""

import datetime as dt
import json
import time
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import requests

from duckduck import DuckAPI
from duckduck.common.retry import RetryPolicy, _retry_after
from duckduck.connectors.api import okta as okta_module
from duckduck.connectors.api.okta import ALL_USERS, Okta, OktaError

BASE = "https://minhaempresa.okta.com"


def _response(request, status, body, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r.headers.update({"Content-Type": "application/json", **(headers or {})})
    r.url = request.url
    r.request = request
    r.encoding = "utf-8"
    return r


class FakeOkta(requests.adapters.BaseAdapter):
    """Routes ``path → handler(params, request) -> (status, body, headers)``; records every call."""

    def __init__(self, routes):
        super().__init__()
        self.routes = routes
        self.calls = []

    def send(self, request, **kwargs):
        url = urlparse(request.url)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append((url.path, params, dict(request.headers)))
        handler = self.routes.get(url.path)
        if handler is None:
            return _response(request, 404, {"errorCode": "E0000007", "errorSummary": "Not found: Resource not found"})
        status, body, headers = handler(params, request)
        return _response(request, status, body, headers)

    def close(self):
        pass

    def paths(self):
        return [path for path, _, _ in self.calls]

    def params(self, path):
        return [p for c, p, _ in self.calls if c == path]


def paged(rows, path, size=None):
    """A list endpoint: ``after`` = how many rows came before; a next link while more are left."""
    def handler(params, request):
        start = int(params.get("after", 0))
        limit = int(params.get("limit", size or 200))
        page = rows(params)[start:start + limit] if callable(rows) else rows[start:start + limit]
        total = len(rows(params) if callable(rows) else rows)
        headers = {}
        if start + limit < total:
            query = "&".join(f"{k}={v}" for k, v in {**params, "after": start + limit, "limit": limit}.items()
                             if k not in ("after", "limit")) + f"&after={start + limit}&limit={limit}"
            headers["Link"] = f'<{BASE}{path}?{query.lstrip("&")}>; rel="next"'
        return 200, page, headers
    return handler


def user(i, status="ACTIVE", first="Ana", updated="2026-10-0{}T12:00:00.000Z"):
    return {"id": f"00u{i}", "status": status, "created": "2025-01-01T00:00:00.000Z",
            "lastUpdated": updated.format(i) if "{}" in updated else updated,
            "activated": None, "type": {"id": "oty1"},
            "profile": {"login": f"user{i}@minhaempresa.com", "email": f"user{i}@minhaempresa.com",
                        "firstName": first, "lastName": f"Silva{i}", "department": "TI", "badgeNumber": i},
            "credentials": {"provider": {"type": "OKTA", "name": "OKTA"}},
            "_links": {"self": {"href": f"{BASE}/api/v1/users/00u{i}"}}}


USERS = [user(1), user(2, status="DEPROVISIONED"), user(3, first="ANA"), user(4, first="Bia"), user(5)]


def connect(routes, **kw):
    okta = Okta(domain="minhaempresa.okta.com", api_token="test-token", **kw)
    fake = FakeOkta(routes)
    okta.session.mount("https://", fake)
    okta.retry = RetryPolicy(retries=1, max_wait=0)
    return okta, fake


def duck_of(okta):
    duck = DuckAPI()
    duck.auto_register({"okta": {"connector": "okta", "domain": "minhaempresa.okta.com",
                                 "authentication": {"type": "local", "api_token": "x"}}})
    for name in list(duck.functions):  # the same tables, on the connector with the fake mounted
        if name.startswith("okta_"):
            duck.register_api_function(name, getattr(okta, name[len("okta_"):]))
    for name, fn in list(duck._streaming_functions.items()):
        if name.startswith("okta_"):
            duck.register_streaming_function(name, getattr(okta, fn.__name__))
    return duck


# ---- users ----------------------------------------------------------------------------------------------------

def test_every_user_is_read_through_search_page_by_page_with_the_api_token():
    okta, fake = connect({"/api/v1/users": paged(USERS, "/api/v1/users")}, default_page_size=2)
    df = okta.users()
    assert list(df["id"]) == ["00u1", "00u2", "00u3", "00u4", "00u5"]
    assert "DEPROVISIONED" in set(df["status"])  # search returns them; the plain list wouldn't
    assert fake.params("/api/v1/users")[0] == {"search": ALL_USERS, "limit": "2"}
    assert len(fake.calls) == 3 and all(h["Authorization"] == "SSWS test-token" for _, _, h in fake.calls)
    assert str(df["lastUpdated"].dtype).startswith("datetime64") and df["lastUpdated"].dt.tz is not None
    assert "profile_firstName" in df.columns and "type_id" in df.columns
    assert not [c for c in df.columns if c.startswith("_links")]


def test_user_conditions_become_one_search_and_a_limit_still_counts_exact_rows():
    okta, fake = connect({"/api/v1/users": paged(USERS, "/api/v1/users")}, default_page_size=2)
    duck = duck_of(okta)
    got = duck.sql("SELECT id FROM okta_users WHERE profile_firstName = 'Ana' AND status = 'ACTIVE' "
                   "AND profile_login LIKE 'user%' AND lastUpdated > '2026-10-02' LIMIT 1").fetchall()
    search = fake.params("/api/v1/users")[0]["search"]
    assert search == ('profile.firstName eq "Ana" and status eq "ACTIVE" and profile.login sw "user" and '
                      'lastUpdated gt "2026-10-01T10:00:00.000Z"')  # a date without an offset: widened 14 h
    # Okta matches values case-insensitively: "ANA" (00u3) comes first — the limit counts only exact rows
    assert got == [("00u5",)]


def test_ilike_is_exact_at_okta_endswith_and_unknown_attributes_stay_with_duckdb():
    okta, fake = connect({"/api/v1/users": paged(USERS, "/api/v1/users"),
                          "/api/v1/meta/schemas/user/default": lambda p, r: (200, {"definitions": {"custom": {
                              "properties": {"badgeNumber": {"type": "integer"}}}}}, {})})
    duck = duck_of(okta)
    plan = duck.explain("SELECT * FROM okta_users WHERE profile_email ILIKE '%@minhaempresa.com' "
                        "AND profile_lastName ILIKE '%silva%' AND profile_badgeNumber = 4 AND lastLogin > '2026-01-01'")
    text = plan.report()
    assert "✓ profile_lastname ILIKE '%silva%' → where" in text
    assert "✓ profile_badgenumber = 4 → where" in text  # a custom attribute: its name and type from the schema
    assert "Okta has no ends-with" in text and "not an attribute Okta's user search takes" in text
    rows = duck.sql("SELECT id FROM okta_users WHERE profile_lastName ILIKE '%silva%' AND profile_badgeNumber = 4").fetchall()
    assert rows == [("00u4",)]
    assert fake.params("/api/v1/users")[-1]["search"] == 'profile.lastName co "silva" and profile.badgeNumber eq 4'


def test_order_by_a_date_reads_the_top_rows_sorted_at_okta():
    def sorted_users(params):
        rows = sorted(USERS, key=lambda u: u[params.get("sortBy", "id")], reverse=params.get("sortOrder") == "desc")
        return rows
    okta, fake = connect({"/api/v1/users": paged(sorted_users, "/api/v1/users")}, default_page_size=2)
    duck = duck_of(okta)
    got = duck.sql("SELECT id FROM okta_users ORDER BY lastUpdated DESC LIMIT 2").fetchall()
    assert got == [("00u5",), ("00u4",)]
    first = fake.params("/api/v1/users")[0]
    assert first["sortBy"] == "lastUpdated" and first["sortOrder"] == "desc" and first["search"] == ALL_USERS
    assert len(fake.calls) == 1  # the first page holds the top 2


# ---- groups and members ----------------------------------------------------------------------------------------

GROUPS = [{"id": f"00g{i}", "type": t, "created": "2024-01-01T00:00:00.000Z",
           "lastUpdated": f"2026-0{i}-01T00:00:00.000Z", "lastMembershipUpdated": "2026-09-01T00:00:00.000Z",
           "profile": {"name": n, "description": None}, "objectClass": ["okta:user_group"]}
          for i, (t, n) in enumerate([("OKTA_GROUP", "Engenharia"), ("APP_GROUP", "Eng AD"),
                                      ("BUILT_IN", "Everyone")], start=1)]


def test_groups_use_filter_or_search_by_what_the_conditions_need():
    okta, fake = connect({"/api/v1/groups": paged(GROUPS, "/api/v1/groups")})
    assert len(okta.groups()) == 3 and fake.params("/api/v1/groups")[0] == {"limit": "200"}  # the plain list
    duck = duck_of(okta)
    duck.sql("SELECT * FROM okta_groups WHERE type = 'OKTA_GROUP' AND lastUpdated >= '2026-01-01T00:00:00Z'").df()
    assert fake.params("/api/v1/groups")[-1]["filter"] == 'type eq "OKTA_GROUP" and lastUpdated ge "2026-01-01T00:00:00.000Z"'
    duck.sql("SELECT * FROM okta_groups WHERE profile_name LIKE 'Eng%'").df()
    assert fake.params("/api/v1/groups")[-1]["search"] == 'profile.name sw "Eng"'
    text = duck.explain("SELECT * FROM okta_groups WHERE profile_name ILIKE 'eng%' AND created > '2026-01-01'").report()
    assert "doesn't document case-insensitive" in text and "only lastUpdated / lastMembershipUpdated" in text


def test_a_join_reads_each_groups_members_and_an_unknown_group_has_none():
    members = {"00g1": [user(1), user(4)], "00g2": [user(5)]}
    routes = {"/api/v1/groups": paged(GROUPS, "/api/v1/groups")}
    for gid, rows in members.items():
        routes[f"/api/v1/groups/{gid}/users"] = paged(rows, f"/api/v1/groups/{gid}/users")
    okta, fake = connect(routes)
    duck = duck_of(okta)
    got = duck.sql("SELECT g.profile_name, m.profile_login FROM okta_groups g "
                   "JOIN okta_group_members m ON m.group_id = g.id WHERE g.type <> 'BUILT_IN' "
                   "ORDER BY 1, 2").fetchall()
    assert got == [("Eng AD", "user5@minhaempresa.com"), ("Engenharia", "user1@minhaempresa.com"),
                   ("Engenharia", "user4@minhaempresa.com")]
    assert "/api/v1/groups/00g3/users" in fake.paths()  # one call per group (Everyone has no members here: 404)
    one = duck.sql("SELECT g.profile_name, m.profile_login, m.group_id FROM okta_groups g "
                   "JOIN okta_group_members m ON m.group_id = g.id WHERE g.type = 'OKTA_GROUP'").fetchall()
    assert one == [("Engenharia", "user1@minhaempresa.com", "00g1"), ("Engenharia", "user4@minhaempresa.com", "00g1")]
    assert okta.group_members("00g9").empty
    with pytest.raises(ValueError, match="isn't an Okta id"):
        okta.group_members("00g1/../users")


# ---- apps -----------------------------------------------------------------------------------------------------

APPS = [{"id": "0oa1", "name": "salesforce", "label": "Salesforce", "status": "ACTIVE", "signOnMode": "SAML_2_0",
         "created": "2025-01-01T00:00:00.000Z", "lastUpdated": "2026-01-01T00:00:00.000Z"},
        {"id": "0oa2", "name": "github", "label": "GitHub", "status": "INACTIVE", "signOnMode": "SAML_2_0",
         "created": "2025-01-01T00:00:00.000Z", "lastUpdated": "2026-01-01T00:00:00.000Z"}]


def test_apps_are_filtered_by_status_or_found_by_a_label_prefix():
    okta, fake = connect({"/api/v1/apps": paged(APPS, "/api/v1/apps"),
                          "/api/v1/apps/0oa1/users": paged([{"id": "00u1", "scope": "USER",
                                                             "profile": {"email": "a@x"}}], "/api/v1/apps/0oa1/users")})
    duck = duck_of(okta)
    assert duck.sql("SELECT id FROM okta_apps WHERE status = 'INACTIVE'").fetchall() == [("0oa2",)]
    assert fake.params("/api/v1/apps")[-1] == {"includeNonDeleted": "true", "filter": 'status eq "INACTIVE"',
                                               "limit": "200"}
    duck.sql("SELECT id FROM okta_apps WHERE label LIKE 'Sales%'").fetchall()
    assert fake.params("/api/v1/apps")[-1]["q"] == "Sales"
    assert list(okta.app_users("0oa1")[["app_id", "id"]].itertuples(index=False, name=None)) == [("0oa1", "00u1")]
    assert fake.params("/api/v1/apps/0oa1/users")[-1]["limit"] == "500"


# ---- the System Log -------------------------------------------------------------------------------------------

def event(i, kind="user.session.start", result="SUCCESS"):
    return {"uuid": f"e{i}", "published": f"2026-10-0{i}T10:00:00.000Z", "eventType": kind, "severity": "INFO",
            "displayMessage": "User login to Okta", "actor": {"id": "00u1", "type": "User",
                                                             "alternateId": "user1@minhaempresa.com"},
            "outcome": {"result": result}, "client": {"ipAddress": "10.0.0.7"},
            "target": [{"id": "0oa1", "type": "AppInstance"}]}


def test_the_system_log_window_filter_and_order_reach_okta():
    events = [event(i) for i in range(1, 6)]
    okta, fake = connect({"/api/v1/logs": paged(events, "/api/v1/logs")})
    duck = duck_of(okta)
    got = duck.sql("SELECT uuid FROM okta_logs WHERE published >= TIMESTAMPTZ '2026-10-02 00:00:00+00' "
                   "AND eventType = 'user.session.start' AND outcome_result = 'SUCCESS' "
                   "AND client_ipAddress LIKE '10.%' ORDER BY published DESC LIMIT 2").fetchall()
    sent = fake.params("/api/v1/logs")[0]
    assert sent["since"] == "2026-10-01T23:59:59.000Z"  # a second outward: inclusive or not isn't documented
    assert sent["filter"] == ('eventType eq "user.session.start" and outcome.result eq "SUCCESS" and '
                              'client.ipAddress sw "10."')
    assert sent["sortOrder"] == "DESCENDING" and "until" in sent
    assert len(got) == 2  # the fake doesn't sort; DuckDB's answer is still the top 2 of what came back
    assert duck.sql("SELECT count(*) FROM okta_logs").fetchone()[0] == 5
    window = fake.params("/api/v1/logs")[-1]
    since = dt.datetime.fromisoformat(window["since"].replace("Z", "+00:00"))
    until = dt.datetime.fromisoformat(window["until"].replace("Z", "+00:00"))
    assert dt.timedelta(days=89) < until - since <= dt.timedelta(days=90, seconds=1)  # log_days
    calls = len(fake.calls)
    assert okta.logs(where=[okta_module.Condition("published", "lt", "2020-01-01T00:00:00Z")]).empty
    assert len(fake.calls) == calls  # the bounds leave nothing: no request
    text = duck.explain("SELECT * FROM okta_logs WHERE eventType ILIKE 'user%'").report()
    assert "doesn't document case-insensitive" in text


def test_an_empty_page_ends_the_log_and_a_next_link_elsewhere_is_refused():
    def polling(params, request):
        return 200, [] if params.get("after") else [event(1)], {"Link": f'<{BASE}/api/v1/logs?after=x>; rel="next"'}
    okta, fake = connect({"/api/v1/logs": polling})
    assert len(okta.logs()) == 1 and len(fake.calls) == 2

    def elsewhere(params, request):
        return 200, [event(1)], {"Link": '<https://attacker.example/api/v1/logs?after=x>; rel="next"'}
    okta, fake = connect({"/api/v1/logs": elsewhere})
    with pytest.raises(OktaError, match="points elsewhere"):
        okta.logs()


# ---- errors, rate limits ---------------------------------------------------------------------------------------

def test_errors_name_the_fix_and_a_rate_limit_waits_for_its_reset():
    okta, _ = connect({"/api/v1/users": lambda p, r: (403, {"errorCode": "E0000006", "errorSummary":
                                                             "You do not have permission"}, {})})
    with pytest.raises(OktaError, match="E0000006.*lacks permission"):
        okta.users()
    tries = []

    def limited(params, request):
        tries.append(time.time())
        if len(tries) == 1:
            return 429, {"errorCode": "E0000047"}, {"X-Rate-Limit-Reset": str(int(time.time()))}
        return 200, [user(1)], {}
    okta, fake = connect({"/api/v1/users": limited})
    assert len(okta.users()) == 1 and len(tries) == 2
    assert float(_retry_after({"X-Rate-Limit-Reset": str(time.time() + 30)})) == pytest.approx(31, abs=1)
    assert _retry_after({"Retry-After": "5", "X-Rate-Limit-Reset": "1"}) == "5"


# ---- OAuth 2.0: private_key_jwt + DPoP ---------------------------------------------------------------------

def test_a_service_app_signs_its_assertion_and_answers_dpop(monkeypatch):
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    posts = []

    def post(url, data, headers, timeout, verify):
        posts.append((url, data, headers))
        request = requests.Request("POST", url).prepare()
        if "DPoP" not in headers:
            return _response(request, 400, {"error": "invalid_dpop_proof",
                                            "error_description": "The DPoP proof JWT header is missing."})
        proof = jwt.decode(headers["DPoP"], options={"verify_signature": False})
        if "nonce" not in proof:
            return _response(request, 400, {"error": "use_dpop_nonce"}, {"dpop-nonce": "n-1"})
        return _response(request, 200, {"access_token": "at-1", "token_type": "DPoP", "expires_in": 3600})

    monkeypatch.setattr(okta_module.requests, "post", post)
    okta = Okta.from_secret({"client_id": "0oasvc", "private_key": pem.replace("\n", "\\n"), "key_id": "k1"},
                            domain="minhaempresa.okta.com")
    fake = FakeOkta({"/api/v1/users": paged([user(1)], "/api/v1/users")})
    okta.session.mount("https://", fake)
    assert len(okta.users()) == 1
    assert len(posts) == 3  # plain → DPoP required → nonce challenge → token
    url, body, _ = posts[-1]
    assert url == f"{BASE}/oauth2/v1/token" and body["scope"] == "okta.users.read okta.groups.read okta.apps.read okta.logs.read"
    assertion = jwt.decode(body["client_assertion"], key.public_key(), algorithms=["RS256"], audience=url)
    assert assertion["iss"] == assertion["sub"] == "0oasvc" and jwt.get_unverified_header(body["client_assertion"])["kid"] == "k1"
    assert posts[1][1]["client_assertion"] != body["client_assertion"]  # a fresh assertion each try
    headers = fake.calls[0][2]
    assert headers["Authorization"] == "DPoP at-1"
    header = jwt.get_unverified_header(headers["DPoP"])
    public = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(header["jwk"]))
    proof = jwt.decode(headers["DPoP"], public, algorithms=["RS256"])  # signed by the key it carries
    assert header["typ"] == "dpop+jwt" and proof["htm"] == "GET" and proof["htu"] == f"{BASE}/api/v1/users"
    assert proof["nonce"] == "n-1" and proof["ath"]  # bound to the access token
    assert set(header["jwk"]) == {"kty", "n", "e"}  # the public part only


def test_a_token_error_says_what_to_change(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    monkeypatch.setattr(okta_module.requests, "post", lambda url, **kw: _response(
        requests.Request("POST", url).prepare(), 400, {"error": "invalid_scope",
                                                       "error_description": "The scope okta.logs.read is not granted"}))
    okta = Okta("minhaempresa.okta.com", client_id="0oasvc", private_key=pem, dpop=False)
    with pytest.raises(OktaError, match="grant the app these scopes"):
        okta.users()
    with pytest.raises(ValueError, match="api_token"):
        Okta("minhaempresa.okta.com")


def test_auto_register_makes_the_tables_and_a_saved_table_keeps_their_rules(tmp_path):
    duck = DuckAPI()
    duck.auto_register({"okta": {"connector": "okta", "domain": "minhaempresa.okta.com",
                                 "authentication": {"type": "local", "api_token": "x"}}})
    names = set(duck.list_tables()["name"])
    assert {"okta_users", "okta_groups", "okta_group_members", "okta_apps", "okta_logs"} <= names
    assert duck.list_tables().set_index("name").loc["okta_users", "source"] == "Okta (Management API)"
    duck.register_view("okta.everyone", {"table": "okta_group_members", "args": {"group_id": "00g3"}})
    from duckduck.common.pushdown import blocker_of

    assert blocker_of(duck.functions["okta_logs"]).__name__ == "log_blocker"

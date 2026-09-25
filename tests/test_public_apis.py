"""Connectors for two public APIs (github.com/public-apis/public-apis): NVD (CVEs) and REST Countries."""

import os
from datetime import datetime
from unittest.mock import MagicMock
from urllib.parse import parse_qsl, urlsplit

import pandas as pd
import pytest

from duckduck import NVD, DuckAPI, RestCountries
from duckduck.nvd import MAX_WINDOW


def _response(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status = MagicMock(side_effect=None if status < 400 else RuntimeError(f"HTTP {status}"))
    return r


# ---------------------------------------------------------------------------
# NVD
# ---------------------------------------------------------------------------

LOG4SHELL = {"cve": {
    "id": "CVE-2021-44228", "sourceIdentifier": "security@apache.org",
    "published": "2021-12-10T10:15:09.143", "lastModified": "2025-02-04T15:15:13.773", "vulnStatus": "Analyzed",
    "cisaExploitAdd": "2021-12-10", "cisaActionDue": "2021-12-24",
    "cisaRequiredAction": "For all affected software assets for which updates exist, the only acceptable remediation "
                          "actions are: 1) Apply updates; OR 2) remove affected assets from agency networks.",
    "cisaVulnerabilityName": "Apache Log4j2 Remote Code Execution Vulnerability",
    "descriptions": [{"lang": "en", "value": "Apache Log4j2 2.0-beta9 through 2.15.0 ... JNDI features ..."},
                     {"lang": "es", "value": "Apache Log4j2 ..."}],
    "metrics": {
        "cvssMetricV31": [
            {"source": "nvd@nist.gov", "type": "Primary",
             "cvssData": {"version": "3.1", "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H",
                          "baseScore": 10.0, "baseSeverity": "CRITICAL"},
             "exploitabilityScore": 3.9, "impactScore": 6.0}],
        "cvssMetricV2": [{"source": "nvd@nist.gov", "type": "Primary",
                          "cvssData": {"version": "2.0", "baseScore": 9.3}, "baseSeverity": "HIGH"}]},
    "weaknesses": [{"source": "security@apache.org", "type": "Secondary",
                    "description": [{"lang": "en", "value": "CWE-917"}]},
                   {"source": "nvd@nist.gov", "type": "Primary",
                    "description": [{"lang": "en", "value": "CWE-502"}]}],
    "references": [{"url": "https://logging.apache.org/log4j/2.x/security.html"}, {"url": "https://example.org"}],
}}
XSS = {"cve": {
    "id": "CVE-2026-1001", "sourceIdentifier": "cna@example.org", "published": "2026-09-01T08:00:00.000",
    "lastModified": "2026-09-02T08:00:00.000", "vulnStatus": "Awaiting Analysis",
    "descriptions": [{"lang": "en", "value": "Cross-site scripting in Example CMS."}],
    "metrics": {"cvssMetricV40": [{"source": "cna@example.org", "type": "Secondary",
                                   "cvssData": {"version": "4.0", "baseScore": 5.1, "baseSeverity": "MEDIUM"}}],
                "cvssMetricV31": [{"source": "cna@example.org", "type": "Secondary",
                                   "cvssData": {"baseScore": 6.1, "baseSeverity": "MEDIUM",
                                                "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N"}}]},
    "weaknesses": [{"description": [{"lang": "en", "value": "CWE-79"}]}],
    "references": [],
}}


def _page(items, total, start=0, per_page=2000):
    return {"resultsPerPage": per_page, "startIndex": start, "totalResults": total, "format": "NVD_CVE",
            "version": "2.0", "timestamp": "2026-09-25T12:00:00.000", "vulnerabilities": items}


class FakeNVD:
    """Serves pages by startIndex from a fixed list of CVEs; records every URL asked."""

    def __init__(self, items, statuses=()):
        self.items, self.urls, self.statuses = items, [], list(statuses)

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if self.statuses:
            return _response({}, status=self.statuses.pop(0))
        q = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        start, size = int(q.get("startIndex", 0)), int(q.get("resultsPerPage", 2000))
        return _response(_page(self.items[start:start + size], len(self.items), start, size))


def _nvd(items, **kw):
    waits = []
    nvd = NVD(sleep=waits.append, **kw)
    fake = FakeNVD(items)
    nvd.session.get = fake
    return nvd, fake, waits


def _params(url):
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def test_a_cve_as_a_row():
    df = NVD._normalize([LOG4SHELL, XSS])
    log4j = df.iloc[0]
    assert (log4j["id"], log4j["severity"], log4j["cvss_score"], log4j["status"]) == \
        ("CVE-2021-44228", "CRITICAL", 10.0, "Analyzed")
    assert log4j["cwe"] == "CWE-917" and log4j["in_kev"] and str(log4j["kev_added"]) == "2021-12-10"
    assert log4j["description"].startswith("Apache Log4j2") and log4j["references"] == 2
    assert log4j["published"] == pd.Timestamp("2021-12-10T10:15:09.143")
    xss = df.iloc[1]
    assert (xss["severity"], xss["cvss4_severity"], xss["cvss4_score"], bool(xss["in_kev"])) == \
        ("MEDIUM", "MEDIUM", 5.1, False)
    assert list(NVD._normalize([]).columns) == list(df.columns)  # an empty answer still has the columns


def test_where_reaches_nvd_as_its_own_parameters():
    nvd, fake, _ = _nvd([LOG4SHELL])
    duck = DuckAPI()
    duck.register_api_function("cves", nvd.cves)
    out = duck.sql("SELECT id, cvss_score FROM cves WHERE severity = 'critical' AND in_kev = true "
                   "AND cwe = 'CWE-917' LIMIT 5").df()
    q = _params(fake.urls[0])
    assert q["cvssV3Severity"] == "CRITICAL" and "hasKev" in q and q["cweId"] == "CWE-917"
    assert q["resultsPerPage"] == "5" and q["startIndex"] == "0" and "noRejected" in q
    assert len(fake.urls) == 1
    assert out.empty  # DuckDB re-applies the WHERE: 'critical' ≠ 'CRITICAL' in SQL — the server match is a superset
    out = duck.sql("SELECT id FROM cves WHERE severity = 'CRITICAL' AND id = 'CVE-2021-44228'").df()
    assert out["id"].tolist() == ["CVE-2021-44228"] and _params(fake.urls[-1])["cveId"] == "CVE-2021-44228"


def test_published_ranges_are_cut_into_nvd_windows():
    nvd, fake, _ = _nvd([XSS])
    duck = DuckAPI()
    duck.register_api_function("cves", nvd.cves)
    duck.sql("SELECT id FROM cves WHERE published >= '2026-01-01' AND published < '2026-09-01'").df()
    windows = [(_params(u)["pubStartDate"], _params(u)["pubEndDate"]) for u in fake.urls]
    assert len(windows) == 3 and windows[0][1] == "2026-09-01T00:00:00.000"  # newest first
    assert windows[-1][0] == "2026-01-01T00:00:00.000"
    spans = [datetime.fromisoformat(b) - datetime.fromisoformat(a) for a, b in windows]
    assert all(s <= MAX_WINDOW for s in spans)
    assert [w[1] for w in windows[1:]] == [w[0] for w in windows[:-1]]  # contiguous: nothing skipped


def test_paginates_to_the_total_and_waits_between_requests():
    items = [{"cve": {**XSS["cve"], "id": f"CVE-2026-{i:04d}"}} for i in range(5)]
    nvd, fake, waits = _nvd(items, default_page_size=2)
    df = nvd.cves()
    assert df["id"].tolist() == [f"CVE-2026-{i:04d}" for i in range(5)] and len(fake.urls) == 3
    assert [_params(u)["startIndex"] for u in fake.urls] == ["0", "2", "4"]
    assert len(waits) == 2 and all(w > 5 for w in waits)  # keyless: ~6s between requests
    assert nvd.cves(limit=3)["id"].tolist() == [f"CVE-2026-{i:04d}" for i in range(3)]


def test_api_key_header_shorter_pauses_and_retry_on_rate_limit():
    nvd = NVD.from_secret({"api_key": "k-123"}, sleep=lambda s: None)
    assert nvd.session.headers["apiKey"] == "k-123" and nvd.request_interval == 0.6
    waits = []
    nvd = NVD(api_key="k", sleep=waits.append, max_retries=2)
    fake = FakeNVD([LOG4SHELL], statuses=[403, 503])
    nvd.session.get = fake
    assert nvd.cves(id="CVE-2021-44228")["id"].tolist() == ["CVE-2021-44228"]
    assert len(fake.urls) == 3 and waits[:2] == [1.0, 2.0]  # backoff doubles
    keyless = NVD.from_secret(None)
    assert "apiKey" not in keyless.session.headers and keyless.request_interval == 6.0


def test_keyword_is_nvd_s_own_search_and_bad_severities_are_refused():
    nvd, fake, _ = _nvd([LOG4SHELL])
    duck = DuckAPI()
    duck.register_api_function("cves", nvd.cves)
    duck.sql("SELECT id FROM cves(keyword='log4j jndi')").df()
    assert _params(fake.urls[0])["keywordSearch"] == "log4j jndi"
    with pytest.raises(ValueError, match="severities"):
        nvd.cves(severity="urgent")
    nvd.include_rejected = True
    nvd.cves()
    assert "noRejected" not in _params(fake.urls[-1])


# ---------------------------------------------------------------------------
# REST Countries
# ---------------------------------------------------------------------------

BRAZIL = {"name": {"common": "Brazil", "official": "Federative Republic of Brazil"}, "cca2": "BR", "cca3": "BRA",
          "ccn3": "076", "independent": True, "unMember": True, "currencies": {"BRL": {"name": "Brazilian real"}},
          "capital": ["Brasília"], "region": "Americas", "subregion": "South America",
          "languages": {"por": "Portuguese"}, "latlng": [-10.0, -55.0], "landlocked": False,
          "borders": ["ARG", "BOL", "COL", "GUF", "GUY", "PRY", "PER", "SUR", "URY", "VEN"], "area": 8515767.0,
          "population": 212559409, "timezones": ["UTC-05:00", "UTC-04:00", "UTC-03:00", "UTC-02:00"],
          "continents": ["South America"], "tld": [".br"], "flag": "🇧🇷"}
PORTUGAL = {"name": {"common": "Portugal", "official": "Portuguese Republic"}, "cca2": "PT", "cca3": "PRT",
            "ccn3": "620", "independent": True, "unMember": True, "currencies": {"EUR": {"name": "Euro"}},
            "capital": ["Lisbon"], "region": "Europe", "subregion": "Southern Europe",
            "languages": {"por": "Portuguese"}, "latlng": [39.5, -8.0], "landlocked": False, "borders": ["ESP"],
            "area": 92090.0, "population": 10305564, "timezones": ["UTC-01:00", "UTC"], "continents": ["Europe"],
            "tld": [".pt"], "flag": "🇵🇹"}
ICELAND = {"name": {"common": "Iceland", "official": "Iceland"}, "cca2": "IS", "cca3": "ISL", "ccn3": "352",
           "region": "Europe", "subregion": "Northern Europe", "capital": ["Reykjavik"], "population": 366425,
           "area": 103000.0, "latlng": [65.0, -18.0], "landlocked": False, "unMember": True, "independent": True}
COUNTRIES = [BRAZIL, PORTUGAL, ICELAND]


class FakeCountries:
    def __init__(self):
        self.calls = []

    def __call__(self, url, params=None, timeout=None):
        path = urlsplit(url).path.replace("/v3.1", "")
        self.calls.append((path, dict(params or {})))
        kind, _, arg = path.strip("/").partition("/")
        arg = arg.lower()
        if kind == "all":
            fields = (params or {})["fields"].split(",")
            assert len(fields) <= 10 and "cca3" in fields  # the API's own limit
            return _response([{f: c[f] for f in fields if f in c} for c in COUNTRIES])
        match = {
            "alpha": lambda c: arg in (c["cca2"].lower(), c["cca3"].lower(), c["ccn3"]),
            "name": lambda c: (arg == c["name"]["common"].lower() or arg == c["name"]["official"].lower())
            if (params or {}).get("fullText") else arg in c["name"]["common"].lower() + " " + c["name"]["official"].lower(),
            "capital": lambda c: arg in [x.lower() for x in c.get("capital", [])],
            "region": lambda c: c["region"].lower() == arg,
            "subregion": lambda c: c["subregion"].lower() == arg,
        }[kind]
        found = [c for c in COUNTRIES if match(c)]
        return _response(found) if found else _response({"status": 404, "message": "Not Found"}, status=404)


def _countries():
    rc = RestCountries()
    fake = FakeCountries()
    rc.session.get = fake
    duck = DuckAPI()
    duck.register_api_function("countries", rc.countries)
    return rc, fake, duck


def test_a_country_as_a_row():
    row = RestCountries._normalize([BRAZIL]).iloc[0]
    assert (row["name"], row["cca3"], row["capital"], row["currencies"], row["languages"]) == \
        ("Brazil", "BRA", "Brasília", "BRL", "Portuguese")
    assert row["borders"].startswith("ARG, BOL") and (row["latitude"], row["longitude"]) == (-10.0, -55.0)
    assert row["un_member"] and not row["landlocked"]


def test_the_most_selective_endpoint_is_asked():
    rc, fake, duck = _countries()
    assert duck.sql("SELECT name FROM countries WHERE cca2 = 'BR'").df()["name"].tolist() == ["Brazil"]
    assert fake.calls[-1] == ("/alpha/BR", {})
    assert duck.sql("SELECT cca3 FROM countries WHERE name = 'Portugal'").df()["cca3"].tolist() == ["PRT"]
    assert fake.calls[-1] == ("/name/Portugal", {"fullText": "true"})
    assert duck.sql("SELECT name FROM countries WHERE region = 'Europe' ORDER BY 1").df()["name"].tolist() == \
        ["Iceland", "Portugal"]
    assert fake.calls[-1] == ("/region/Europe", {})


def test_a_name_pattern_is_a_partial_search_filtered_exactly():
    rc, fake, duck = _countries()
    out = duck.sql("SELECT name FROM countries WHERE name ILIKE 'port%'").df()
    assert out["name"].tolist() == ["Portugal"] and fake.calls[-1][0] == "/name/port"
    # the other filters the function took are applied before LIMIT, so the one row is a right one
    out = duck.sql("SELECT name FROM countries WHERE name ILIKE '%l%' AND region = 'Europe' LIMIT 1").df()
    assert out["name"].tolist() == ["Iceland"] and fake.calls[-1][0] == "/name/l"


def test_all_is_asked_in_field_groups_and_joined():
    rc, fake, duck = _countries()
    df = duck.sql("SELECT name, population, borders FROM countries WHERE population > 1000000 ORDER BY 1").df()
    assert df["name"].tolist() == ["Brazil", "Portugal"] and df.loc[1, "borders"] == "ESP"
    groups = [p["fields"].split(",") for path, p in fake.calls if path == "/all"]
    assert len(groups) == 3 and {f for g in groups for f in g} >= {"name", "population", "borders", "latlng"}


def test_nothing_found_is_an_empty_table_not_an_error():
    rc, fake, duck = _countries()
    assert duck.sql("SELECT name FROM countries WHERE cca3 = 'XXX'").df().empty


def test_auto_register_needs_no_authentication():
    duck = DuckAPI()
    duck.auto_register({"nvd": {"request_interval": 0}, "world": {"connector": "restcountries"}})
    tables = duck.list_tables().set_index("name")
    assert tables.loc["nvd_cves", "source"].startswith("NVD") and "severity =" in tables.loc["nvd_cves", "pushdown"]
    assert tables.loc["world_countries", "source"] == "REST Countries (HTTP API)"


# ---------------------------------------------------------------------------
# Live — only with DUCKDUCK_LIVE=1 and network access to both hosts
# ---------------------------------------------------------------------------

live = pytest.mark.skipif(os.environ.get("DUCKDUCK_LIVE") != "1", reason="set DUCKDUCK_LIVE=1 to call the real APIs")


@live
def test_live_nvd():
    df = NVD().cves(id="CVE-2021-44228")
    assert df["id"].tolist() == ["CVE-2021-44228"] and df.loc[0, "severity"] == "CRITICAL" and df.loc[0, "in_kev"]


@live
def test_live_restcountries():
    rc = RestCountries()
    assert rc.countries(cca2="BR").loc[0, "cca3"] == "BRA"
    assert len(rc.countries()) > 200

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
# REST Countries (v5 — the keyless v3.1 was retired)
# ---------------------------------------------------------------------------

from duckduck.restcountries import RestCountriesError  # noqa: E402


def _v5(common, official, a2, a3, n3, region, subregion, capital, population, **more):
    return {"uuid": f"uuid-{a3}", "names": {"common": common, "official": official},
            "codes": {"alpha_2": a2, "alpha_3": a3, "ccn3": n3},
            "capitals": [{"name": capital, "attributes": {"primary": True},
                          "coordinates": {"lat": 0.0, "lng": 0.0}}],
            "region": region, "subregion": subregion, "population": population, **more}


# the README's sample record, as documented
CANADA = {**_v5("Canada", "Canada", "CA", "CAN", "124", "Americas", "North America", "Ottawa", 41575585),
          "currencies": [{"code": "CAD", "name": "Canadian dollar", "symbol": "$"}],
          "leaders": [{"name": "Mark Carney", "title": "Prime Minister"}], "calling_codes": ["1"],
          "memberships": {"un": True, "nato": True, "g7": True, "commonwealth": True, "eu": False},
          "flag": {"emoji": "🇨🇦", "url_svg": "https://flagcdn.com/ca.svg"}}
BRAZIL = {**_v5("Brazil", "Federative Republic of Brazil", "BR", "BRA", "076", "Americas", "South America",
                "Brasília", 212559409),
          "currencies": [{"code": "BRL", "name": "Brazilian real"}], "languages": [{"code": "por", "name": "Portuguese"}],
          "memberships": {"un": True, "brics": True}, "area": {"kilometers": 8515767.0, "miles": 3287956.0}}
PORTUGAL = _v5("Portugal", "Portuguese Republic", "PT", "PRT", "620", "Europe", "Southern Europe", "Lisbon", 10305564)
ICELAND = _v5("Iceland", "Iceland", "IS", "ISL", "352", "Europe", "Northern Europe", "Reykjavik", 366425)
COUNTRIES = [BRAZIL, CANADA, ICELAND, PORTUGAL]


class FakeCountries:
    """v5: data.objects; limit/offset paging; reads by /code, /names.common, /capitals, /subregion, ?region."""

    def __init__(self, ignore_offset=False):
        self.calls, self.ignore_offset = [], ignore_offset

    def __call__(self, url, params=None, timeout=None):
        path = urlsplit(url).path.replace("/countries/v5", "")
        params = dict(params or {})
        self.calls.append((path, {k: v for k, v in params.items() if k not in ("limit", "offset")}))
        kind, _, arg = path.strip("/").partition("/")
        arg = arg.lower()
        match = {
            "": lambda c: "region" not in params or c["region"] == params["region"],
            "code": lambda c: arg in (c["codes"]["alpha_2"].lower(), c["codes"]["alpha_3"].lower(), c["codes"]["ccn3"]),
            "names.common": lambda c: c["names"]["common"].lower() == arg,
            "capitals": lambda c: arg in [x["name"].lower() for x in c["capitals"]],
            "subregion": lambda c: c["subregion"].lower() == arg,
        }[kind]
        found = [c for c in COUNTRIES if match(c)]
        if not found:
            return _response({"error": {"message": "not found"}}, status=404)
        offset = 0 if self.ignore_offset else int(params.get("offset", 0))
        page = found[offset:offset + int(params.get("limit", 250))]
        r = _response({"data": {"objects": page}})
        r.url = url
        return r


def _countries(**fake):
    rc = RestCountries(api_key="rc_test")
    f = FakeCountries(**fake)
    rc.session.get = f
    duck = DuckAPI()
    duck.register_api_function("countries", rc.countries)
    return rc, f, duck


def test_a_country_as_a_row():
    df = RestCountries._normalize([CANADA, BRAZIL]).set_index("cca3")
    ca, br = df.loc["CAN"], df.loc["BRA"]
    assert (ca["name"], ca["cca2"], ca["capital"], ca["population"], ca["currencies"]) == \
        ("Canada", "CA", "Ottawa", 41575585, "CAD")
    assert ca["memberships"] == "commonwealth, g7, nato, un" and bool(ca["un_member"])  # eu: false left out
    assert ca["leaders"] == "Mark Carney (Prime Minister)" and ca["flag"] == "🇨🇦" and ca["calling_codes"] == "1"
    assert br["languages"] == "Portuguese" and br["area"] == 8515767.0 and pd.isna(br["latitude"])
    assert pd.isna(br["borders"]) and pd.isna(br["timezones"])  # not in the record: empty, never guessed


def test_the_most_selective_read_is_asked():
    rc, fake, duck = _countries()
    assert rc.session.headers["Authorization"] == "Bearer rc_test"
    assert duck.sql("SELECT name FROM countries WHERE cca2 = 'BR'").df()["name"].tolist() == ["Brazil"]
    assert fake.calls[-1] == ("/code/BR", {})
    assert duck.sql("SELECT cca3 FROM countries WHERE name = 'Portugal'").df()["cca3"].tolist() == ["PRT"]
    assert fake.calls[-1] == ("/names.common/Portugal", {})
    assert duck.sql("SELECT name FROM countries WHERE capital = 'Ottawa'").df()["name"].tolist() == ["Canada"]
    assert duck.sql("SELECT name FROM countries WHERE region = 'Europe' ORDER BY 1").df()["name"].tolist() == \
        ["Iceland", "Portugal"]
    assert fake.calls[-1] == ("", {"region": "Europe"})


def test_other_filters_are_applied_before_the_limit():
    rc, fake, duck = _countries()
    out = duck.sql("SELECT name FROM countries WHERE name ILIKE '%l%' AND region = 'Europe' LIMIT 1").df()
    assert out["name"].tolist() == ["Iceland"] and fake.calls[-1] == ("", {"region": "Europe"})
    out = duck.sql("SELECT name, population FROM countries WHERE population > 100000000").df()
    assert out["name"].tolist() == ["Brazil"]


def test_paging_stops_when_nothing_new_comes():
    rc, fake, duck = _countries(ignore_offset=True)  # an API that ignores offset must not loop
    rc.page_size = 2
    assert len(rc.countries()) == 2 and len(fake.calls) == 2  # the repeated page ends it
    rc, fake, duck = _countries()
    rc.page_size = 2
    assert rc.countries()["cca3"].tolist() == ["BRA", "CAN", "ISL", "PRT"] and len(fake.calls) == 3


def test_nothing_found_is_an_empty_table_not_an_error():
    rc, fake, duck = _countries()
    assert duck.sql("SELECT name FROM countries WHERE cca3 = 'XXX'").df().empty


def test_a_notice_or_the_retired_api_is_an_error_never_a_row():
    rc = RestCountries(api_key="k")
    notice = _response({"message": "REST Countries v3.1 has been retired. See restcountries.com"})
    notice.url = "https://files-03.restcountries.com/countries.00/legacy.json?fields=cca3"
    rc.session.get = lambda url, params=None, timeout=None: notice
    with pytest.raises(RestCountriesError, match="retired REST Countries API"):
        rc.countries()
    odd = _response({"message": "maintenance"})
    odd.url = "https://api.restcountries.com/countries/v5"
    rc.session.get = lambda url, params=None, timeout=None: odd
    with pytest.raises(RestCountriesError, match="data.objects.*maintenance"):
        rc.countries()
    denied = _response({"error": "invalid api key"}, status=401)
    rc.session.get = lambda url, params=None, timeout=None: denied
    with pytest.raises(RestCountriesError, match="401.*invalid api key"):
        rc.countries()


def test_a_key_is_required():
    with pytest.raises(ValueError, match="sign-up"):
        RestCountries()
    assert RestCountries.from_secret({"api_key": "k"}).session.headers["Authorization"] == "Bearer k"


def test_auto_register():
    duck = DuckAPI()
    duck.auto_register({"nvd": {"request_interval": 0},
                        "world": {"connector": "restcountries", "authentication": {"type": "local", "api_key": "k"}}})
    tables = duck.list_tables().set_index("name")
    assert tables.loc["nvd_cves", "source"].startswith("NVD") and "severity =" in tables.loc["nvd_cves", "pushdown"]
    assert tables.loc["world_countries", "source"] == "REST Countries (HTTP API)"
    with pytest.raises(Exception, match="authentication"):
        DuckAPI().auto_register({"world": {"connector": "restcountries"}})


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
    rc = RestCountries(api_key=os.environ.get("RESTCOUNTRIES_API_KEY", "rc_live_demo"))
    assert rc.countries(cca2="BR").loc[0, "cca3"] == "BRA"
    assert len(rc.countries()) > 200

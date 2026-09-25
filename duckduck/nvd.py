"""
NVD — the U.S. National Vulnerability Database's CVE API 2.0, as DuckAPI tables.

Listed in `public-apis <https://github.com/public-apis/public-apis>`_
(Security → *National Vulnerability Database*). No key is needed; with one
(``api_key``, sent as the ``apiKey`` header — request it at
https://nvd.nist.gov/developers/request-an-api-key) NVD allows 50 requests
per 30 seconds instead of 5, and the wrapper waits less between pages.

.. note::
   Built from NVD's published API documentation
   (https://nvd.nist.gov/developers/vulnerabilities); the session that
   wrote it couldn't reach ``services.nvd.nist.gov`` (network policy), so
   the tests replay the documented response shape. Verify against the live
   API before relying on it.

Table: ``cves`` — one row per CVE (withdrawn, *Rejected* ones left out unless
``include_rejected=True``)::

    id | published | last_modified | status | description | severity | cvss_score | cvss_vector
       | cvss4_severity | cvss4_score | cwe | in_kev | kev_added | kev_due | kev_action | kev_name
       | source | references

Push-down (the operator → parameter convention of ``duckduck.pushdown``):

==========================================  =======================================================
SQL                                          NVD request
==========================================  =======================================================
``id = 'CVE-2021-44228'``                    ``cveId``
``severity = 'CRITICAL'``                    ``cvssV3Severity`` (``severity`` is CVSS v3.x)
``cvss4_severity = 'HIGH'``                  ``cvssV4Severity``
``cwe = 'CWE-79'``                           ``cweId``
``in_kev = true``                            ``hasKev`` (in CISA's Known Exploited Vulnerabilities)
``published >= / > / <= / < '2026-09-01'``   ``pubStartDate`` / ``pubEndDate`` — split into windows
                                             of at most 120 days, NVD's limit
``last_modified >= … / <= …``                ``lastModStartDate`` / ``lastModEndDate`` (same)
``LIMIT n``                                  ``resultsPerPage=n``, one request
==========================================  =======================================================

``keyword`` (inline: ``nvd_cves(keyword='log4j')``) is NVD's own
``keywordSearch`` — words in the description, not a SQL pattern, so a
``description LIKE`` stays with DuckDB.

Every match NVD does is a superset of what the column holds (``severity``
is one CVSS v3 metric's severity; NVD matches a CVE having *any* v3 metric
of that severity), and DuckDB re-applies the whole ``WHERE`` anyway.

Pagination: ``startIndex`` / ``resultsPerPage`` (up to 2,000) until
``totalResults``; NVD asks clients to pause between requests (6 s without
a key), and answers 403/429/503 when rushed — retried with backoff.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pandas as pd
import requests

from .logs import PageProgress, get_logger, instrument_session

logger = get_logger("nvd")

BASE_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
MAX_PAGE = 2000
#: NVD refuses a published / last-modified range longer than this.
MAX_WINDOW = timedelta(days=120)
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_RETRY_STATUS = (403, 429, 500, 502, 503, 504)

COLUMNS = ["id", "published", "last_modified", "status", "description", "severity", "cvss_score", "cvss_vector",
           "cvss4_severity", "cvss4_score", "cwe", "in_kev", "kev_added", "kev_due", "kev_action", "kev_name",
           "source", "references"]


class NVD:
    """
    Client for the NVD CVE API 2.0.

    Parameters
    ----------
    api_key : str, optional
        NVD API key (``apiKey`` header): a higher rate limit, shorter pauses.
    default_page_size : int
        Results per page when paginating (max 2,000).
    request_interval : float, optional
        Seconds between requests. Default: 6 without a key, 0.6 with one
        (NVD's guidance for its 5 / 50 requests per 30 s limits).
    max_retries : int
        Retries of a request NVD answered 403 / 429 / 5xx (rate limit or
        overload), each after twice the previous pause.
    base_url : str
        The CVE API endpoint (a mirror or a proxy).
    include_rejected : bool
        Keep CVEs NVD marked *Rejected* (withdrawn IDs); left out by default.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_page_size: int = MAX_PAGE,
        request_interval: Optional[float] = None,
        max_retries: int = 3,
        base_url: str = BASE_URL,
        include_rejected: bool = False,
        timeout: float = 60.0,
        verify: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url
        self.default_page_size = max(1, min(int(default_page_size), MAX_PAGE))
        self.request_interval = request_interval if request_interval is not None else (0.6 if api_key else 6.0)
        self.max_retries = max_retries
        self.include_rejected = include_rejected
        self.timeout = timeout
        self._sleep = sleep
        self._last_request: Optional[float] = None

        self.session = requests.Session()
        instrument_session(self.session, "nvd")
        self.session.headers.update({"Accept": "application/json"})
        if api_key:
            self.session.headers["apiKey"] = api_key
        self.session.verify = verify

    @classmethod
    def from_secret(cls, secret: Optional[Dict[str, Any]] = None, **overrides) -> "NVD":
        """
        Builds NVD from a credentials dict (``auto_register()``): ``api_key``
        is optional — without an ``authentication`` block it runs keyless.
        """
        secret = secret or {}
        api_key = overrides.pop("api_key", None) or secret.get("api_key") or secret.get("apiKey")
        return cls(api_key=api_key, **overrides)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _pause(self) -> None:
        if self._last_request is not None and self.request_interval > 0:
            wait = self.request_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)

    def _get(self, params: List[Tuple[str, Optional[str]]]) -> Dict[str, Any]:
        """One request (after the pause NVD asks for), retried on a rate limit or overload."""
        query = "&".join(k if v is None else f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params)
        url = f"{self.base_url}?{query}" if query else self.base_url
        delay = max(self.request_interval, 1.0)
        for attempt in range(self.max_retries + 1):
            self._pause()
            response = self.session.get(url, timeout=self.timeout)
            self._last_request = time.monotonic()
            if response.status_code in _RETRY_STATUS and attempt < self.max_retries:
                logger.info("NVD answered %s — retrying in %.0fs", response.status_code, delay)
                self._sleep(delay)
                self._last_request = None  # the backoff was the pause
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------
    # Push-down → request parameters
    # ------------------------------------------------------------------

    @staticmethod
    def _when(value: Any) -> Optional[datetime]:
        if value is None or value == "":
            return None
        ts = pd.Timestamp(value)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        return ts.to_pydatetime()

    @classmethod
    def _range(cls, gte: Any, gt: Any, lte: Any, lt: Any) -> Tuple[Optional[datetime], Optional[datetime]]:
        """The widest window the comparisons allow — ``>`` as ``>=``, ``<`` as ``<=``: a superset."""
        starts = [t for t in (cls._when(gte), cls._when(gt)) if t is not None]
        ends = [t for t in (cls._when(lte), cls._when(lt)) if t is not None]
        return (max(starts) if starts else None), (min(ends) if ends else None)

    @staticmethod
    def _windows(start: Optional[datetime], end: Optional[datetime],
                 now: Optional[datetime] = None) -> List[Tuple[datetime, datetime]]:
        """``[start, end]`` cut into NVD-sized windows, newest first; an open end is now, an open start
        only with an end (then 120 days before it)."""
        if start is None and end is None:
            return []
        end = end or (now or datetime.now(timezone.utc).replace(tzinfo=None))
        start = start or (end - MAX_WINDOW)
        if start > end:
            return []
        out = []
        while end > start:
            lo = max(start, end - MAX_WINDOW)
            out.append((lo, end))
            end = lo
        return out or [(start, end)]

    @staticmethod
    def _stamp(t: datetime) -> str:
        return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}"

    @staticmethod
    def _severity(value: Optional[str], param: str) -> Optional[str]:
        if value is None:
            return None
        sev = str(value).strip().upper()
        if sev not in SEVERITIES:
            raise ValueError(f"{param}={value!r}: NVD severities are {', '.join(SEVERITIES)}")
        return sev

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def _plans(self, keyword, id, severity, cvss4_severity, cwe, in_kev,
               published, modified) -> List[List[Tuple[str, Optional[str]]]]:
        """The request parameter lists to run — one per date window (or one, with no dates)."""
        base: List[Tuple[str, Optional[str]]] = [] if self.include_rejected else [("noRejected", None)]
        if id:
            base.append(("cveId", str(id).strip().upper()))
        if keyword:
            base.append(("keywordSearch", keyword))
        if severity:
            base.append(("cvssV3Severity", self._severity(severity, "severity")))
        if cvss4_severity:
            base.append(("cvssV4Severity", self._severity(cvss4_severity, "cvss4_severity")))
        if cwe:
            base.append(("cweId", str(cwe).strip().upper()))
        if in_kev is True or str(in_kev).lower() == "true":
            base.append(("hasKev", None))  # in_kev = false can't be asked: DuckDB filters it
        pub = self._windows(*published)
        mod = self._windows(*modified)
        if pub and mod:  # both ranges: every pair of windows (NVD takes one of each per request)
            return [base + [("pubStartDate", self._stamp(a)), ("pubEndDate", self._stamp(b)),
                            ("lastModStartDate", self._stamp(c)), ("lastModEndDate", self._stamp(d))]
                    for a, b in pub for c, d in mod]
        if pub:
            return [base + [("pubStartDate", self._stamp(a)), ("pubEndDate", self._stamp(b))] for a, b in pub]
        if mod:
            return [base + [("lastModStartDate", self._stamp(a)), ("lastModEndDate", self._stamp(b))] for a, b in mod]
        return [base]

    def _iter_pages(self, params: List[Tuple[str, Optional[str]]], limit: Optional[int] = None,
                    what: str = "cves") -> Iterator[List[Dict[str, Any]]]:
        """``startIndex`` / ``resultsPerPage`` until ``totalResults`` (or ``limit`` rows)."""
        progress = PageProgress("nvd", what)
        start, left = 0, limit
        while True:
            size = self.default_page_size if left is None else max(1, min(left, MAX_PAGE))
            payload = self._get(params + [("resultsPerPage", str(size)), ("startIndex", str(start))])
            items = payload.get("vulnerabilities") or []
            total = payload.get("totalResults")
            progress.page(len(items), total_pages=-(-int(total) // size) if total else None, total_rows=total)
            if items:
                yield items
            start += len(items)
            if left is not None:
                left -= len(items)
            if not items or (total is not None and start >= int(total)) or (left is not None and left <= 0):
                return

    def cves(
        self,
        keyword: Optional[str] = None,
        id: Optional[str] = None,
        severity: Optional[str] = None,
        cvss4_severity: Optional[str] = None,
        cwe: Optional[str] = None,
        in_kev: Optional[bool] = None,
        published_gte: Optional[str] = None,
        published_gt: Optional[str] = None,
        published_lte: Optional[str] = None,
        published_lt: Optional[str] = None,
        last_modified_gte: Optional[str] = None,
        last_modified_gt: Optional[str] = None,
        last_modified_lte: Optional[str] = None,
        last_modified_lt: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """CVEs from the National Vulnerability Database (CVSS, CWE, CISA KEV), filters pushed to NVD."""
        rows: List[Dict[str, Any]] = []
        for params in self._plans(keyword, id, severity, cvss4_severity, cwe, in_kev,
                                  self._range(published_gte, published_gt, published_lte, published_lt),
                                  self._range(last_modified_gte, last_modified_gt, last_modified_lte,
                                              last_modified_lt)):
            for page in self._iter_pages(params, limit=None if limit is None else limit - len(rows)):
                rows.extend(page)
            if limit is not None and len(rows) >= limit:
                break
        return self._normalize(rows[:limit] if limit is not None else rows)

    def iter_cves(
        self,
        keyword: Optional[str] = None,
        id: Optional[str] = None,
        severity: Optional[str] = None,
        cvss4_severity: Optional[str] = None,
        cwe: Optional[str] = None,
        in_kev: Optional[bool] = None,
        published_gte: Optional[str] = None,
        published_gt: Optional[str] = None,
        published_lte: Optional[str] = None,
        published_lt: Optional[str] = None,
        last_modified_gte: Optional[str] = None,
        last_modified_gt: Optional[str] = None,
        last_modified_lte: Optional[str] = None,
        last_modified_lt: Optional[str] = None,
    ) -> Iterator[pd.DataFrame]:
        """``cves`` one page at a time (``DuckAPI.stream``)."""
        for params in self._plans(keyword, id, severity, cvss4_severity, cwe, in_kev,
                                  self._range(published_gte, published_gt, published_lte, published_lt),
                                  self._range(last_modified_gte, last_modified_gt, last_modified_lte,
                                              last_modified_lt)):
            for page in self._iter_pages(params):
                df = self._normalize(page)
                if not df.empty:
                    yield df

    # ------------------------------------------------------------------
    # Response → rows
    # ------------------------------------------------------------------

    @staticmethod
    def _metric(metrics: Dict[str, Any], *keys: str) -> Dict[str, Any]:
        """The ``Primary`` metric of the first version present (else its first metric)."""
        for key in keys:
            entries = metrics.get(key) or []
            if entries:
                return next((m for m in entries if m.get("type") == "Primary"), entries[0])
        return {}

    @classmethod
    def _row(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        cve = item.get("cve", item)
        metrics = cve.get("metrics") or {}
        v3 = cls._metric(metrics, "cvssMetricV31", "cvssMetricV30")
        v4 = cls._metric(metrics, "cvssMetricV40")
        v3data, v4data = v3.get("cvssData") or {}, v4.get("cvssData") or {}
        english = next((d.get("value") for d in cve.get("descriptions") or [] if d.get("lang") == "en"), None)
        cwes = [d.get("value") for w in cve.get("weaknesses") or [] for d in w.get("description") or []
                if str(d.get("value", "")).startswith("CWE-")]
        return {
            "id": cve.get("id"),
            "published": cve.get("published"),
            "last_modified": cve.get("lastModified"),
            "status": cve.get("vulnStatus"),
            "description": english,
            "severity": v3data.get("baseSeverity") or v3.get("baseSeverity"),
            "cvss_score": v3data.get("baseScore"),
            "cvss_vector": v3data.get("vectorString"),
            "cvss4_severity": v4data.get("baseSeverity"),
            "cvss4_score": v4data.get("baseScore"),
            "cwe": cwes[0] if cwes else None,
            "in_kev": cve.get("cisaExploitAdd") is not None,
            "kev_added": cve.get("cisaExploitAdd"),
            "kev_due": cve.get("cisaActionDue"),
            "kev_action": cve.get("cisaRequiredAction"),
            "kev_name": cve.get("cisaVulnerabilityName"),
            "source": cve.get("sourceIdentifier"),
            "references": len(cve.get("references") or []),
        }

    @classmethod
    def _normalize(cls, items: List[Dict[str, Any]]) -> pd.DataFrame:
        df = pd.DataFrame([cls._row(i) for i in items], columns=COLUMNS)
        for col in ("published", "last_modified"):
            df[col] = pd.to_datetime(df[col], errors="coerce")
        for col in ("kev_added", "kev_due"):
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
        return df

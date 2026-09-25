"""
REST Countries (https://restcountries.com, API v5) as a DuckAPI table.

Listed in `public-apis <https://github.com/public-apis/public-apis>`_
(Geocoding → *REST Countries*). The old keyless v3.1 API
(``restcountries.com/v3.1``) was retired: it now redirects to a static
notice. v5 lives at ``api.restcountries.com/countries/v5`` and needs an API
key — free tier at https://restcountries.com/sign-up — sent as
``Authorization: Bearer <key>``. (Their README uses ``rc_live_demo`` as a
demo key for trying it out.)

.. note::
   Built from the project's README (the v5 endpoints, the
   ``data.objects`` envelope and a sample record); the full field
   reference at restcountries.com/docs couldn't be read while writing it,
   and the session couldn't reach the API. Fields the sample doesn't show
   (languages, borders, timezones, coordinates, area…) are read in every
   shape they plausibly take, and missing ones are left empty — never
   guessed. Paging uses ``limit`` / ``offset`` and stops as soon as a page
   brings nothing new, so an API that ignores ``offset`` can't loop.

Table: ``countries`` — one row per country::

    name | official_name | cca2 | cca3 | ccn3 | region | subregion | capital | population | area
         | languages | currencies | continents | timezones | borders | independent | un_member
         | landlocked | latitude | longitude | tld | flag | calling_codes | memberships | leaders

(``capital`` is the primary capital; list fields are comma-separated:
currency and border codes, language names, the memberships that are true —
``un, nato, g7``…; ``leaders`` as ``name (title)``.)

Push-down — the most selective read the ``WHERE`` allows; every other
condition the function took is applied to the rows it got, exactly as DuckDB
would, so ``LIMIT`` stays right:

===============================================  ======================================
SQL                                               request
===============================================  ======================================
``cca2 = 'BR'`` / ``cca3 = 'BRA'`` / ``ccn3``      ``/code/{code}``
``name = 'Brazil'``                               ``/names.common/{name}``
``capital = 'Brasília'``                          ``/capitals/{capital}``
``subregion = 'South America'``                   ``/subregion/{subregion}``
``region = 'Americas'``                           ``?region=Americas``
anything else (``name ILIKE`` included)           every country (~250 rows)
===============================================  ======================================
"""

import json
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pandas as pd
import requests

from .logs import PageProgress, get_logger, instrument_session
from .pushdown import require_like

logger = get_logger("restcountries")

BASE_URL = "https://api.restcountries.com/countries/v5"
#: Where the retired keyless API was — a request that ends up here gets the migration notice, not countries.
RETIRED = "restcountries.com/v3"
MAX_PAGES = 50
COLUMNS = ["name", "official_name", "cca2", "cca3", "ccn3", "region", "subregion", "capital", "population", "area",
           "languages", "currencies", "continents", "timezones", "borders", "independent", "un_member", "landlocked",
           "latitude", "longitude", "tld", "flag", "calling_codes", "memberships", "leaders"]


class RestCountriesError(RuntimeError):
    """The API answered something that isn't a list of countries (a notice, an error, a login page)."""


class RestCountries:
    """
    Client for REST Countries v5.

    Parameters
    ----------
    api_key : str
        Your key (``Authorization: Bearer``) — free at restcountries.com/sign-up.
    base_url : str
        The countries endpoint (a proxy, or a self-hosted copy of the same API).
    page_size : int
        Countries per request when reading many (there are ~250).
    """

    def __init__(self, api_key: Optional[str] = None, base_url: str = BASE_URL, page_size: int = 250,
                 timeout: float = 30.0, verify: bool = True):
        if not api_key:
            raise ValueError(
                "REST Countries v5 needs an API key (the keyless v3.1 API was retired): get one free at "
                "https://restcountries.com/sign-up and put it in the service's authentication block "
                '("api_key") — or try their demo key, rc_live_demo'
            )
        self.base_url = base_url.rstrip("/")
        self.page_size = max(1, int(page_size))
        self.timeout = timeout
        self.session = requests.Session()
        instrument_session(self.session, "restcountries")
        self.session.headers.update({"Accept": "application/json", "Authorization": f"Bearer {api_key}"})
        self.session.verify = verify

    @classmethod
    def from_secret(cls, secret: Optional[Dict[str, Any]] = None, **overrides) -> "RestCountries":
        """For ``auto_register()``: ``api_key`` from the ``authentication`` block (or its secret)."""
        secret = secret or {}
        api_key = overrides.pop("api_key", None) or secret.get("api_key") or secret.get("token")
        return cls(api_key=api_key, **overrides)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """The countries of one response and the total it reports (if it does); 404 → none."""
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        final = str(getattr(response, "url", "") or "")
        if RETIRED in final or "legacy.json" in final:
            raise RestCountriesError(f"{final} is the retired REST Countries API (v3.1) — use {BASE_URL}")
        if response.status_code == 404:
            return [], 0
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            raise RestCountriesError(f"REST Countries answered {response.status_code}: {_snippet(payload, response)}")
        return _objects(payload, response)

    @staticmethod
    def _q(value: Any) -> str:
        return requests.utils.quote(str(value), safe="")

    def _iter_pages(self, path: str, params: Optional[Dict[str, Any]] = None) -> Iterator[List[Dict[str, Any]]]:
        """``limit`` / ``offset`` until a page brings nothing new (or the reported total is reached)."""
        progress = PageProgress("restcountries", path or "/")
        seen, offset = set(), 0
        for _ in range(MAX_PAGES):
            items, total = self._get(path, {**(params or {}), "limit": self.page_size, "offset": offset})
            new = [i for i in items if _key(i) not in seen]
            seen.update(_key(i) for i in new)
            progress.page(len(new), total_rows=total)
            if new:
                yield new
            offset += len(items)
            if not new or len(items) < self.page_size or (total is not None and len(seen) >= total):
                return

    def _read(self, cca2, cca3, ccn3, name, capital, subregion, region) -> List[Dict[str, Any]]:
        """The most selective read the filters allow."""
        code = cca3 or cca2 or ccn3
        if code:
            path, params = f"/code/{self._q(code)}", None
        elif name:
            path, params = f"/names.common/{self._q(name)}", None
        elif capital:
            path, params = f"/capitals/{self._q(capital)}", None
        elif subregion:
            path, params = f"/subregion/{self._q(subregion)}", None
        elif region:
            path, params = "", {"region": region}
        else:
            path, params = "", None
        return [c for page in self._iter_pages(path, params) for c in page]

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def countries(
        self,
        cca2: Optional[str] = None,
        cca3: Optional[str] = None,
        ccn3: Optional[str] = None,
        name: Optional[str] = None,
        name_ilike: Optional[str] = None,
        capital: Optional[str] = None,
        subregion: Optional[str] = None,
        region: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Countries: names, ISO codes, region, capital, population, area, languages, currencies, memberships."""
        like = require_like(name_ilike, "name_ilike") if name_ilike is not None else None
        df = self._normalize(self._read(cca2, cca3, ccn3, name, capital, subregion, region))
        for col, value in (("cca2", cca2), ("cca3", cca3), ("ccn3", ccn3), ("name", name), ("capital", capital),
                           ("subregion", subregion), ("region", region)):
            if value is not None:
                df = df[df[col] == value]
        if like is not None:
            names, text = df["name"].fillna("").str.lower(), like.text.lower()
            match: Callable[[pd.Series], pd.Series] = {
                "equals": lambda s: s == text, "startswith": lambda s: s.str.startswith(text),
                "endswith": lambda s: s.str.endswith(text), "contains": lambda s: s.str.contains(text, regex=False),
            }[like.kind]
            df = df[match(names)]
        df = df.sort_values("name", kind="stable").reset_index(drop=True)
        return df.head(limit) if limit is not None else df

    # ------------------------------------------------------------------
    # Response → rows
    # ------------------------------------------------------------------

    @classmethod
    def _row(cls, c: Dict[str, Any]) -> Dict[str, Any]:
        names = c.get("names") or c.get("name") or {}
        codes = c.get("codes") or {}
        capitals = c.get("capitals") or c.get("capital") or []
        primary = next((x for x in capitals if isinstance(x, dict) and (x.get("attributes") or {}).get("primary")),
                       capitals[0] if capitals else None)
        lat, lng = _coordinates(c)
        memberships = c.get("memberships") or {}
        flag = c.get("flag")
        return {
            "name": names.get("common") if isinstance(names, dict) else names,
            "official_name": names.get("official") if isinstance(names, dict) else None,
            "cca2": codes.get("alpha_2") or c.get("cca2"),
            "cca3": codes.get("alpha_3") or c.get("cca3"),
            "ccn3": codes.get("ccn3") or c.get("ccn3"),
            "region": c.get("region") or None,
            "subregion": c.get("subregion") or None,
            "capital": (primary.get("name") if isinstance(primary, dict) else primary) or None,
            "population": c.get("population"),
            "area": _area(c.get("area")),
            "languages": _joined(c.get("languages"), "name"),
            "currencies": _joined(c.get("currencies"), "code", dict_keys=True),
            "continents": _joined(c.get("continents")),
            "timezones": _joined(c.get("timezones")),
            "borders": _joined(c.get("borders"), "alpha_3"),
            "independent": _flag(c, "independent", "classification"),
            "un_member": memberships.get("un") if "un" in memberships else c.get("unMember"),
            "landlocked": _flag(c, "landlocked", "geography"),
            "latitude": lat, "longitude": lng,
            "tld": _joined(c.get("tld") or c.get("tlds")),
            "flag": flag.get("emoji") if isinstance(flag, dict) else flag,
            "calling_codes": _joined(c.get("calling_codes")),
            "memberships": ", ".join(sorted(k for k, v in memberships.items() if v is True)) or None,
            "leaders": ", ".join(f"{x.get('name')} ({x.get('title')})" if x.get("title") else str(x.get("name"))
                                 for x in c.get("leaders") or [] if isinstance(x, dict) and x.get("name")) or None,
        }

    @classmethod
    def _normalize(cls, items: List[Dict[str, Any]]) -> pd.DataFrame:
        return pd.DataFrame([cls._row(c) for c in items], columns=COLUMNS)


# ---------------------------------------------------------------------------
# Reading the response — whatever isn't a list of countries is an error, never a row
# ---------------------------------------------------------------------------

def _snippet(payload: Any, response: Any) -> str:
    text = json.dumps(payload)[:300] if payload is not None else str(getattr(response, "text", ""))[:300]
    return text or "(empty body)"


def _objects(payload: Any, response: Any) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    objects = data.get("objects") if isinstance(data, dict) else None
    if isinstance(objects, list) and all(isinstance(o, dict) for o in objects):
        total = next((m.get(k) for m in (data.get("meta"), data, payload.get("meta")) if isinstance(m, dict)
                      for k in ("total", "total_count", "count") if isinstance(m.get(k), int)), None)
        return objects, total
    raise RestCountriesError(
        f"REST Countries didn't answer with countries (expected data.objects) from "
        f"{getattr(response, 'url', '')}: {_snippet(payload, response)}"
    )


def _key(item: Dict[str, Any]) -> str:
    codes = item.get("codes") or {}
    return str(item.get("uuid") or codes.get("alpha_3") or item.get("cca3") or json.dumps(item, sort_keys=True))


def _joined(values: Any, field: str = "name", dict_keys: bool = False) -> Optional[str]:
    """A list of strings / of objects (their ``field``) / a dict (its keys or values) → ``"a, b"``."""
    if not values:
        return None
    if isinstance(values, dict):
        items = list(values.keys()) if dict_keys else [v.get(field) if isinstance(v, dict) else v
                                                       for v in values.values()]
    else:
        items = [v.get(field) or v.get("code") or v.get("name") if isinstance(v, dict) else v for v in values]
    items = sorted({str(v) for v in items if v not in (None, "")})
    return ", ".join(items) or None


def _area(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        value = value.get("kilometers", value.get("km2"))
    return float(value) if isinstance(value, (int, float)) else None


def _flag(c: Dict[str, Any], key: str, group: str) -> Optional[bool]:
    for where in (c, c.get(group) or {}):
        if isinstance(where.get(key), bool):
            return where[key]
    return None


def _coordinates(c: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    for where in (c.get("coordinates"), (c.get("geography") or {}).get("coordinates")):
        if isinstance(where, dict) and "lat" in where:
            return where.get("lat"), where.get("lng", where.get("lon"))
    latlng = c.get("latlng")
    if isinstance(latlng, list) and len(latlng) == 2:
        return latlng[0], latlng[1]
    return None, None

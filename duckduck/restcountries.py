"""
REST Countries (https://restcountries.com, API v3.1) as a DuckAPI table.

Listed in `public-apis <https://github.com/public-apis/public-apis>`_
(Geocoding → *REST Countries*): no key, no pagination — one request returns
every country that matches.

.. note::
   Built from the API's published documentation; the session that wrote
   it couldn't reach ``restcountries.com`` (network policy), so the tests
   replay the documented response shape. ``/all`` requires a ``fields``
   list of at most 10 fields, so the wrapper asks for the columns in
   groups and joins them on ``cca3``. Verify against the live API before
   relying on it.

Table: ``countries`` — one row per country::

    name | official_name | cca2 | cca3 | ccn3 | region | subregion | capital | population | area
         | languages | currencies | continents | timezones | borders | independent | un_member
         | landlocked | latitude | longitude | tld | flag

(``capital`` is the first capital; ``languages`` / ``currencies`` /
``continents`` / ``timezones`` / ``borders`` / ``tld`` are comma-separated —
currency and border codes, language names.)

Push-down — one endpoint per call, the most selective the ``WHERE`` allows;
every other condition the function took is applied to the rows it got, so
``LIMIT`` stays right:

===============================================  ==================================
SQL                                               request
===============================================  ==================================
``cca2 = 'BR'`` / ``cca3 = 'BRA'`` / ``ccn3``      ``/alpha/{code}``
``name = 'Brazil'``                               ``/name/{name}?fullText=true``
``name ILIKE '%bra%'`` (or LIKE: a superset)      ``/name/{text}`` (partial, any case)
``capital = 'Brasília'``                          ``/capital/{capital}``
``subregion = 'South America'``                   ``/subregion/{subregion}``
``region = 'Americas'``                           ``/region/{region}``
anything else                                     ``/all`` (DuckDB filters)
===============================================  ==================================
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests

from .logs import get_logger, instrument_session
from .pushdown import require_like

logger = get_logger("restcountries")

BASE_URL = "https://restcountries.com/v3.1"
#: ``/all`` refuses more than this many ``fields`` per request.
MAX_FIELDS = 10
#: The API fields the columns are made from (``cca3`` joins the groups ``/all`` is asked in).
FIELDS = ["cca3", "name", "cca2", "ccn3", "region", "subregion", "capital", "population", "area", "languages",
          "currencies", "continents", "timezones", "borders", "independent", "unMember", "landlocked", "latlng",
          "tld", "flag"]
COLUMNS = ["name", "official_name", "cca2", "cca3", "ccn3", "region", "subregion", "capital", "population", "area",
           "languages", "currencies", "continents", "timezones", "borders", "independent", "un_member", "landlocked",
           "latitude", "longitude", "tld", "flag"]


class RestCountries:
    """
    Client for REST Countries v3.1.

    Parameters
    ----------
    base_url : str
        The API root (a mirror or a self-hosted copy).
    """

    def __init__(self, base_url: str = BASE_URL, timeout: float = 30.0, verify: bool = True):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        instrument_session(self.session, "restcountries")
        self.session.headers.update({"Accept": "application/json"})
        self.session.verify = verify

    @classmethod
    def from_secret(cls, secret: Optional[Dict[str, Any]] = None, **overrides) -> "RestCountries":
        """For ``auto_register()``: there are no credentials — only options (``base_url``, ``timeout``)."""
        return cls(**overrides)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """A list of countries; a 404 (nothing matches) is an empty one."""
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else [data]

    def _all(self) -> List[Dict[str, Any]]:
        """``/all`` in groups of at most ``MAX_FIELDS`` fields, joined on ``cca3``."""
        rest = [f for f in FIELDS if f != "cca3"]
        merged: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(rest), MAX_FIELDS - 1):
            group = ["cca3"] + rest[i:i + MAX_FIELDS - 1]
            for item in self._get("/all", {"fields": ",".join(group)}):
                merged.setdefault(item.get("cca3"), {}).update(item)
        return list(merged.values())

    @staticmethod
    def _q(value: Any) -> str:
        return requests.utils.quote(str(value), safe="")

    def _endpoint(self, cca2, cca3, ccn3, name, name_ilike, capital, subregion,
                  region) -> Tuple[str, Optional[Dict[str, str]]]:
        """The most selective request the filters allow."""
        code = cca3 or cca2 or ccn3
        if code:
            return f"/alpha/{self._q(code)}", None
        if name:
            return f"/name/{self._q(name)}", {"fullText": "true"}
        if name_ilike:
            return f"/name/{self._q(require_like(name_ilike, 'name_ilike').text)}", None
        if capital:
            return f"/capital/{self._q(capital)}", None
        if subregion:
            return f"/subregion/{self._q(subregion)}", None
        if region:
            return f"/region/{self._q(region)}", None
        return "/all", None

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
        """Countries: names, ISO codes, region, capital, population, area, languages, currencies, borders."""
        path, params = self._endpoint(cca2, cca3, ccn3, name, name_ilike, capital, subregion, region)
        items = self._all() if path == "/all" else self._get(path, params)
        df = self._normalize(items)
        # the endpoint applied one filter (loosely: any case, partial names); every one the function took is
        # applied here exactly as DuckDB would, so the first `limit` rows are the right ones
        for col, value in (("cca2", cca2), ("cca3", cca3), ("ccn3", ccn3), ("name", name), ("capital", capital),
                           ("subregion", subregion), ("region", region)):
            if value is not None:
                df = df[df[col] == value]
        if name_ilike is not None:
            like = require_like(name_ilike, "name_ilike")
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

    @staticmethod
    def _joined(values: Any) -> Optional[str]:
        if not values:
            return None
        return ", ".join(sorted(str(v) for v in values))

    @classmethod
    def _row(cls, c: Dict[str, Any]) -> Dict[str, Any]:
        names = c.get("name") or {}
        latlng = c.get("latlng") or [None, None]
        capitals = c.get("capital") or []
        return {
            "name": names.get("common"),
            "official_name": names.get("official"),
            "cca2": c.get("cca2"), "cca3": c.get("cca3"), "ccn3": c.get("ccn3"),
            "region": c.get("region") or None, "subregion": c.get("subregion") or None,
            "capital": capitals[0] if capitals else None,
            "population": c.get("population"), "area": c.get("area"),
            "languages": cls._joined((c.get("languages") or {}).values()),
            "currencies": cls._joined((c.get("currencies") or {}).keys()),
            "continents": cls._joined(c.get("continents")),
            "timezones": cls._joined(c.get("timezones")),
            "borders": cls._joined(c.get("borders")),
            "independent": c.get("independent"), "un_member": c.get("unMember"), "landlocked": c.get("landlocked"),
            "latitude": latlng[0] if len(latlng) > 0 else None, "longitude": latlng[1] if len(latlng) > 1 else None,
            "tld": cls._joined(c.get("tld")),
            "flag": c.get("flag"),
        }

    @classmethod
    def _normalize(cls, items: List[Dict[str, Any]]) -> pd.DataFrame:
        return pd.DataFrame([cls._row(c) for c in items], columns=COLUMNS)

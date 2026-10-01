"""
Which files of a Glue table a query needs — the way Athena finds them.

Athena doesn't list a table's whole S3 prefix. It asks the Glue catalog for
the partitions the query's WHERE selects (``get_partitions`` with an
``Expression``, served from partition indexes when the table has them), or
— for a table with *partition projection* — computes the partition values
and their locations from the table's properties without asking anyone. Then
it lists only those locations, skipping hidden files (a name starting with
``_`` or ``.``), and reads every other object as the table's format whatever
its extension (Athena's own output files have none).

This module holds the pure parts (no AWS calls): the Glue expression for a
set of push-down conditions, the projected partitions, the location
template, which listed keys are data files. ``duckduck.glue.GlueTable`` does
the calls.
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from itertools import product
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .pushdown import Condition

#: Glue column types → DuckDB types (a partition value is a string in Glue; this is its column's type)
DUCK_TYPES = {
    "string": "VARCHAR", "varchar": "VARCHAR", "char": "VARCHAR", "tinyint": "TINYINT", "smallint": "SMALLINT",
    "int": "INTEGER", "integer": "INTEGER", "bigint": "BIGINT", "float": "FLOAT", "double": "DOUBLE",
    "boolean": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMP",
}
_NUMERIC = ("tinyint", "smallint", "int", "integer", "bigint", "float", "double", "decimal")
_GLUE_OPS = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

#: at most this many partitions from a projection; more → the whole location is listed instead
MAX_PROJECTED = 10000


def duck_type(glue_type: str) -> str:
    t = (glue_type or "string").strip().lower()
    if t.startswith("decimal"):
        return t.upper()
    return DUCK_TYPES.get(t.split("(")[0], "VARCHAR")


def partition_conditions(where: Optional[Iterable[Condition]], keys: Dict[str, str]) -> List[Condition]:
    """The push-down conditions on partition keys (eq / in / comparisons) — the ones that can pick partitions."""
    return [c for c in where or () if c.column.lower() in keys and c.op in (*_GLUE_OPS, "in")]


def _glue_literal(value: Any, glue_type: str) -> Optional[str]:
    t = (glue_type or "string").lower()
    if t.startswith(_NUMERIC):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None  # 'abc' against an int key: leave it to DuckDB
        return str(int(number)) if number.is_integer() else repr(number)
    return "'" + str(value).replace("'", "''") + "'"


def glue_expression(conditions: Sequence[Condition], keys: Dict[str, str]) -> Optional[str]:
    """
    ``get_partitions``' ``Expression`` for these conditions (ANDed), or None
    when none can be written. A condition that can't be written is left out:
    the partitions returned are then a superset, and DuckDB still filters.
    """
    parts = []
    for c in conditions:
        name = next((k for k in keys if k == c.column.lower()), None)
        if name is None:
            continue
        glue_type = keys[name]
        if c.op == "in":
            values = [_glue_literal(v, glue_type) for v in c.value or ()]
            if not values or any(v is None for v in values):
                continue
            parts.append(f"{name} IN ({', '.join(values)})")
            continue
        literal = _glue_literal(c.value, glue_type)
        if literal is not None:
            parts.append(f"{name} {_GLUE_OPS[c.op]} {literal}")
    return " AND ".join(parts) if parts else None


def is_data_file(relative_key: str, size: Optional[int]) -> bool:
    """A listed object that's part of the table: not empty, no path part hidden (``_SUCCESS``, ``.hive-staging``,
    ``_temporary`` — a ``_k=v`` partition folder isn't hidden), not a folder marker (``x_$folder$``) nor a checksum. Any other name — with or without an
    extension — is a data file."""
    if not relative_key or relative_key.endswith("/") or size == 0:
        return False
    if relative_key.endswith("$folder$") or relative_key.endswith(".crc"):
        return False
    # hidden as Spark reads it: a part starting with "." — or with "_" unless it's a k=v partition (_load_date=…)
    return not any(part.startswith(".") or (part.startswith("_") and "=" not in part)
                   for part in relative_key.split("/") if part)


def split_s3(uri: str) -> Tuple[str, str]:
    """``s3://bucket/a/b/`` → (``bucket``, ``a/b/``) — the key prefix always ends with ``/`` (or is empty)."""
    m = re.match(r"^s3a?n?://([^/]+)/?(.*)$", uri)
    if not m:
        raise ValueError(f"not an S3 location: {uri!r}")
    prefix = m.group(2)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return m.group(1), prefix


def hive_location(location: str, values: Dict[str, str], keys: Sequence[str]) -> str:
    return location.rstrip("/") + "/" + "/".join(f"{k}={values[k]}" for k in keys) + "/"


def is_hive_layout(partitions: Sequence[Tuple[str, Dict[str, str]]], keys: Sequence[str]) -> bool:
    """Every partition's location names its values as ``key=value`` — DuckDB's hive_partitioning reads them back."""
    return all(all(f"/{k}={v[k]}" in loc.rstrip("/") + "/" or loc.rstrip("/").endswith(f"/{k}={v[k]}") for k in keys)
               for loc, v in partitions)


# ---------------------------------------------------------------------------
# Partition projection (Athena): partitions computed from table properties
# ---------------------------------------------------------------------------

_JAVA_FORMAT = [("yyyy", "%Y"), ("MM", "%m"), ("dd", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S"),
                ("yy", "%y")]
_UNITS = {"YEARS": "years", "MONTHS": "months", "WEEKS": "weeks", "DAYS": "days", "HOURS": "hours",
          "MINUTES": "minutes", "SECONDS": "seconds"}


def _strftime_of(java: str) -> Optional[str]:
    """A Java DateTimeFormatter pattern (``yyyy-MM-dd``, ``yyyy/MM/dd/HH``) → strftime; None if it uses more."""
    out, i = "", 0
    while i < len(java):
        for token, code in _JAVA_FORMAT:
            if java.startswith(token, i):
                out += code
                i += len(token)
                break
        else:
            ch = java[i]
            if ch.isalpha():
                return None  # a field this doesn't know (E, a, S…): can't project exactly
            if ch == "'":
                end = java.find("'", i + 1)
                if end < 0:
                    return None
                out += java[i + 1:end].replace("%", "%%")
                i = end + 1
                continue
            out += "%%" if ch == "%" else ch
            i += 1
    return out


def _add(moment: datetime, amount: int, unit: str) -> datetime:
    if unit in ("years", "months"):
        months = amount * (12 if unit == "years" else 1)
        year, month = divmod(moment.month - 1 + months, 12)
        year += moment.year
        day = min(moment.day, calendar.monthrange(year, month + 1)[1])
        return moment.replace(year=year, month=month + 1, day=day)
    return moment + timedelta(**{unit: amount})


def _date_bound(text: str, fmt: str, now: datetime) -> Optional[datetime]:
    text = text.strip()
    m = re.fullmatch(r"NOW(?:\s*([+-])\s*(\d+)\s*(YEARS?|MONTHS?|WEEKS?|DAYS?|HOURS?|MINUTES?|SECONDS?))?", text,
                     re.I)
    if m:
        if not m.group(1):
            return now
        unit = _UNITS[m.group(3).upper().rstrip("S") + "S"]
        return _add(now, int(m.group(2)) * (1 if m.group(1) == "+" else -1), unit)
    try:
        return datetime.strptime(text, fmt)
    except ValueError:
        return None


def _parse_value(value: Any, fmt: Optional[str]) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    for candidate in ([fmt] if fmt else []) + ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
        try:
            return datetime.strptime(text[:len(datetime(2000, 1, 1).strftime(candidate))], candidate)
        except ValueError:
            continue
    return None


def _keep(value: Any, conditions: Sequence[Condition], as_key: Any) -> bool:
    """Whether a candidate partition value passes every condition on its key (compared as ``as_key`` says)."""
    for c in conditions:
        mine = as_key(value)
        if c.op == "in":
            wanted = [as_key(v) for v in c.value or ()]
            if None in wanted or mine is None:
                continue  # can't compare: keep (superset)
            if mine not in wanted:
                return False
            continue
        other = as_key(c.value)
        if other is None or mine is None:
            continue
        try:
            ok = {"eq": mine == other, "gt": mine > other, "gte": mine >= other, "lt": mine < other,
                  "lte": mine <= other}[c.op]
        except TypeError:
            continue
        if not ok:
            return False
    return True


def _projected_values(name: str, params: Dict[str, str], conditions: Sequence[Condition],
                      now: datetime) -> Optional[List[str]]:
    kind = (params.get(f"projection.{name}.type") or "").lower()
    mine = [c for c in conditions if c.column.lower() == name]
    if kind == "enum":
        values = [v.strip() for v in (params.get(f"projection.{name}.values") or "").split(",") if v.strip()]
        return [v for v in values if _keep(v, mine, lambda x: str(x))]
    if kind == "integer":
        try:
            low, high = [int(x) for x in (params.get(f"projection.{name}.range") or "").split(",")]
        except ValueError:
            return None
        step = int(params.get(f"projection.{name}.interval") or 1)
        digits = int(params.get(f"projection.{name}.digits") or 0)
        for c in mine:  # narrow the range first: it may be huge
            try:
                values = [int(float(v)) for v in (c.value if c.op == "in" else [c.value])]
            except (TypeError, ValueError):
                continue
            if c.op in ("eq", "in"):
                return [str(v).zfill(digits) for v in sorted(set(values)) if low <= v <= high and (v - low) % step == 0
                        and _keep(v, mine, lambda x: _int(x))]
            if c.op in ("gt", "gte"):
                low = max(low, values[0] + (1 if c.op == "gt" else 0))
            else:
                high = min(high, values[0] - (1 if c.op == "lt" else 0))
        if (high - low) // max(step, 1) > MAX_PROJECTED:
            return None
        return [str(v).zfill(digits) for v in range(low, high + 1, step) if _keep(v, mine, lambda x: _int(x))]
    if kind == "date":
        java = params.get(f"projection.{name}.format") or ""
        fmt = _strftime_of(java)
        bounds = (params.get(f"projection.{name}.range") or "").split(",")
        if fmt is None or len(bounds) != 2:
            return None
        low, high = (_date_bound(b, fmt, now) for b in bounds)
        if low is None or high is None:
            return None
        unit = _UNITS.get((params.get(f"projection.{name}.interval.unit") or "").upper(),
                          "hours" if "%H" in fmt else "days")
        step = int(params.get(f"projection.{name}.interval") or 1)
        start, high = _date_window(low, high, mine, fmt)
        n = 0
        if start > low and unit not in ("years", "months"):  # jump to the query's first value, on the grid
            n = max(0, int((start - low) / timedelta(**{unit: step})))
        out, moment, first = [], _add(low, n * step, unit), n
        while moment <= high:
            out.append(moment.strftime(fmt))
            n += 1
            if n - first > MAX_PROJECTED:
                return None
            moment = _add(low, n * step, unit)
        return [v for v in out if _keep(v, mine, lambda x: _parse_value(x, fmt))]
    if kind == "injected":
        eqs = [c for c in mine if c.op in ("eq", "in")]
        if not eqs:
            return None  # Athena itself refuses a query without it
        values = [str(v) for v in (eqs[0].value if eqs[0].op == "in" else [eqs[0].value])]
        return [v for v in values if _keep(v, mine, lambda x: str(x))]
    return None


def _int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _date_window(low: datetime, high: datetime, conditions: Sequence[Condition], fmt: str) -> Tuple[datetime, datetime]:
    """The query's own bounds on a projected date range (so ``dt >= '2026-09-01'`` doesn't walk from 2015): the
    first moment to produce and the last one. The caller keeps the values on the projection's own grid."""
    for c in conditions:
        values = [_parse_value(v, fmt) for v in (c.value if c.op == "in" else [c.value])]
        if not values or any(v is None for v in values):
            continue
        if c.op in ("lt", "lte", "eq", "in"):
            high = min(high, max(values))
        if c.op in ("gt", "gte", "eq", "in"):
            low = max(low, min(values))
    return low, high


def projected_partitions(table: Dict[str, Any], conditions: Sequence[Condition],
                         now: Optional[datetime] = None) -> Optional[List[Tuple[str, Dict[str, str]]]]:
    """
    Athena partition projection: the (location, {key: value}) of every
    partition the conditions allow, computed from the table's
    ``projection.*`` properties and ``storage.location.template`` — no Glue
    call. None when the table has no projection or it can't be computed
    exactly here (an unknown type or date format, an injected key with no
    value, more than ``MAX_PROJECTED`` partitions): the caller then lists
    the whole location.
    """
    params = {k: str(v) for k, v in (table.get("Parameters") or {}).items()}
    if str(params.get("projection.enabled", "")).lower() != "true":
        return None
    keys = [k["Name"].lower() for k in table.get("PartitionKeys") or []]
    if not keys:
        return None
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    per_key: List[List[str]] = []
    for name in keys:
        values = _projected_values(name, params, conditions, now)
        if values is None:
            return None
        per_key.append(values)
    total = 1
    for values in per_key:
        total *= len(values)
    if total > MAX_PROJECTED:
        return None
    location = (table.get("StorageDescriptor") or {}).get("Location") or ""
    template = params.get("storage.location.template")
    out = []
    for combo in product(*per_key):
        values = dict(zip(keys, combo))
        if template:
            loc = template
            for k, v in values.items():
                loc = re.sub(r"\$\{" + re.escape(k) + r"\}", v, loc, flags=re.I)
            out.append((loc if loc.endswith("/") else loc + "/", values))
        else:
            out.append((hive_location(location, values, keys), values))
    return out

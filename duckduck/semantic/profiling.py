"""
Deterministic column profiles for catalog generation — computed with
pandas from the rows generation samples, never by the LLM.

Per column: distinct values, share of empty values, min/max (numbers and
dates — also dates stored as text), the values' shape (IP, email, URL,
domain — ``shape``), a few example values, and — for categorical
columns — every value seen (``values``), which generation merges into
the field's enumerated values.
"""

import re
from typing import Any, Dict, List, Optional

import pandas as pd

#: Semantic types that identify things — never turned into value lists,
#: however few distinct values a sample happens to have.
IDENTIFIER_TYPES = {"ip_address", "user", "email", "host", "domain", "url", "hostname", "id"}
#: Semantic types whose example values are personal data: kept out of the
#: catalog unless ``sample_sensitive``.
SENSITIVE_TYPES = {"user", "email"}

_MAX_TEXT = 60

#: Value shapes recognizable without any model → the semantic type they denote (first match wins).
SHAPES = [
    ("url", re.compile(r"^[a-z][a-z0-9+.-]*://\S+$", re.I)),
    ("email", re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)),
    ("ip_address", re.compile(r"^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$|^(?=.*:.*:)[0-9a-f:]+(/\d{1,3})?$", re.I)),
    ("domain", re.compile(r"^(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}$", re.I)),
]


def profile_frame(df: pd.DataFrame, max_enum_values: int = 20, examples: int = 3) -> Dict[str, Dict[str, Any]]:
    """``{column: {distinct, null_ratio, min?, max?, examples, values?}}`` for a sample."""
    rows = len(df)
    out: Dict[str, Dict[str, Any]] = {}
    for col in df.columns:
        series = df[col]
        empty = series.isna() | (series.astype(str).str.strip() == "")
        present = series[~empty]
        stats: Dict[str, Any] = {
            "distinct": int(present.astype(str).nunique()),
            "null_ratio": round(float(empty.mean()), 3) if rows else None,
            "examples": _examples(present, examples),
        }
        low, high = _range(present)
        if low is not None:
            stats["min"], stats["max"] = low, high
        shape = _shape(present)
        if shape:
            stats["shape"] = shape
        if _categorical(present, stats["distinct"], rows, max_enum_values):
            stats["values"] = sorted(present.astype(str).unique().tolist())
        out[str(col)] = stats
    return out


def _examples(present: pd.Series, n: int) -> List[str]:
    seen: List[str] = []
    for value in present.astype(str):
        text = value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…"
        if text not in seen:
            seen.append(text)
        if len(seen) >= n:
            break
    return seen


def _range(present: pd.Series):
    """(min, max) as text for numbers and dates; ``(None, None)`` otherwise."""
    if present.empty:
        return None, None
    if pd.api.types.is_bool_dtype(present):
        return None, None
    if pd.api.types.is_numeric_dtype(present) or pd.api.types.is_datetime64_any_dtype(present):
        return _text(present.min()), _text(present.max())
    as_text = present.astype(str)
    if as_text.str.match(r"^\d{4}-\d{2}-\d{2}").mean() >= 0.9:  # ISO dates stored as text
        parsed = pd.to_datetime(as_text, errors="coerce", utc=True).dropna()
        if len(parsed) >= 0.9 * len(as_text):
            return _text(parsed.min()), _text(parsed.max())
    return None, None


def _shape(present: pd.Series) -> Optional[str]:
    """The shape nearly every value has (IP, email, URL, domain), if any."""
    if present.empty or pd.api.types.is_numeric_dtype(present) or pd.api.types.is_datetime64_any_dtype(present):
        return None
    values = present.astype(str).str.strip()
    for name, pattern in SHAPES:
        if values.str.match(pattern).mean() >= 0.9:
            return name
    return None


def _categorical(present: pd.Series, distinct: int, rows: int, max_values: int) -> bool:
    """Few values, each repeating: a category column (not a sparse identifier)."""
    if distinct == 0 or distinct > max_values or rows < 20:
        return False
    if pd.api.types.is_numeric_dtype(present) or pd.api.types.is_datetime64_any_dtype(present):
        return False
    return distinct <= len(present) / 4


def _text(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and value != value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)

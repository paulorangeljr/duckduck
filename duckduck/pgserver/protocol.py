"""
The PostgreSQL wire protocol (v3) — the parts a SQL client needs: startup,
password, the simple and extended query flows, and values as text or binary.

Types go out as PostgreSQL's own OIDs, so a client shows a DuckDB DOUBLE as
``float8`` and a TIMESTAMP as ``timestamp``; anything without a PostgreSQL
counterpart (STRUCT, MAP, UNION…) goes out as JSON text.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import json
import re
import struct
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

PROTOCOL_V3 = 196608
SSL_REQUEST = 80877103
GSSENC_REQUEST = 80877104
CANCEL_REQUEST = 80877102

# PostgreSQL type OIDs (pg_type.dat)
BOOL, BYTEA, CHAR, NAME, INT8, INT2, INT2VECTOR, INT4, REGPROC, TEXT, OID = 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26
JSON_, FLOAT4, FLOAT8, UNKNOWN, VARCHAR, BPCHAR = 114, 700, 701, 705, 1043, 1042
DATE, TIME, TIMESTAMP, TIMESTAMPTZ, INTERVAL, TIMETZ, NUMERIC, UUID, JSONB = 1082, 1083, 1114, 1184, 1186, 1266, 1700, 2950, 3802

#: element type → its array type
ARRAY_OF = {BOOL: 1000, BYTEA: 1001, CHAR: 1002, NAME: 1003, INT2: 1005, INT4: 1007, TEXT: 1009, VARCHAR: 1015,
            INT8: 1016, FLOAT4: 1021, FLOAT8: 1022, OID: 1028, TIMESTAMP: 1115, DATE: 1182, TIME: 1183,
            TIMESTAMPTZ: 1185, INTERVAL: 1187, NUMERIC: 1231, UUID: 2951, JSON_: 199, JSONB: 3807}
ELEMENT_OF = {v: k for k, v in ARRAY_OF.items()}

#: oid → (typname, typlen, typcategory) — what pg_type lists
TYPES: Dict[int, Tuple[str, int, str]] = {
    BOOL: ("bool", 1, "B"), BYTEA: ("bytea", -1, "U"), CHAR: ("char", 1, "S"), NAME: ("name", 64, "S"),
    INT8: ("int8", 8, "N"), INT2: ("int2", 2, "N"), INT2VECTOR: ("int2vector", -1, "A"), INT4: ("int4", 4, "N"),
    REGPROC: ("regproc", 4, "N"), TEXT: ("text", -1, "S"), OID: ("oid", 4, "N"), JSON_: ("json", -1, "U"),
    FLOAT4: ("float4", 4, "N"), FLOAT8: ("float8", 8, "N"), UNKNOWN: ("unknown", -2, "X"),
    BPCHAR: ("bpchar", -1, "S"), VARCHAR: ("varchar", -1, "S"), DATE: ("date", 4, "D"), TIME: ("time", 8, "D"),
    TIMESTAMP: ("timestamp", 8, "D"), TIMESTAMPTZ: ("timestamptz", 8, "D"), INTERVAL: ("interval", 16, "T"),
    TIMETZ: ("timetz", 12, "D"), NUMERIC: ("numeric", -1, "N"), UUID: ("uuid", 16, "U"), JSONB: ("jsonb", -1, "U"),
    1033: ("aclitem", 12, "U"), 30: ("oidvector", -1, "A"), 2205: ("regclass", 4, "N"), 2206: ("regtype", 4, "N"),
}
for _elem, _arr in ARRAY_OF.items():
    TYPES[_arr] = ("_" + TYPES[_elem][0], -1, "A")

#: how format_type() names them (information_schema.columns.data_type too)
FORMAT_NAMES = {BOOL: "boolean", INT2: "smallint", INT4: "integer", INT8: "bigint", FLOAT4: "real",
                FLOAT8: "double precision", VARCHAR: "character varying", BPCHAR: "character",
                TIMESTAMP: "timestamp without time zone", TIMESTAMPTZ: "timestamp with time zone",
                TIME: "time without time zone", TIMETZ: "time with time zone", CHAR: '"char"'}

_SIMPLE_TYPES = {
    "BOOLEAN": BOOL, "TINYINT": INT2, "SMALLINT": INT2, "INTEGER": INT4, "BIGINT": INT8,
    "UTINYINT": INT2, "USMALLINT": INT4, "UINTEGER": OID, "UBIGINT": NUMERIC, "HUGEINT": NUMERIC,
    "UHUGEINT": NUMERIC, "FLOAT": FLOAT4, "DOUBLE": FLOAT8, "VARCHAR": TEXT, "BLOB": BYTEA,
    "DATE": DATE, "TIME": TIME, "TIMESTAMP": TIMESTAMP, "TIMESTAMP_S": TIMESTAMP, "TIMESTAMP_MS": TIMESTAMP,
    "TIMESTAMP_NS": TIMESTAMP, "TIMESTAMP WITH TIME ZONE": TIMESTAMPTZ, "TIME WITH TIME ZONE": TIMETZ,
    "INTERVAL": INTERVAL, "UUID": UUID, "JSON": JSON_, "BIT": TEXT, "VARINT": NUMERIC, "BIGNUM": NUMERIC,
}


def oid_of(duck_type: Any) -> int:
    """A DuckDB type (``BIGINT``, ``DECIMAL(18,3)``, ``VARCHAR[]``, ``STRUCT(...)``) → the PostgreSQL OID it goes out as."""
    name = str(duck_type).upper().strip()
    if name.endswith("[]") or re.search(r"\[\d+\]$", name):
        element = oid_of(re.sub(r"\[\d*\]$", "", name))
        return JSON_ if element in (JSON_,) or element in ELEMENT_OF else ARRAY_OF.get(element, JSON_)
    if name.startswith("DECIMAL") or name.startswith("NUMERIC"):
        return NUMERIC
    if name.startswith(("STRUCT", "MAP", "UNION")):
        return JSON_
    if name.startswith("ENUM"):
        return TEXT
    return _SIMPLE_TYPES.get(name, TEXT)


def typmod_of(duck_type: Any) -> int:
    """DECIMAL(p,s) → PostgreSQL's numeric typmod; -1 otherwise."""
    m = re.match(r"(?:DECIMAL|NUMERIC)\((\d+),\s*(\d+)\)", str(duck_type).upper())
    return ((int(m.group(1)) << 16) | int(m.group(2))) + 4 if m else -1


def format_type(oid: int, typmod: int = -1) -> str:
    """``format_type(oid, typmod)``: ``integer``, ``numeric(18,3)``, ``text[]``."""
    if oid in ELEMENT_OF:
        return format_type(ELEMENT_OF[oid], typmod) + "[]"
    if oid == NUMERIC and typmod is not None and typmod >= 4:
        return f"numeric({(typmod - 4) >> 16},{(typmod - 4) & 0xFFFF})"
    return FORMAT_NAMES.get(oid) or TYPES.get(oid, ("text",))[0]


# ---------------------------------------------------------------------------
# values → text / binary
# ---------------------------------------------------------------------------

def _json_default(v: Any) -> Any:
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return "\\x" + bytes(v).hex()
    if isinstance(v, _dt.timedelta):
        return _interval_text(v)
    if hasattr(v, "tolist"):
        return v.tolist()
    return str(v)


def _interval_text(v: _dt.timedelta) -> str:
    days, secs, micros = v.days, v.seconds, v.microseconds
    hh, rest = divmod(secs, 3600)
    mm, ss = divmod(rest, 60)
    clock = f"{hh:02d}:{mm:02d}:{ss:02d}" + (f".{micros:06d}" if micros else "")
    return (f"{days} day{'s' if abs(days) != 1 else ''} " if days else "") + clock


def _array_item(v: Any, element: int) -> str:
    if v is None:
        return "NULL"
    text = to_text(v, element)
    if text == "" or re.search(r'[{},"\\\s]', text) or text.upper() == "NULL":
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def to_text(v: Any, oid: int) -> str:
    """A value as PostgreSQL writes it in text format."""
    if isinstance(v, bool):
        return "t" if v else "f"
    if oid in ELEMENT_OF and isinstance(v, (list, tuple)):
        return "{" + ",".join(_array_item(x, ELEMENT_OF[oid]) for x in v) + "}"
    if oid == INT2VECTOR and isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v)
    if isinstance(v, float):
        if v != v:
            return "NaN"
        if v in (float("inf"), float("-inf")):
            return "Infinity" if v > 0 else "-Infinity"
        return repr(v)
    if isinstance(v, _dt.datetime):
        text = v.isoformat(sep=" ")
        if v.tzinfo is not None and text.endswith("+00:00"):
            text = text[:-6] + "+00"
        return text
    if isinstance(v, (_dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, _dt.timedelta):
        return _interval_text(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(v).hex()
    if isinstance(v, (dict, list, tuple)) or hasattr(v, "tolist"):
        return json.dumps(v.tolist() if hasattr(v, "tolist") else v, default=_json_default, ensure_ascii=False)
    return str(v)


_PG_EPOCH = _dt.datetime(2000, 1, 1)
_PG_EPOCH_DATE = _dt.date(2000, 1, 1)


def _numeric_binary(v: Any) -> bytes:
    d = decimal.Decimal(v)
    if d.is_nan():
        return struct.pack("!hhHh", 0, 0, 0xC000, 0)
    sign, digits, exp = d.as_tuple()
    text = "".join(map(str, digits))
    dscale = max(0, -exp)
    if exp > 0:
        text += "0" * exp
        exp = 0
    if len(text) < -exp:  # 0.001: more decimal places than digits
        text = "0" * (-exp - len(text)) + text
    int_part = text[: len(text) + exp] if exp else text
    frac_part = text[len(text) + exp:] if exp else ""
    int_part = int_part.lstrip("0")
    int_part = "0" * (-len(int_part) % 4) + int_part
    frac_part = frac_part + "0" * (-len(frac_part) % 4)
    groups = [int(int_part[i:i + 4]) for i in range(0, len(int_part), 4)] + \
             [int(frac_part[i:i + 4]) for i in range(0, len(frac_part), 4)]
    weight = len(int_part) // 4 - 1
    while groups and groups[0] == 0:
        groups.pop(0)
        weight -= 1
    while groups and groups[-1] == 0:
        groups.pop()
    if not groups:
        weight = 0
    return struct.pack(f"!hhHh{len(groups)}H", len(groups), weight, 0x4000 if sign else 0, dscale, *groups)


def to_binary(v: Any, oid: int) -> bytes:
    """A value in PostgreSQL's binary format; types without one fall back to their text bytes."""
    if oid in ELEMENT_OF and isinstance(v, (list, tuple)) or (oid in ELEMENT_OF and hasattr(v, "tolist")):
        items = v.tolist() if hasattr(v, "tolist") else list(v)
        element = ELEMENT_OF[oid]
        head = struct.pack("!iiiii", 1, int(any(x is None for x in items)), element, len(items), 1)
        body = b"".join(struct.pack("!i", -1) if x is None else
                        (lambda raw: struct.pack("!i", len(raw)) + raw)(to_binary(x, element)) for x in items)
        return head + body
    if oid == BOOL:
        return b"\x01" if v else b"\x00"
    if oid == INT2:
        return struct.pack("!h", int(v))
    if oid in (INT4, OID, REGPROC):
        return struct.pack("!i" if oid == INT4 else "!I", int(v))
    if oid == INT8:
        return struct.pack("!q", int(v))
    if oid == FLOAT4:
        return struct.pack("!f", float(v))
    if oid == FLOAT8:
        return struct.pack("!d", float(v))
    if oid == NUMERIC:
        return _numeric_binary(v)
    if oid == BYTEA and isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    if oid == UUID:
        return (v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))).bytes
    if oid == DATE and isinstance(v, _dt.date):
        return struct.pack("!i", (v - _PG_EPOCH_DATE).days)
    if oid in (TIMESTAMP, TIMESTAMPTZ) and isinstance(v, _dt.datetime):
        base = v.astimezone(_dt.timezone.utc).replace(tzinfo=None) if v.tzinfo else v
        delta = base - _PG_EPOCH
        return struct.pack("!q", (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds)
    if oid == TIME and isinstance(v, _dt.time):
        return struct.pack("!q", ((v.hour * 60 + v.minute) * 60 + v.second) * 1_000_000 + v.microsecond)
    if oid == INTERVAL and isinstance(v, _dt.timedelta):
        return struct.pack("!qii", v.seconds * 1_000_000 + v.microseconds, v.days, 0)
    if oid == JSONB:
        return b"\x01" + to_text(v, oid).encode()
    return to_text(v, oid).encode("utf-8")


def parse_array(text: str) -> List[Optional[str]]:
    """PostgreSQL's array text ``{a,"b c",NULL}`` → ``["a", "b c", None]`` (one dimension; nested ones as text)."""
    body = text.strip()
    if not (body.startswith("{") and body.endswith("}")):
        raise ValueError(f"not an array literal: {text[:40]!r}")
    body = body[1:-1]
    items: List[Optional[str]] = []
    i, n = 0, len(body)
    while i < n:
        while i < n and body[i] in " \t\n":
            i += 1
        if i >= n:
            break
        if body[i] == '"':
            i += 1
            out = []
            while i < n and body[i] != '"':
                if body[i] == "\\" and i + 1 < n:
                    i += 1
                out.append(body[i])
                i += 1
            i += 1  # the closing quote
            items.append("".join(out))
            while i < n and body[i] != ",":
                i += 1
        else:
            depth, start = 0, i
            while i < n and (body[i] != "," or depth):
                depth += body[i] == "{"
                depth -= body[i] == "}"
                i += 1
            word = body[start:i].strip()
            items.append(None if word.upper() == "NULL" else word)
        i += 1  # the comma
    return items


def _binary_array(raw: bytes) -> List[Any]:
    ndim, _, element = struct.unpack_from("!iii", raw, 0)
    if ndim == 0:
        return []
    pos = 12
    count = 1
    for _ in range(ndim):
        size, _lower = struct.unpack_from("!ii", raw, pos)
        count *= size
        pos += 8
    out = []
    for _ in range(count):
        size = struct.unpack_from("!i", raw, pos)[0]
        pos += 4
        if size < 0:
            out.append(None)
        else:
            out.append(decode_param(raw[pos:pos + size], 1, element))
            pos += size
    return out


def decode_param(raw: Optional[bytes], fmt: int, oid: int) -> Any:
    """A Bind parameter → a Python value (None for NULL; a list for an array)."""
    if raw is None:
        return None
    if oid in ELEMENT_OF:
        return _binary_array(raw) if fmt == 1 else parse_array(raw.decode("utf-8"))
    if fmt == 0:
        return raw.decode("utf-8")
    if oid == BOOL:
        return raw != b"\x00"
    if oid == INT2:
        return struct.unpack("!h", raw)[0]
    if oid in (INT4, OID):
        return struct.unpack("!i" if oid == INT4 else "!I", raw)[0]
    if oid == INT8:
        return struct.unpack("!q", raw)[0]
    if oid == FLOAT4:
        return struct.unpack("!f", raw)[0]
    if oid == FLOAT8:
        return struct.unpack("!d", raw)[0]
    if oid == BYTEA:
        return bytes(raw)
    if oid == UUID and len(raw) == 16:
        return str(uuid.UUID(bytes=raw))
    if oid == DATE and len(raw) == 4:
        return (_PG_EPOCH_DATE + _dt.timedelta(days=struct.unpack("!i", raw)[0])).isoformat()
    if oid in (TIMESTAMP, TIMESTAMPTZ) and len(raw) == 8:
        return (_PG_EPOCH + _dt.timedelta(microseconds=struct.unpack("!q", raw)[0])).isoformat(sep=" ")
    if oid == JSONB and raw[:1] == b"\x01":
        return raw[1:].decode("utf-8")
    return raw.decode("utf-8", errors="replace")


def sql_literal(value: Any, oid: int = 0) -> str:
    """A parameter written into the query: numbers bare (when they are numbers), everything else quoted."""
    if value is None:
        return "NULL"
    if isinstance(value, (list, tuple)):
        element = ELEMENT_OF.get(oid, 0)
        return "[" + ", ".join(sql_literal(v, element) for v in value) + "]"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, bytes):
        return "'\\x" + value.hex() + "'::BLOB"
    text = str(value)
    if oid in (INT2, INT4, INT8, OID, FLOAT4, FLOAT8, NUMERIC) and re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text.strip()):
        return text.strip()
    if oid == BOOL and text.lower() in ("t", "true", "f", "false", "1", "0", "yes", "no", "on", "off"):
        return "TRUE" if text.lower() in ("t", "true", "1", "yes", "on") else "FALSE"
    return "'" + text.replace("'", "''") + "'"


_DOLLAR = re.compile(r"\$(\d+)")
_IN_ANY = re.compile(r"\b(?:ANY|ALL|SOME)\s*\(\s*$", re.I)
_ARRAY_AFTER = re.compile(r"\s*::\s*(?:\w+|\"char\")\s*\[\s*\]")


def bind_params(query: str, values: Sequence[Any], oids: Sequence[int], masked: Callable[[str], str]) -> str:
    """``$1``, ``$2``… outside strings and comments → the values, written as SQL literals."""
    if not values:
        return query
    hidden = masked(query)
    out, last = [], 0
    for m in _DOLLAR.finditer(hidden):
        n = int(m.group(1))
        if not 1 <= n <= len(values):
            continue
        value, oid = values[n - 1], oids[n - 1] if n - 1 < len(oids) else 0
        if (not oid and isinstance(value, str) and value.startswith("{") and value.endswith("}")
                and (_IN_ANY.search(hidden[:m.start()]) or _ARRAY_AFTER.match(hidden[m.end():]))):
            try:  # an untyped '{a,b}' where an array goes (psycopg sends lists this way): the array
                value = parse_array(value)
            except ValueError:
                pass
        out.append(query[last:m.start()])
        out.append(sql_literal(value, oid))
        last = m.end()
    out.append(query[last:])
    return "".join(out)


_ARRAY_CAST = re.compile(r"'[^']*'(\s*::\s*(?:pg_catalog\s*\.\s*)?(?:\w+|\"char\")\s*\[\s*\])")


def array_literals(query: str, masked: Callable[[str], str]) -> str:
    """``'{a,b}'::text[]`` (PostgreSQL's array text, as pgjdbc's getSQLKeywords sends it) → ``['a', 'b']::text[]``."""
    if "{" not in query:
        return query
    hidden = masked(query)  # strings blanked (same length): only real casts of real literals match
    out, last = [], 0
    for m in _ARRAY_CAST.finditer(hidden):
        cast_at = m.start(1)
        literal = query[m.start():cast_at].rstrip()
        text = literal[1:-1].replace("''", "'")
        if not text.lstrip().startswith("{"):
            continue
        try:
            items = parse_array(text)
        except ValueError:
            continue
        out.append(query[last:m.start()])
        out.append("[" + ", ".join(sql_literal(x) for x in items) + "]" + query[cast_at:m.end()])
        last = m.end()
    out.append(query[last:])
    return "".join(out)


def count_params(query: str, masked: Callable[[str], str]) -> int:
    return max((int(n) for n in _DOLLAR.findall(masked(query))), default=0)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def message(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack("!i", len(payload) + 4) + payload


def cstr(text: str) -> bytes:
    return text.encode("utf-8") + b"\x00"


def authentication(code: int, extra: bytes = b"") -> bytes:
    return message(b"R", struct.pack("!i", code) + extra)


def parameter_status(name: str, value: str) -> bytes:
    return message(b"S", cstr(name) + cstr(value))


def backend_key(pid: int, secret: int) -> bytes:
    return message(b"K", struct.pack("!ii", pid, secret))


def ready(status: str = "I") -> bytes:
    return message(b"Z", status.encode())


def row_description(fields: List[Tuple[str, int, int]], formats: Sequence[int] = ()) -> bytes:
    """``fields``: (name, type oid, typmod); ``formats``: per column 0 text / 1 binary (one entry = all)."""
    parts = [struct.pack("!h", len(fields))]
    for i, (name, oid, typmod) in enumerate(fields):
        fmt = _format_at(formats, i)
        size = TYPES.get(oid, ("", -1, ""))[1]
        parts.append(cstr(name) + struct.pack("!ihihih", 0, 0, oid, size if size > 0 else -1, typmod, fmt))
    return message(b"T", b"".join(parts))


def _format_at(formats: Sequence[int], i: int) -> int:
    if not formats:
        return 0
    return formats[0] if len(formats) == 1 else (formats[i] if i < len(formats) else 0)


def data_row(values: Sequence[Any], oids: Sequence[int], formats: Sequence[int] = ()) -> bytes:
    parts = [struct.pack("!h", len(values))]
    for i, v in enumerate(values):
        if v is None:
            parts.append(struct.pack("!i", -1))
            continue
        raw = to_binary(v, oids[i]) if _format_at(formats, i) == 1 else to_text(v, oids[i]).encode("utf-8")
        parts.append(struct.pack("!i", len(raw)) + raw)
    return message(b"D", b"".join(parts))


def command_complete(tag: str) -> bytes:
    return message(b"C", cstr(tag))


def error(text: str, code: str = "XX000", severity: str = "ERROR", hint: Optional[str] = None) -> bytes:
    fields = b"S" + cstr(severity) + b"V" + cstr(severity) + b"C" + cstr(code) + b"M" + cstr(text)
    if hint:
        fields += b"H" + cstr(hint)
    return message(b"E", fields + b"\x00")


def notice(text: str) -> bytes:
    return message(b"N", b"S" + cstr("NOTICE") + b"V" + cstr("NOTICE") + b"C" + cstr("00000") + b"M" + cstr(text) + b"\x00")


PARSE_COMPLETE = message(b"1")
BIND_COMPLETE = message(b"2")
CLOSE_COMPLETE = message(b"3")
NO_DATA = message(b"n")
EMPTY_QUERY = message(b"I")
PORTAL_SUSPENDED = message(b"s")


def parameter_description(oids: Sequence[int]) -> bytes:
    return message(b"t", struct.pack(f"!h{len(oids)}i", len(oids), *oids))


class Reader:
    """Reads a message body field by field."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def int16(self) -> int:
        v = struct.unpack_from("!h", self.data, self.pos)[0]
        self.pos += 2
        return v

    def int32(self) -> int:
        v = struct.unpack_from("!i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def cstr(self) -> str:
        end = self.data.index(b"\x00", self.pos)
        text = self.data[self.pos:end].decode("utf-8", errors="replace")
        self.pos = end + 1
        return text

    def bytes(self, n: int) -> bytes:
        v = self.data[self.pos:self.pos + n]
        self.pos += n
        return v

    def byte(self) -> str:
        v = self.data[self.pos:self.pos + 1].decode()
        self.pos += 1
        return v

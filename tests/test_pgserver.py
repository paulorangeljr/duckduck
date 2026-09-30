"""duckduck's tables over the PostgreSQL wire protocol (``duckduck.pgserver``): what SQL clients see and can do.

A tiny client written here (``Wire``: plain sockets) covers the protocol itself and cancelling; psycopg, when
installed, plays a real client (the extended protocol, binary values, prepared statements, TLS)."""

import datetime
import decimal
import json
import os
import socket
import struct
import subprocess
import threading
import time

import pandas as pd
import pytest

from duckduck import DuckAPI, progress
from duckduck.kinds import catalog
from duckduck.pgserver import PGServer
from duckduck.pgserver import catalog as pgcat
from duckduck.pgserver import protocol as pg


class Glue:
    def __init__(self):
        self.calls = []

    @catalog(lists="table")
    def tables(self, limit=None):
        return pd.DataFrame([{"database": "sec", "table_name": "proxy logs"}])

    def table(self, database: str, table_name: str, where=None, limit=None):
        self.calls.append({"database": database, "table_name": table_name, "where": where, "limit": limit})
        return pd.DataFrame({"host": ["a", "b"], "bytes": [5, 20], "at": pd.to_datetime(["2026-01-01", "2026-01-02"])})


def _duck():
    duck, glue = DuckAPI(), Glue()
    calls = {"cves": 0}

    def cves(severity=None, limit=None):
        """CVEs from the NVD."""
        calls["cves"] += 1
        df = pd.DataFrame({"id": ["CVE-1", "CVE-2"], "severity": ["HIGH", "LOW"], "score": [7.5, 2.0]})
        return df[df["severity"] == severity] if severity else df

    def slow(limit=None):
        progress.wait(20)
        return pd.DataFrame({"x": [1]})

    for name, fn, service in [("s3_data_tables", glue.tables, "s3_data"), ("s3_data_table", glue.table, "s3_data"),
                              ("nvd_cves", cves, "nvd"), ("nvd_slow", slow, "nvd")]:
        duck.register_api_function(name, fn)
        duck.service_of[name] = service
    duck.service_prefix.update({"s3_data": "s3_data", "nvd": "nvd"})
    duck.register_view("s3_data.sec.proxy_logs", {"table": "s3_data_table",
                                                   "args": {"database": "sec", "table_name": "proxy logs"},
                                                   "description": "Proxy logs"})
    duck.register_view("high_cves", {"sql": "SELECT id FROM nvd.cves WHERE severity = 'HIGH'",
                                     "description": "The high ones"})
    return duck, glue, calls


@pytest.fixture
def served(tmp_path):
    duck, glue, calls = _duck()
    server = PGServer(duck, port=0, columns_file=str(tmp_path / "columns.json")).start()
    yield server, duck, glue, calls
    server.shutdown()


class Wire:
    """Just enough of a PostgreSQL client: startup, simple queries, cancel."""

    def __init__(self, port, user="me", password=None):
        self.port = port
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        body = struct.pack("!i", pg.PROTOCOL_V3) + b"user\x00" + user.encode() + b"\x00database\x00duckduck\x00\x00"
        self.sock.sendall(struct.pack("!i", len(body) + 4) + body)
        self.params, self.key = {}, None
        while True:
            kind, data = self.read()
            if kind == "R" and struct.unpack("!i", data[:4])[0] == 3:
                pw = (password or "").encode() + b"\x00"
                self.sock.sendall(b"p" + struct.pack("!i", len(pw) + 4) + pw)
            elif kind == "S":
                name, value = data[:-1].split(b"\x00", 1)
                self.params[name.decode()] = value.decode()
            elif kind == "K":
                self.key = struct.unpack("!ii", data)
            elif kind == "E":
                raise ConnectionError(self.error_text(data))
            elif kind == "Z":
                return

    def read(self):
        head = b""
        while len(head) < 5:
            chunk = self.sock.recv(5 - len(head))
            if not chunk:
                raise ConnectionError("closed")
            head += chunk
        n = struct.unpack("!i", head[1:])[0] - 4
        data = b""
        while len(data) < n:
            data += self.sock.recv(n - len(data))
        return head[:1].decode(), data

    @staticmethod
    def error_text(data):
        fields = dict((f[:1].decode(), f[1:].decode()) for f in data.split(b"\x00") if f)
        return f"{fields.get('C')}: {fields.get('M')}"

    def query(self, sql):
        """(columns, rows, tags, error) of a simple query."""
        body = sql.encode() + b"\x00"
        self.sock.sendall(b"Q" + struct.pack("!i", len(body) + 4) + body)
        columns, rows, tags, error = [], [], [], None
        while True:
            kind, data = self.read()
            if kind == "T":
                r = pg.Reader(data)
                columns = []
                for _ in range(r.int16()):
                    name = r.cstr()
                    r.int32(), r.int16()
                    oid = r.int32()
                    r.int16(), r.int32(), r.int16()
                    columns.append((name, oid))
            elif kind == "D":
                r = pg.Reader(data)
                row = []
                for _ in range(r.int16()):
                    n = r.int32()
                    row.append(None if n < 0 else r.bytes(n).decode())
                rows.append(row)
            elif kind == "C":
                tags.append(data.rstrip(b"\x00").decode())
            elif kind == "E":
                error = self.error_text(data)
            elif kind == "Z":
                return columns, rows, tags, error

    def cancel(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(struct.pack("!iiii", 16, pg.CANCEL_REQUEST, *self.key))
        s.close()

    def close(self):
        self.sock.sendall(b"X\x00\x00\x00\x04")
        self.sock.close()


# -- what a client sees ---------------------------------------------------------------------------------------


def test_connectors_are_schemas_and_every_table_without_arguments_is_listed(served):
    server = served[0]
    w = Wire(server.port)
    assert w.params["server_version"] == pgcat.SERVER_VERSION and w.params["client_encoding"] == "UTF8"
    _, rows, _, error = w.query("SELECT n.nspname, c.relname, c.relkind FROM pg_catalog.pg_class c "
                                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                                "WHERE n.nspname <> 'pg_catalog' ORDER BY 1, 2")
    assert error is None
    assert ["s3_data.sec", "proxy_logs", "r"] in rows  # a saved table named by its address
    assert ["nvd", "cves", "r"] in rows and ["s3_data", "tables", "r"] in rows
    assert ["public", "high_cves", "v"] in rows  # a saved query: a view, with its SQL
    assert not any(r[1] == "table" for r in rows)  # a table function needs arguments: not a table
    _, rows, _, _ = w.query("SELECT definition FROM pg_catalog.pg_views WHERE viewname = 'high_cves'")
    assert rows == [["SELECT id FROM nvd.cves WHERE severity = 'HIGH'"]]
    _, rows, _, _ = w.query("SELECT obj_description('nvd.cves'::regclass, 'pg_class')")
    assert rows == [["CVEs from the NVD."]]
    w.close()


def test_arrays_are_found_the_way_npgsql_and_jdbc_look_for_them(served):
    w = Wire(served[0].port)
    # Npgsql (Power BI): an array type is one whose receive function is array_recv
    _, rows, _, error = w.query("SELECT t.typname, p.proname, t.typelem FROM pg_type t "
                                "LEFT JOIN pg_proc p ON p.oid = t.typreceive WHERE t.oid IN (23, 1007) ORDER BY t.oid")
    assert error is None and rows == [["int4", "int4_recv", "0"], ["_int4", "array_recv", "23"]]
    # pgjdbc
    _, rows, _, _ = w.query("SELECT typinput='pg_catalog.array_in'::regproc FROM pg_catalog.pg_type WHERE oid = 1007")
    assert rows == [["t"]]
    w.close()


def test_columns_are_learned_from_the_first_read_and_kept(served, tmp_path):
    server = served[0]
    server.learn_at_most = 0  # not read on request: the placeholder until a query reads the table
    w = Wire(server.port)
    sql = ("SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
           "JOIN pg_class c ON c.oid = a.attrelid WHERE c.relname = 'cves' ORDER BY a.attnum")
    assert w.query(sql)[1] == [[pgcat.UNKNOWN_COLUMN, "text"]]
    w.query("SELECT * FROM nvd.cves")
    assert w.query(sql)[1] == [["id", "text"], ["severity", "text"], ["score", "double precision"]]
    kept = json.loads((tmp_path / "columns.json").read_text())["tables"]
    assert kept["nvd_cves"][2] == ["score", "DOUBLE"]
    again = PGServer(server.source, port=0, columns_file=str(tmp_path / "columns.json"))
    assert again.columns.get("nvd_cves")[0] == ("id", "VARCHAR")  # a restart still knows them
    w.close()


def test_a_saved_table_added_later_shows_up_without_reconnecting(served):
    server, duck = served[0], served[1]
    w = Wire(server.port)
    assert w.query("SELECT count(*) FROM pg_class WHERE relname = 'recent'")[1] == [["0"]]
    duck.register_view("nvd.recent", {"sql": "SELECT id FROM nvd.cves"})
    assert w.query("SELECT count(*) FROM pg_class WHERE relname = 'recent'")[1] == [["1"]]
    w.close()


# -- queries ------------------------------------------------------------------------------------------------------


def test_the_way_clients_write_names_reaches_the_table_with_push_down(served):
    server, _, glue, _ = served
    w = Wire(server.port)
    # DBeaver quotes a schema with a dot in it
    cols, rows, tags, error = w.query('SELECT host, bytes FROM "s3_data.sec"."proxy_logs" WHERE bytes > 10')
    assert error is None and rows == [["b", "20"]] and tags == ["SELECT 1"]
    assert [(c.column, c.op, c.value) for c in glue.calls[-1]["where"]] == [("bytes", "gt", 10)]
    assert glue.calls[-1]["database"] == "sec" and glue.calls[-1]["table_name"] == "proxy logs"
    assert [c[1] for c in cols] == [pg.TEXT, pg.INT8]
    assert w.query('SELECT * FROM "public"."high_cves"')[1] == [["CVE-1"]]
    assert w.query("SELECT * FROM public.high_cves")[1] == [["CVE-1"]]
    # a table function by address: not listed, still queryable
    assert w.query("SELECT count(*) AS n FROM s3_data.sec.other")[1] == [["2"]]
    w.close()


def test_read_only_and_no_files(served):
    w = Wire(served[0].port)
    assert w.query("CREATE TABLE x (a INT)")[3].startswith("25006:")
    assert w.query("DELETE FROM nvd.cves")[3].startswith("25006:")
    error = w.query("SELECT * FROM read_csv('/etc/passwd')")[3]
    assert error and "disabled" in error
    assert w.query("SELECT 1; SET enable_external_access = true; SELECT 2")[2] == ["SELECT 1", "SET", "SELECT 1"]
    assert "disabled" in w.query("SELECT * FROM read_csv('/etc/passwd')")[3]  # SET never reaches DuckDB
    w.close()


def test_what_clients_send_besides_queries(served):
    w = Wire(served[0].port)
    assert w.query("SET extra_float_digits = 3")[2] == ["SET"]
    assert w.query("SET application_name = 'tool'")[2] == ["SET"]
    assert w.query("SHOW application_name")[1] == [["tool"]]
    assert w.query("SHOW transaction isolation level")[1] == [["read committed"]]
    assert w.query("SHOW search_path")[1] == [['"$user", public']]
    assert w.query("SHOW nope")[3].startswith("42704:")
    assert w.query("BEGIN")[2] == ["BEGIN"]
    assert w.query("SELECT * FROM nope")[3].startswith("42P01:")
    assert w.query("SELECT 1")[3].startswith("25P02:")  # failed transaction until ROLLBACK
    assert w.query("ROLLBACK")[2] == ["ROLLBACK"]
    assert w.query("SELECT 1 AS one")[1] == [["1"]]
    cols, rows, _, _ = w.query("SELECT version(), current_user, current_schema(), count(*) FROM nvd.cves")
    assert [c[0] for c in cols] == ["version", "current_user", "current_schema", "count"]
    assert rows[0][1:] == ["me", "public", "2"] and rows[0][0].startswith("PostgreSQL ")
    assert len(w.query("SHOW TABLES")[1]) >= 4  # duckduck's own list
    w.close()


def test_the_source_cache_is_shared_and_temp_tables_go_away(served):
    server, _, _, calls = served
    w = Wire(server.port)
    w.query("SELECT * FROM nvd.cves")
    w.query("SELECT id FROM nvd.cves ORDER BY id DESC")
    assert calls["cves"] == 1
    other = Wire(server.port)
    other.query("SELECT count(*) FROM nvd.cves")
    assert calls["cves"] == 1  # another client, the same read
    assert other.query("SELECT count(*) FROM duckdb_views() WHERE temporary AND starts_with(view_name, '_api_')")[1] \
        == [["0"]]
    w.close()
    other.close()


def test_cancel_stops_the_running_query(served):
    w = Wire(served[0].port)
    threading.Timer(0.5, w.cancel).start()
    started = time.time()
    _, _, _, error = w.query("SELECT * FROM nvd.slow")
    assert error.startswith("57014:") and time.time() - started < 5
    assert w.query("SELECT 1")[1] == [["1"]]  # the connection keeps working
    w.close()


# -- sign-in ----------------------------------------------------------------------------------------------------


def test_passwords_and_where_it_may_listen():
    duck, _, _ = _duck()
    server = PGServer(duck, port=0, users={"alice": "s3cret"}).start()
    try:
        with pytest.raises(ConnectionError, match="28P01"):
            Wire(server.port, "alice", "wrong")
        with pytest.raises(ConnectionError, match="28P01"):
            Wire(server.port, "bob", "s3cret")
        Wire(server.port, "alice", "s3cret").close()
    finally:
        server.shutdown()
    with pytest.raises(ValueError, match="needs a password"):
        PGServer(duck, host="0.0.0.0", port=0)


def test_config_section(tmp_path, monkeypatch):
    from duckduck.pgserver.server import config_problems

    assert config_problems({"enabled": True, "port": 5433}) == []
    problems = " ".join(config_problems({"prot": 1, "ssl_required": True, "host": "0.0.0.0"}))
    assert "prot" in problems and "needs pg_server.tls" in problems and "reachable from other machines" in problems
    path = tmp_path / "duckduck.json"
    path.write_text(json.dumps({"services": {}, "pg_server": {
        "port": 0, "database": "lake", "columns_file": "cols.json",
        "authentication": {"type": "local", "carol": "pw"}}}))
    duck, _, _ = _duck()
    server = PGServer.from_config(duck, str(path))
    assert server.database == "lake" and server.columns.path == str(tmp_path / "cols.json")
    assert server.password_check("carol", "pw") and not server.password_check("carol", "no")
    monkeypatch.setenv("DUCKDUCK_PG_PASSWORD", "envpw")
    path.write_text(json.dumps({"services": {}, "pg_server": {"port": 0}}))
    assert PGServer.from_config(duck, str(path)).password_check("anyone", "envpw")


# -- a real client --------------------------------------------------------------------------------------------------


def test_psycopg_extended_protocol_binary_and_prepared(served):
    psycopg = pytest.importorskip("psycopg")
    server, _, glue, _ = served
    with psycopg.connect(f"host=127.0.0.1 port={server.port} dbname=duckduck user=me", autocommit=True) as c:
        rows = c.execute('SELECT host FROM "s3_data.sec".proxy_logs WHERE bytes > %s', [10]).fetchall()
        assert rows == [("b",)] and glue.calls[-1]["where"][0].value == 10
        values = ("SELECT 1::int, 2::bigint, 2.5::double, 1.25::decimal(5,2), true, date '2026-01-02', "
                  "timestamp '2026-01-02 03:04:05', 'txt', ['a', NULL], {'k': 1}, NULL")
        expected = (1, 2, 2.5, decimal.Decimal("1.25"), True, datetime.date(2026, 1, 2),
                    datetime.datetime(2026, 1, 2, 3, 4, 5), "txt", ["a", None], {"k": 1}, None)
        assert c.execute(values).fetchone() == expected
        with c.cursor(binary=True) as b:
            assert b.execute(values).fetchone()[:9] == expected[:9]
        for severity in ["HIGH", "LOW", "HIGH"]:
            c.execute("SELECT id FROM nvd.cves WHERE severity = %s", [severity], prepare=True)
        with pytest.raises(psycopg.errors.UndefinedTable):
            c.execute("SELECT * FROM nope")
        assert c.execute("SELECT 42").fetchone() == (42,)


def test_psycopg_over_tls(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / "k.pem"),
                        "-out", str(tmp_path / "c.pem"), "-days", "1", "-subj", "/CN=localhost"],
                       check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("openssl isn't available to make a test certificate")
    duck, _, _ = _duck()
    server = PGServer(duck, port=0, users={"alice": "pw"}, tls=(str(tmp_path / "c.pem"), str(tmp_path / "k.pem")),
                      ssl_required=True).start()
    try:
        dsn = f"host=127.0.0.1 port={server.port} dbname=duckduck user=alice password=pw"
        with psycopg.connect(dsn + " sslmode=require") as c:
            assert c.info.pgconn.ssl_in_use and c.execute("SELECT count(*) FROM nvd.cves").fetchone() == (2,)
        with pytest.raises(psycopg.OperationalError, match="encrypted"):
            psycopg.connect(dsn + " sslmode=disable")
    finally:
        server.shutdown()


# -- pieces ---------------------------------------------------------------------------------------------------------


def test_types_go_out_as_postgresql_s():
    assert pg.oid_of("VARCHAR") == pg.TEXT and pg.oid_of("DOUBLE") == pg.FLOAT8 and pg.oid_of("BIGINT") == pg.INT8
    assert pg.oid_of("DECIMAL(18,3)") == pg.NUMERIC and pg.format_type(pg.NUMERIC, pg.typmod_of("DECIMAL(18,3)")) \
        == "numeric(18,3)"
    assert pg.oid_of("VARCHAR[]") == 1009 and pg.oid_of("STRUCT(a INTEGER)") == pg.JSON_
    assert pg.oid_of("STRUCT(a INTEGER)[]") == pg.JSON_ and pg.oid_of("TIMESTAMP WITH TIME ZONE") == pg.TIMESTAMPTZ
    assert pg.to_text(["a b", None, "c"], 1009) == '{"a b",NULL,c}' and pg.to_text(True, pg.BOOL) == "t"
    for text in ["0.001", "12345.678", "-0.5", "1E+3", "0"]:
        raw = pg.to_binary(decimal.Decimal(text), pg.NUMERIC)
        n, weight, sign, scale = struct.unpack_from("!hhHh", raw)
        digits = struct.unpack_from(f"!{n}H", raw, 8)
        value = sum((decimal.Decimal(d) * decimal.Decimal(10000) ** (weight - i) for i, d in enumerate(digits)),
                    decimal.Decimal(0))
        assert (-value if sign else value) == decimal.Decimal(text)


def test_catalog_rewrite_keeps_strings_and_comments():
    from duckduck.addresses import _masked

    duck, _, _ = _duck()
    import duckdb

    cat = pgcat.PgCatalog(duckdb.connect(), duck)
    cat.refresh()
    q = pgcat.rewrite("SELECT 'pg_class', c.relname FROM pg_catalog.pg_class c -- from pg_type\n"
                      "WHERE c.oid = 'nvd.cves'::regclass AND c.relname::name = 'x'", cat, "me", _masked)
    assert "'pg_class'" in q and "-- from pg_type" in q and f"{pgcat.SCHEMA}.pg_class c" in q
    assert str(cat.oids[("nvd", "cves")]) in q and "::VARCHAR" in q


# -- what DBeaver / pgjdbc do beyond plain queries --------------------------------------------------------------------


def test_array_literals_the_way_pgjdbc_sends_its_keyword_list(served):
    w = Wire(served[0].port)
    _, rows, _, error = w.query("SELECT count(*) FROM pg_catalog.pg_get_keywords() "
                                "WHERE word <> ALL ('{a,abs,select,\"null\"}'::text[]) AND word = 'select'")
    assert error is None and rows == [["0"]]
    assert w.query("SELECT '{x,\"y z\",NULL}'::text[] AS a")[1] == [['{x,"y z",NULL}']]
    assert w.query("SELECT '{not an array' AS t")[1] == [["{not an array"]]  # only real array casts change
    w.close()


def test_the_active_schema_is_current_schema_and_where_bare_names_are_found(served):
    w = Wire(served[0].port)
    assert w.query("SET search_path TO nvd, public")[2] == ["SET"]
    assert w.query("SELECT current_schema()")[1] == [["nvd"]]
    assert w.query("SELECT count(*) FROM cves")[1] == [["2"]]  # nvd.cves
    assert w.query("SELECT count(*) FROM high_cves")[1] == [["1"]]  # a table of its own stays itself
    w.query("SET search_path = \"$user\", public")
    assert w.query("SELECT current_schema()")[1] == [["public"]]
    w.close()


def test_a_client_asking_for_one_table_s_columns_gets_them_read_now(served):
    server, _, _, calls = served
    w = Wire(server.port)
    sql = ("SELECT a.attname FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
           "WHERE c.relname = '{}' AND a.attnum > 0 ORDER BY a.attnum")
    assert w.query(sql.format("cves"))[1] == [["id"], ["severity"], ["score"]]
    assert calls["cves"] == 1
    # a whole schema's listing doesn't read every table
    before = calls["cves"]
    w.query("SELECT c.relname, a.attname FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c "
            "ON c.oid = a.attrelid JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 's3_data'")
    assert calls["cves"] == before
    w.close()


def test_sql_generated_from_the_placeholder_column_still_runs(served):
    w = Wire(served[0].port)
    cols, rows, _, error = w.query(f"SELECT x.{pgcat.UNKNOWN_COLUMN} FROM nvd.cves x ORDER BY {pgcat.UNKNOWN_COLUMN}")
    assert error is None and [c[0] for c in cols] == ["id", "severity", "score"] and len(rows) == 2
    error = w.query(f"SELECT * FROM nvd.cves WHERE {pgcat.UNKNOWN_COLUMN} = 'x'")[3]
    assert error.startswith("42703:") and "run SELECT * FROM it once" in error
    w.close()


def test_psycopg_sends_a_list_as_an_array(served):
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(f"host=127.0.0.1 port={served[0].port} dbname=duckduck user=me", autocommit=True) as c:
        assert c.execute("SELECT %s::text[] AS a", [["x", "y z", None]]).fetchone() == (["x", "y z", None],)
        assert c.execute("SELECT id FROM nvd.cves WHERE severity = ANY(%s) ORDER BY id", [["HIGH", "LOW"]]).fetchall() \
            == [("CVE-1",), ("CVE-2",)]
